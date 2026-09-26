import logging
from typing import Dict, Any, Optional
from uuid import UUID
from app.config import settings
from app.database.session import SessionLocal
from app.models import UploadedFile
from app.models.rpa_job import RpaJob, RpaJobStatus
from app.models.transaction import Transaction
from app.parsers.pipeline import TransactionParsingPipeline
from app.services.transaction_storage import TransactionStorageService, parse_date_flexible

logger = logging.getLogger(__name__)

REDIS_URL = settings.REDIS_URL

import time
from app.utils.metrics import metrics_collector

def process_file_parsing_task(file_id: UUID, file_path: str, user_id: UUID = None, account_id: UUID = None, pdf_password: Optional[str] = None, rpa_job_id: UUID = None, source_channel: Optional[str] = None) -> Dict[str, Any]:
    logger.info(f"[Parsing Job] Starting task for File ID: {file_id} (Password Provided: {bool(pdf_password)})")
    start_time = time.time()
    db = SessionLocal()
    db_file = None
    summary = {
        "file_id": str(file_id),
        "status": "FAILED",
        "total_extracted": 0,
        "total_valid": 0,
        "total_stored": 0,
        "validation_errors": [],
        "rule_matches": 0,
        "ml_predictions": 0,
        "error_message": None
    }
    try:
        # Step 1: Update status to PROCESSING
        import uuid as uuid_mod
        file_uuid = file_id
        if isinstance(file_id, str):
            try:
                file_uuid = uuid_mod.UUID(file_id)
            except Exception:
                file_uuid = file_id

        if isinstance(user_id, str):
            try:
                user_id = uuid_mod.UUID(user_id)
            except Exception:
                pass

        if isinstance(account_id, str):
            try:
                account_id = uuid_mod.UUID(account_id)
            except Exception:
                pass

        db_file = db.query(UploadedFile).filter(UploadedFile.id == file_uuid).first()
        if db_file:
            db_file.status = "PROCESSING"
            db.commit()

        _update_rpa_job_status(db, rpa_job_id, RpaJobStatus.PARSING)

        # Step 2-7: Execute parsing, validation, rule engine, and ML model via Pipeline
        pipeline = TransactionParsingPipeline()
        pipeline_output = pipeline.process_file_with_validation(file_path, pdf_password=pdf_password)

        processed_txns = pipeline_output["transactions"]
        val_errors = pipeline_output["errors"]

        rule_count = sum(1 for t in processed_txns if t.get("decision", {}).get("prediction_source") == "Rule Engine")
        ml_count = sum(1 for t in processed_txns if t.get("decision", {}).get("prediction_source") == "ML Model")

        _update_rpa_job_status(db, rpa_job_id, RpaJobStatus.IMPORTING)

        # Step 8: Save processed transactions (ProcessedTransaction table) & create Statement + Transaction rows
        storage_service = TransactionStorageService()
        stored_records = storage_service.store_processed_transactions(
            db=db,
            processed_transactions=processed_txns,
            file_id=str(file_id),
            user_id=str(user_id) if user_id else (str(db_file.user_id) if db_file else None),
            model_version="v1.0.0"
        )

        # Step 9: Create/update Statement record & write to transactions table
        import hashlib, pathlib
        from app.models.statement import Statement, SourceChannel
        from app.services.transaction_storage import resolve_account_id

        target_user_id = user_id if user_id else (db_file.user_id if db_file else None)
        sha = hashlib.sha256(pathlib.Path(file_path).read_bytes()).hexdigest()
        acc_id = account_id if account_id else (resolve_account_id(db, target_user_id, processed_txns) if target_user_id else None)
        
        stmt = None
        if db_file:
            stmt = db.query(Statement).filter(
                Statement.uploaded_file_id == db_file.id,
                Statement.user_id == target_user_id
            ).first()

        if not stmt:
            stmt = db.query(Statement).filter(
                Statement.file_sha256 == sha,
                Statement.user_id == target_user_id
            ).first()

        if stmt:
            if db_file:
                stmt.uploaded_file_id = db_file.id
            if acc_id and stmt.account_id != acc_id:
                stmt.account_id = acc_id

        if db_file:
            db_file.status = "COMPLETED" if (pipeline_output.get("total_valid", 0) > 0 or len(processed_txns) > 0) else "FAILED"

        if stmt is None and target_user_id:
            parsed_dates = [parse_date_flexible(t["date"]).date() for t in processed_txns if t.get("date") and parse_date_flexible(t["date"])]
            
            # Derive opening balance correctly:
            #   The parser returns a *running* balance after each row.
            #   processed_txns[0]["balance"] is the balance AFTER the first
            #   transaction, not before it.  Reconstruct the true opening by
            #   reversing the first row's movement:
            #       opening = balance_after_row1 - credit_row1 + debit_row1
            from decimal import Decimal
            def _to_paise(v):
                if v in (None, "", 0, "0", 0.0): return None
                try: return int((Decimal(str(v)) * 100).quantize(Decimal("1")))
                except Exception: return None

            opening_balance_paise = None
            closing_balance_paise = None
            if processed_txns:
                first_txn = processed_txns[0]
                last_txn  = processed_txns[-1]
                last_bal  = last_txn.get("balance")

                # Prefer explicit statement-level closing balance if parser provided one
                statement_meta = pipeline_output.get("statement_meta", {})
                stmt_closing = statement_meta.get("closing_balance") if statement_meta else None
                if stmt_closing is not None:
                    closing_balance_paise = _to_paise(stmt_closing)
                else:
                    closing_balance_paise = _to_paise(last_bal)

                first_bal_raw = first_txn.get("balance")
                if first_bal_raw not in (None, "", 0, "0", 0.0):
                    try:
                        first_bal_d  = Decimal(str(first_bal_raw))
                        first_debit  = Decimal(str(first_txn.get("debit",  0) or 0))
                        first_credit = Decimal(str(first_txn.get("credit", 0) or 0))
                        # opening = running balance after row1, reversed
                        opening_d = first_bal_d - first_credit + first_debit
                        opening_balance_paise = int((opening_d * 100).quantize(Decimal("1")))
                    except Exception:
                        opening_balance_paise = None

            effective_channel = source_channel or SourceChannel.UPLOAD.value
            stmt = Statement(
                user_id=target_user_id,
                account_id=acc_id,
                source_channel=effective_channel,
                file_sha256=sha,
                storage_path=file_path,
                original_filename=db_file.filename if db_file else pathlib.Path(file_path).name,
                period_from=min(parsed_dates) if parsed_dates else None,
                period_to=max(parsed_dates) if parsed_dates else None,
                opening_balance_paise=opening_balance_paise,
                closing_balance_paise=closing_balance_paise,
                status="parsed",
                uploaded_file_id=db_file.id if db_file else None,
            )
            db.add(stmt)
            db.flush()

        if stmt:
            if acc_id and not stmt.account_id:
                stmt.account_id = acc_id
            if source_channel:
                stmt.source_channel = source_channel
            db.query(Transaction).filter(Transaction.statement_id == stmt.id).delete(synchronize_session=False)
            db.flush()
            storage_service.store_transactions(
                db=db,
                processed_txns=processed_txns,
                user_id=target_user_id,
                account_id=stmt.account_id,
                statement_id=stmt.id,
                source_channel=stmt.source_channel or "MANUAL_UPLOAD"
            )
            cont_passed = pipeline_output.get("continuity_passed", True)
            stmt.reconciled = cont_passed
            if not cont_passed:
                stmt.reconciliation_note = (
                    f"continuity_gate_failed: pass_rate="
                    f"{pipeline_output.get('continuity_pass_rate', 0.0) * 100:.1f}%"
                )
        elif processed_txns:
            # No Statement could be created — target_user_id was unresolvable — so
            # the canonical ledger write above was skipped while the staging write
            # had already succeeded. That asymmetry is exactly how 3,288 rows ended
            # up living only in processed_transactions. Fail loudly instead: the
            # rows are still recoverable from staging, but nobody can act on a
            # divergence they are never told about.
            logger.error(
                f"[Parsing Queue] LEDGER WRITE SKIPPED for file '{file_path}': no Statement "
                f"could be created (user_id={target_user_id!r}, account_id={acc_id!r}). "
                f"{len(processed_txns)} transaction(s) exist in processed_transactions but NOT "
                f"in the canonical transactions table. This file needs re-ingestion with a "
                f"resolvable user."
            )
            pipeline_output["ledger_write_skipped"] = True
            pipeline_output["ledger_write_skipped_reason"] = (
                "no Statement could be created; user_id unresolvable"
            )

        db.commit()

        # Anomalies and policy compliance are recomputed HERE, as part of
        # ingestion, so the dashboard reflects the statement that was just
        # uploaded without the user having to find and press Re-scan. Failures
        # are logged and reported but never fail the upload — the ledger write
        # above has already committed, and turning a detector bug into a failed
        # upload would push the user into re-uploading and creating duplicates.
        # How well this statement's narration format was understood. Logged at
        # ingest so an unfamiliar bank is visible the moment it arrives, rather
        # than surfacing later as a user wondering why one supplier appears in
        # their review queue four times.
        grouping_report = None
        try:
            from app.categorization.counterparty import grouping_health
            grouping_report = grouping_health(
                str(t.get("description") or t.get("raw_text") or "")
                for t in processed_txns
            ).as_dict()
            if not grouping_report["is_healthy"]:
                logger.warning(
                    "[Grouping] file '%s': coverage %.1f%% but %.1f%% of rows are "
                    "grouped by narration text and look like they name a party. "
                    "This statement's format is only partly understood. "
                    "Examples: %s",
                    file_path,
                    grouping_report["coverage"] * 100,
                    grouping_report["suspect_share"] * 100,
                    grouping_report["suspect_keys"][:5],
                )
        except Exception:
            logger.exception("[Grouping] health check failed; ingestion continues")

        compliance_summary_result = None
        if target_user_id:
            from app.compliance.auto_scan import run_scan_safely
            compliance_summary_result = run_scan_safely(
                db, target_user_id, account_id=acc_id
            )

        _update_rpa_job_status(db, rpa_job_id, RpaJobStatus.SUCCESS)


        duration = time.time() - start_time
        metrics_collector.record_parsing_job(
            duration_sec=duration,
            total_extracted=pipeline_output["total_extracted"],
            total_valid=pipeline_output["total_valid"],
            val_errors=val_errors,
            success=True
        )

        # Step 10: Return a processing summary.
        #
        # "Parsed 60 rows" and "stored 60 rows" are different claims, and only
        # the second one means the user has their statement. A run that
        # extracted rows and wrote none is a FAILURE however cleanly the parser
        # ran — reporting COMPLETED there sends the user to a Transactions tab
        # that is empty for no visible reason, with a green tick behind them.
        extracted = pipeline_output["total_valid"] or pipeline_output["total_extracted"]

        # Counted from the canonical table, not from the staging write. The two
        # can disagree: store_processed_transactions() writing 60 rows into
        # processed_transactions says nothing about whether the ledger the
        # Transactions tab, dashboard and reports all read from received them.
        ledger_rows = 0
        if stmt is not None:
            from sqlalchemy import func as _sa_func
            ledger_rows = db.query(_sa_func.count(Transaction.id)).filter(
                Transaction.statement_id == stmt.id
            ).scalar() or 0

        stored_none = extracted > 0 and (len(stored_records) == 0 or ledger_rows == 0)
        if stored_none:
            logger.error(
                "[Parsing Job] File ID %s: %d row(s) parsed and validated but "
                "%d reached processed_transactions and %d reached the ledger. "
                "Reporting FAILED — the user must not be told this succeeded.",
                file_id, extracted, len(stored_records), ledger_rows,
            )

        # The UploadedFile row was set to COMPLETED before the ledger write was
        # attempted, so on its own it records "we got this far", not "the user
        # has their statement". Correct it here, where the answer is known.
        # It matters beyond the label: /files/upload keys its duplicate-upload
        # short-circuit off this status, so a row left COMPLETED after storing
        # nothing would make re-uploading the statement a permanent no-op.
        if db_file:
            desired = "FAILED" if stored_none else "COMPLETED"
            if db_file.status != desired:
                db_file.status = desired
                db.commit()

        summary.update({
            "status": "FAILED" if stored_none else "COMPLETED",
            "error": (
                f"{extracted} transaction(s) were read from this file but none "
                f"could be saved. The file itself is fine — check the server log "
                f"for the reason, then upload it again."
            ) if stored_none else None,
            "total_extracted": pipeline_output["total_extracted"],
            "total_valid": pipeline_output["total_valid"],
            "total_stored": len(stored_records),
            "validation_errors": val_errors,
            "rule_matches": rule_count,
            "ml_predictions": ml_count,
            # Reported so the UI can show the fresh figures directly, and so a
            # scan that failed is visible rather than silently stale.
            "compliance_scan": compliance_summary_result,
            "grouping_health": grouping_report,
        })

        if not stored_none:
            logger.info(
                f"[Parsing Job] Successfully completed task for File ID: {file_id}. "
                f"Saved {len(stored_records)} transactions in {duration:.2f}s."
            )
        return summary
    except Exception as e:
        duration = time.time() - start_time
        metrics_collector.record_parsing_job(
            duration_sec=duration,
            total_extracted=0,
            total_valid=0,
            val_errors=[],
            success=False
        )
        logger.error(f"[Parsing Job Error] Failed processing file {file_id}: {e}", exc_info=True)
        _mark_file_failed(db, db_file, file_id)
        _update_rpa_job_status(db, rpa_job_id, RpaJobStatus.FAILED, str(e))
        summary["error_message"] = str(e)
        return summary
    finally:
        db.close()


def _mark_file_failed(db, db_file, file_id) -> None:
    """Record the failure on the UploadedFile row, whatever state the session is in.

    This used to be `db_file.status = "FAILED"; db.commit()` inline in the
    except block, and it did not work when it mattered most. The exception that
    lands here has usually come from a statement postgres refused, and postgres
    then refuses every further command in that transaction — so the commit
    raised PendingRollbackError *from inside the except block* and escaped the
    task. The status stayed at 'PROCESSING', the value written when the job
    started.

    A file stuck in PROCESSING is indistinguishable from one still being
    worked on, and /files/upload treats it as already-ingested: re-uploading
    the same statement returned the dead file id and queued nothing. The user's
    only recovery action became a silent no-op, and the Transactions tab,
    dashboard and reports stayed empty with no error visible anywhere.

    Rolling back first is what makes the write land. This function must never
    raise: it runs on the failure path, and a failure to record a failure would
    replace a legible error with a stack trace from the handler.
    """
    if db_file is None:
        return
    try:
        db.rollback()
        fresh = db.query(UploadedFile).filter(UploadedFile.id == db_file.id).first()
        if fresh is not None:
            fresh.status = "FAILED"
            db.commit()
    except Exception:
        logger.exception(
            "[Parsing Job] Could not mark File ID %s as FAILED. It may be left "
            "in PROCESSING; re-uploading the file will still re-queue it.",
            file_id,
        )
        try:
            db.rollback()
        except Exception:
            pass


def _update_rpa_job_status(db, rpa_job_id, status, error_message: Optional[str] = None) -> None:
    if not rpa_job_id:
        return
    try:
        if isinstance(rpa_job_id, str):
            import uuid as uuid_mod
            rpa_job_id = uuid_mod.UUID(rpa_job_id)
        job = db.query(RpaJob).filter(RpaJob.id == rpa_job_id).first()
        if not job:
            return
        job.status = status
        if error_message:
            job.error_message = error_message
        db.commit()
    except Exception:
        db.rollback()
