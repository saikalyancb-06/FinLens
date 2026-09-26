"""Bridge from a discovered document into the application's existing pipeline.

Nothing about parsing, validation, normalisation, categorisation, deduplication
or storage is reimplemented here. A discovered statement becomes an
``UploadedFile`` and is handed to ``process_file_parsing_task`` — the same
function a manual upload and an RPA download go through — so email-sourced
transactions land in the same tables, with the same categories, as every other
source.

Idempotency comes from the pipeline itself: ``statements`` carries a unique index
on ``(user_id, file_sha256)``, and ``process_file_parsing_task`` reuses the
existing statement row and replaces its transactions rather than appending. Two
scans of the same mailbox therefore cannot produce two copies of a transaction.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

# Import the session module before any individual model module: it owns the
# mapper-registration loop that must run first. See app/mailbox/registry.py
# for the full explanation of why the order matters.
import app.database.session  # noqa: F401  (imported for its side effect)
from app.email.models import EmailAttachment, ImportHistory
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.models.uploaded_file import UploadedFile

logger = logging.getLogger(__name__)

#: How a statement reached the system, per provider. Displayed in the ledger and
#: on reports; "GMAIL" is preserved verbatim so existing rows stay consistent.
SOURCE_CHANNELS = {
    "gmail": "GMAIL",
    "microsoft": "MICROSOFT",
    "imap": "IMAP",
}


def source_channel_for(provider: Optional[str]) -> str:
    return SOURCE_CHANNELS.get((provider or "").strip().lower(), "EMAIL")


@dataclass
class IngestResult:
    status: str = "FAILED"            # SUCCESS | FAILED
    file_id: Optional[str] = None
    transactions_created: int = 0
    total_extracted: int = 0
    total_valid: int = 0
    error_message: Optional[str] = None
    summary: Dict[str, Any] = None  # type: ignore[assignment]

    @property
    def ok(self) -> bool:
        return self.status == "SUCCESS"


def ingest_attachment(
    db: Session,
    attachment: EmailAttachment,
    *,
    user_id,
    account_id,
    pdf_password: Optional[str] = None,
    provider: Optional[str] = None,
) -> IngestResult:
    """Run one discovered document through the canonical ingestion pipeline.

    ``account_id`` must already have been validated as belonging to ``user_id``
    by the caller — this function does not re-check ownership because it has no
    request context to check it against, and a function that silently accepts
    whatever account id it is handed should not also look like it is enforcing
    something.
    """
    from app.services.parsing_queue import process_file_parsing_task

    result = IngestResult(summary={})

    if not attachment.local_path or not os.path.exists(attachment.local_path):
        result.error_message = (
            f"The downloaded copy of '{attachment.filename}' is no longer on disk; "
            "re-scan the mailbox to fetch it again."
        )
        return result

    uploaded_file = UploadedFile(
        user_id=user_id,
        filename=attachment.filename,
        file_path=attachment.local_path,
        file_size=attachment.file_size_bytes,
        mime_type=attachment.mime_type,
        file_sha256=attachment.file_hash,
        status="PROCESSING",
    )
    db.add(uploaded_file)
    db.commit()
    db.refresh(uploaded_file)
    result.file_id = str(uploaded_file.id)

    try:
        summary = process_file_parsing_task(
            file_id=uploaded_file.id,
            file_path=attachment.local_path,
            user_id=user_id,
            account_id=account_id,
            pdf_password=pdf_password,
            source_channel=source_channel_for(provider or _provider_of(db, attachment)),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("[Statement Ingest] parsing failed for attachment %s", attachment.id)
        result.error_message = str(exc)
        _record_history(db, attachment, uploaded_file, account_id, user_id, result)
        return result

    result.summary = summary or {}
    result.total_extracted = int(result.summary.get("total_extracted", 0) or 0)
    result.total_valid = int(result.summary.get("total_valid", 0) or 0)

    statement = db.query(Statement).filter(
        Statement.uploaded_file_id == uploaded_file.id,
        Statement.user_id == user_id,
    ).first()
    if statement is None and attachment.file_hash:
        # The pipeline reuses an existing statement when the same bytes were
        # ingested before, in which case it is not linked to *this* upload row.
        statement = db.query(Statement).filter(
            Statement.file_sha256 == attachment.file_hash,
            Statement.user_id == user_id,
        ).first()

    if statement is not None:
        result.transactions_created = db.query(Transaction).filter(
            Transaction.statement_id == statement.id
        ).count()

    stored = int(result.summary.get("total_stored", 0) or 0)
    if result.transactions_created == 0 and result.total_valid == 0 and stored == 0:
        uploaded_file.status = "FAILED"
        attachment.import_status = "FAILED"
        result.error_message = (
            result.summary.get("error_message")
            or f"Parsing produced 0 valid transactions for '{attachment.filename}'."
        )
        _record_history(db, attachment, uploaded_file, account_id, user_id, result)
        return result

    attachment.import_status = "IMPORTED"
    attachment.file_id = uploaded_file.id
    if attachment.classification not in ("PDF_PASSWORD_REQUIRED", "PASSWORD_INVALID"):
        attachment.classification = "BANK_STATEMENT_CONFIRMED"
    result.status = "SUCCESS"
    result.transactions_created = result.transactions_created or stored or result.total_valid
    _record_history(db, attachment, uploaded_file, account_id, user_id, result)
    return result


def _provider_of(db: Session, attachment: EmailAttachment) -> Optional[str]:
    if not attachment.connected_account_id:
        return None
    from app.email.models import ConnectedAccount

    connection = db.query(ConnectedAccount).filter(
        ConnectedAccount.id == attachment.connected_account_id
    ).first()
    return connection.provider if connection else None


def _record_history(
    db: Session,
    attachment: EmailAttachment,
    uploaded_file: UploadedFile,
    account_id,
    user_id,
    result: IngestResult,
) -> None:
    try:
        db.add(ImportHistory(
            user_id=user_id,
            attachment_id=attachment.id,
            file_id=uploaded_file.id if uploaded_file else None,
            account_id=account_id,
            status="SUCCESS" if result.ok else "FAILED",
            bank_name=attachment.institution_name or attachment.bank_name,
            filename=attachment.filename,
            total_extracted=result.total_extracted,
            total_valid=result.total_valid,
            total_stored=result.transactions_created,
            error_message=result.error_message,
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[Statement Ingest] could not write import history for %s", attachment.id)
