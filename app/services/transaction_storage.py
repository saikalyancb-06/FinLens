import logging
import datetime
import hashlib
import re
import uuid
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from app.models.transaction import Transaction
from app.currency.fx_parser import derive_rate, parse_fx_leg
from app.models.processed_transaction import ProcessedTransaction

logger = logging.getLogger(__name__)

def parse_date_flexible(date_str: str) -> Optional[datetime.datetime]:
    if not date_str or not str(date_str).strip():
        return None
    s = str(date_str).strip()
    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%Y/%m/%d",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d, %Y",
        "%d-%b-%Y"
    ]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

class TransactionStorageService:
    def store_processed_transactions(
        self,
        db: Session,
        processed_transactions: List[Dict[str, Any]],
        file_id: Optional[str] = None,
        user_id: Optional[str] = None,
        model_version: str = "v1.0.0"
    ) -> List[ProcessedTransaction]:
        """
        Stores processed transactions into PostgreSQL with deduplication per uploaded file.
        """
        try:
            if file_id:
                try:
                    target_file_id = uuid.UUID(file_id) if isinstance(file_id, str) else file_id
                    db.query(ProcessedTransaction).filter(
                        ProcessedTransaction.file_id == target_file_id
                    ).delete(synchronize_session=False)
                    db.commit()
                except Exception as exc:  # noqa: BLE001 - re-parsing must not hard-fail here
                    # The rollback is the point. A failed DELETE poisons the
                    # PostgreSQL transaction, so every INSERT below then died with
                    # "current transaction is aborted" — the swallowed error took
                    # the whole batch with it while the log stayed silent.
                    db.rollback()
                    logger.warning(
                        "[Storage] could not clear previous processed rows for file %s (%s); "
                        "continuing — this batch may leave stale rows behind.",
                        file_id, exc,
                    )

            db_records: List[ProcessedTransaction] = []
            now = datetime.datetime.utcnow()

            for txn in processed_transactions:
                raw_text = txn.get("raw_text", "")
                date_str = txn.get("date", "")
                
                # Parse Date with flexible formats
                parsed_date = parse_date_flexible(date_str)

                desc = str(txn.get("description", ""))
                debit = float(txn.get("debit", 0.0))
                credit = float(txn.get("credit", 0.0))
                amount = float(txn.get("amount", 0.0))
                balance = float(txn.get("balance", 0.0))
                ref_no = str(txn.get("reference_number", ""))
                txn_type = str(txn.get("transaction_type", ""))

                decision = txn.get("decision", {})
                rule_match = txn.get("rule_match", {})

                rule_cat = rule_match.get("category", "")
                rule_conf = float(decision.get("rule_confidence", rule_match.get("confidence", 0.0)))
                matched_rule = str(decision.get("matched_rule", rule_match.get("matched_rule", "")))

                ml_cat = decision.get("category", "") if decision.get("prediction_source") == "ML Model" else None
                ml_conf = float(decision.get("ml_confidence", 0.0))
                ml_top_three = decision.get("top_three_ml", [])

                final_category = str(decision.get("category", "Uncategorized"))
                confidence = float(decision.get("final_confidence", 0.0))
                prediction_source = str(decision.get("prediction_source", "Unknown"))
                reasoning = str(decision.get("reasoning", ""))

                parsed_file_id = None
                if file_id:
                    try:
                        parsed_file_id = uuid.UUID(file_id) if isinstance(file_id, str) else file_id
                    except Exception:
                        pass

                parsed_user_id = None
                if user_id:
                    try:
                        parsed_user_id = uuid.UUID(user_id) if isinstance(user_id, str) else user_id
                    except Exception:
                        pass

                record = ProcessedTransaction(
                    id=uuid.uuid4(),
                    file_id=parsed_file_id,
                    user_id=parsed_user_id,
                    original_raw_text=raw_text,
                    date=parsed_date,
                    description=desc,
                    debit=debit,
                    credit=credit,
                    amount=amount,
                    balance=balance,
                    reference_number=ref_no,
                    transaction_type=txn_type,
                    rule_category=rule_cat,
                    rule_confidence=rule_conf,
                    rule_matched=matched_rule,
                    ml_category=ml_cat,
                    ml_confidence=ml_conf,
                    ml_top_three=ml_top_three,
                    final_category=final_category,
                    confidence=confidence,
                    prediction_source=prediction_source,
                    reasoning=reasoning,
                    model_version=model_version,
                    processing_timestamp=now
                )
                db_records.append(record)

            BATCH_SIZE = 1000
            for i in range(0, len(db_records), BATCH_SIZE):
                chunk = db_records[i:i + BATCH_SIZE]
                db.add_all(chunk)
                db.commit()

            return db_records
        except Exception as e:
            logger.error(f"[Database Error] Failed saving processed transactions for file '{file_id}': {e}", exc_info=True)
            db.rollback()
            raise e

    def store_transactions(
        self,
        db: Session,
        processed_txns: List[Dict[str, Any]],
        *,
        user_id: Any,
        account_id: Optional[Any] = None,
        statement_id: Optional[Any] = None,
        entity_id: Optional[Any] = None,
        source_channel: str = "MANUAL_UPLOAD"
    ) -> List[Transaction]:
        from decimal import Decimal, ROUND_HALF_UP
        from app.models.transaction import Transaction, Direction, SourceType
        from app.models.account import Account
        from app.models.category import Category
        from app.models.prediction import Prediction

        parsed_user_id = uuid.UUID(str(user_id)) if isinstance(user_id, str) else user_id
        parsed_account_id = uuid.UUID(str(account_id)) if account_id and isinstance(account_id, str) else account_id
        parsed_statement_id = uuid.UUID(str(statement_id)) if statement_id and isinstance(statement_id, str) else statement_id
        parsed_entity_id = uuid.UUID(str(entity_id)) if entity_id and isinstance(entity_id, str) else entity_id

        # Resolve entity_id from Account if not explicitly passed
        if not parsed_entity_id and parsed_account_id:
            acct = db.query(Account).filter(Account.id == parsed_account_id).first()
            if acct and acct.entity_id:
                parsed_entity_id = acct.entity_id

        def _paise(v) -> Optional[int]:
            """Rupees (any representation) to integer paise, or None for "no amount".

            Two things this gets right that the previous version did not:

            * Zero is ALWAYS None. The old membership test caught 0 / "0" / 0.0 but
              not "0.00" or "0.0", which is exactly how a CSV writes an empty
              debit column — so the same absent amount landed as NULL from one
              statement and as 0 from the next, and `debit IS NULL` stopped
              meaning what it says.
            * Ties round HALF UP, the convention Indian bank statements use and
              the one app/currency/service.py already applies. Decimal's default
              is half-to-even, so a three-decimal figure such as 12.345 became
              1234 paise here and 1235 anywhere else in the application.
            """
            if v is None or v == "":
                return None
            try:
                d = Decimal(str(v).strip().replace(",", ""))
            except Exception:
                return None
            if d == 0:
                return None
            return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

        def detect_method(desc: str) -> Optional[str]:
            if not desc: return None
            u = desc.upper()
            for m in ["UPI", "NEFT", "RTGS", "IMPS", "CHQ", "CHEQUE", "ATM"]:
                if m in u: return m
            return None

        from app.parsers.normalizer import extract_reference_number
        def extract_reference(desc: str) -> Optional[str]:
            if not desc: return None
            ref = extract_reference_number(desc)
            return ref if ref else None

        def compute_txn_hash(
            account_id, txn_date, debit, credit, narration, reference
        ) -> str:
            """Content fingerprint for the indexed Transaction.hash dedup column.

            The column is indexed for deduplication scans but was never written,
            leaving it NULL on every row. Populating it lets duplicate detection
            key off an index instead of the O(n^2) pairwise comparison in
            DeduplicationEngine.
            """
            parts = [
                str(account_id or ""),
                txn_date.isoformat() if txn_date else "",
                str(debit or 0),
                str(credit or 0),
                re.sub(r"\s+", " ", (narration or "")).strip().upper(),
                (reference or "").strip().upper(),
            ]
            return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

        # Seed categories if missing and map Category name to category_id
        from app.services.category_seeder import (
            normalize_category_name, resolve_path, seed_categories, seed_category_tree,
        )
        from app.categorization.deep import classify_deep
        cat_map = seed_categories(db)
        # slug -> id for the hierarchy. Seeded here rather than at startup so a
        # database that has had the migration but not a restart still ingests
        # into a complete tree.
        tree_map = seed_category_tree(db)

        # Account type, read once for the batch. It breaks exactly one tie: the
        # same large NEFT credit is salary on a salary account and revenue on a
        # current account, and no amount of narration parsing settles that.
        account_type = None
        if parsed_account_id:
            try:
                from app.models.account import Account
                acct = db.query(Account).filter(Account.id == parsed_account_id).first()
                account_type = getattr(acct, "account_type", None) if acct else None
            except Exception:  # pragma: no cover - a missing account is not fatal
                account_type = None


        from app.categorization.hybrid import classify_transaction
        from app.categorization.counterparty_memory import load_memory, lookup
        from app.categorization.hybrid import memory_should_override

        # Loaded once for the whole batch. A 1,800-row statement would otherwise
        # issue 1,800 queries against a table holding a few hundred rows.
        #
        # GUARDED, and the reason matters. This is a lookup that IMPROVES
        # categorisation; it is not what makes a transaction real. When the
        # query failed — a schema that had not caught up with the models, in
        # practice — the exception propagated out of here and took the entire
        # ledger write with it. The user's statement parsed cleanly, 60 rows
        # validated, and nothing was stored: an empty Transactions tab and an
        # empty review queue, from a convenience feature.
        #
        # Degrading to "no memory" costs the user one round of re-categorising
        # counterparties they had already decided. Losing the write costs them
        # the statement.
        # Isolated in a SAVEPOINT, and that detail is the whole fix. A failed
        # statement poisons the enclosing postgres transaction: every later
        # statement errors until someone rolls back. But a plain db.rollback()
        # here throws away work already flushed in this transaction — the
        # Statement row — and the transaction INSERT then fails with
        #
        #   ForeignKeyViolation: violates constraint transactions_statement_id_fkey
        #
        # which loses the ledger write just as thoroughly as the original error.
        # A savepoint rolls back only this lookup and leaves the outer
        # transaction, and the Statement, intact.
        cp_memory = {}
        if parsed_user_id:
            try:
                with db.begin_nested():
                    cp_memory = load_memory(db, parsed_user_id)
            except Exception as exc:  # noqa: BLE001 - see comment above
                cp_memory = {}
                logger.error(
                    "[Storage] counterparty memory unavailable (%s). Continuing "
                    "without it: transactions will still be stored, but "
                    "previously-categorised counterparties will not be applied "
                    "automatically on this batch. Run 'alembic upgrade head' if "
                    "this is a schema mismatch.", exc,
                )

        rows = []
        row_sources = []
        hybrid_results = []
        for idx, t in enumerate(processed_txns):
            debit = _paise(t.get("debit"))
            credit = _paise(t.get("credit"))
            balance = _paise(t.get("balance"))
            dt = parse_date_flexible(t.get("date"))

            if debit and credit:
                raise ValueError(f"row {idx}: both debit and credit set")
            if not debit and not credit:
                continue

            raw_narration = str(t.get("raw_text") or t.get("description") or "")
            clean_narration = str(t.get("description", "")).upper().strip()
            row_idx = t.get("row_index", idx)

            # Categorise with the hybrid rule+ML engine. This is the same code path
            # the review queue re-runs for its suggestions, so what a reviewer sees
            # matches what ingestion decided. When it abstains, the transaction is
            # deliberately stored WITHOUT a category so it surfaces for review rather
            # than entering the ledger under a guess.
            hybrid_result = classify_transaction(
                narration=clean_narration or raw_narration,
                amount=(float(debit or credit or 0)) / 100.0,
                direction="CREDIT" if credit else "DEBIT",
                # Lets the trade-name rule tell a restaurant's fish SUPPLIER
                # from a fishmonger's own grocery run. Same keyword, different
                # books.
                account_type=account_type,
            )

            # The engine reads narration SHAPE; it cannot know what business the
            # other party is in. If the user has already told us — by
            # categorising any earlier row naming this counterparty — apply that
            # decision rather than sending an identical row back to review.
            # This only ever OVERRIDES an abstention: a rule that actually fired
            # (a bank charge, a tax) keeps its answer, because those are facts
            # about the transaction, not about who was paid.
            # Looked up unconditionally now, because the hierarchy treats a
            # confirmed human decision as its strongest evidence. The override
            # below still only applies to an abstention, exactly as before.
            mem_hit = (lookup(cp_memory, clean_narration or raw_narration)
                       if cp_memory else None)

            # A saved decision outranks a trade guess. Without the second
            # clause here, teaching the system that "KUMAR FISH" is Professional
            # Fees would be silently ignored on the next upload, because the
            # trade rule now answers confidently and the memory was only
            # consulted on an abstention. A keyword must never overrule a person.
            if cp_memory and memory_should_override(
                    hybrid_result.classification_rule, hybrid_result.requires_review):
                hit = mem_hit
                if hit:
                    hybrid_result.category = hit.category
                    hybrid_result.requires_review = False
                    hybrid_result.classification_method = "counterparty_memory"
                    hybrid_result.classification_confidence = 0.95
                    hybrid_result.classification_rule = f"counterparty:{hit.key}"
                    hybrid_result.explanation = (
                        f"Categorised as {hit.category} because you have already "
                        f"categorised {hit.display} that way "
                        f"({hit.times_confirmed} time"
                        f"{'s' if hit.times_confirmed != 1 else ''})."
                    )

            hybrid_results.append(hybrid_result)

            # ---- Hierarchical placement -------------------------------------
            # Built ON TOP of the answer above rather than replacing it: the
            # hybrid category is the anchor that usually fixes level 1, and this
            # resolves the rest of the path — but only as far as the narration,
            # the counterparty and the user's own past decisions support. A path
            # stops where the evidence stops, so depth varies row by row.
            deep = classify_deep(
                clean_narration or raw_narration,
                direction="credit" if credit else "debit",
                amount=(float(debit or credit or 0)) / 100.0,
                declared_method=t.get("transaction_type"),
                upstream_category=hybrid_result.category,
                upstream_confidence=hybrid_result.classification_confidence,
                upstream_requires_review=hybrid_result.requires_review,
                memory_category=mem_hit.category if mem_hit else None,
                memory_confirmations=mem_hit.times_confirmed if mem_hit else 0,
                account_type=account_type,
            )
            _deep_node_id, deep_path = resolve_path(db, deep.path, tree_map)

            # A category is written whenever the engine produced one, even if it
            # also wants the row confirmed. The engine has THREE outcomes, not
            # two: confident, provisional (a narration pattern matched, weaker
            # evidence than an exact merchant rule), and nothing at all.
            #
            # Provisional answers used to be computed and then discarded. On a
            # real 1,823-row statement that threw away 1,183 answers — 65% of the
            # file — and every one of them showed to the user as uncategorised.
            # The row still carries requires_review=True, so it appears in the
            # review queue for confirmation; it just no longer appears in the
            # reports as a void.
            _resolved = (
                hybrid_result.category
                and hybrid_result.category.lower() != "uncategorized"
            )
            if _resolved:
                cat_name = normalize_category_name(hybrid_result.category)
                matched_cat_id = (
                    cat_map.get(cat_name.lower())
                    or cat_map.get(hybrid_result.category.lower())
                )
            else:
                # The hybrid engine produced nothing. Fall back to a category the CALLER
                # supplied, if any — the Account Aggregator and email-alert paths
                # classify upstream and must not lose that work just because the
                # statement classifier could not read the narration. An alias name
                # ("Settlement") still resolves through the seeded category map, so
                # a supplied category never degrades to NULL.
                decision = t.get("decision", {}) or {}
                supplied = str(
                    decision.get("category")
                    or t.get("final_category")
                    or t.get("category")
                    or ""
                ).strip()
                if supplied and supplied.lower() != "uncategorized":
                    cat_name = normalize_category_name(supplied)
                    matched_cat_id = (
                        cat_map.get(cat_name.lower()) or cat_map.get(supplied.lower())
                    )
                    if matched_cat_id:
                        hybrid_result.requires_review = False
                        hybrid_result.category = cat_name
                        hybrid_result.classification_method = "upstream"
                        hybrid_result.explanation = (
                            f"Categorised as {cat_name} by the upstream ingestion source; "
                            f"the statement classifier abstained ({hybrid_result.explanation})"
                        )
                else:
                    matched_cat_id = None

            txn_reference = (
                str(t.get("reference_number")) if t.get("reference_number")
                else extract_reference(clean_narration)
            )
            txn_date_val = dt.date() if dt else datetime.date.today()

            txn = Transaction(
                id=uuid.uuid4(),
                user_id=parsed_user_id,
                account_id=parsed_account_id,
                entity_id=parsed_entity_id,
                statement_id=parsed_statement_id,
                source_type=SourceType.STATEMENT,
                source_channel=source_channel,
                direction=Direction.DEBIT if debit else Direction.CREDIT,
                debit_paise=debit,
                credit_paise=credit,
                balance_paise=balance,
                txn_date=txn_date_val,
                row_index=int(row_idx) if row_idx is not None else idx,
                narration_raw=raw_narration,
                narration_clean=clean_narration,
                payment_method=t.get("transaction_type") or detect_method(clean_narration),
                reference_no=txn_reference,
                # Deliberately the FLAT category, not the deepest tree node.
                # `category_id` is what the review queue, the prediction row and
                # the reports resolve a category NAME through, and those all
                # speak the flat vocabulary. Pointing it at a leaf made the
                # review queue report a Swiggy row as "Coffee" and an
                # unclassifiable row as "Other / Uncategorized" instead of the
                # sentinel that means "no decision". The tree link lives in
                # `category_path`, which is where the drill-down reads it.
                category_id=matched_cat_id,
                category=deep.category,
                category_path=deep_path,
                # The flat label stays on the row: the review queue's dropdown,
                # the saved counterparty decisions and the P&L reports are all
                # written against that vocabulary, and losing it here would
                # break them for every newly ingested statement.
                legacy_category=(
                    normalize_category_name(hybrid_result.category)
                    if hybrid_result.category
                    and hybrid_result.category.lower() != "uncategorized"
                    else None
                ),
                category_confidence=round(deep.confidence, 3),
                flow_type=deep.flow_type,
                transaction_method=deep.transaction_method,
                merchant=deep.merchant,
                counterparty=deep.counterparty,
                hash=compute_txn_hash(
                    parsed_account_id, txn_date_val, debit, credit,
                    clean_narration or raw_narration, txn_reference,
                ),
            )

            # Capture the foreign leg from the advice text, if the narration
            # names one. Read off the raw narration rather than the cleaned one:
            # cleaning strips punctuation that the amount pattern depends on.
            # A row with no parseable FX leg keeps its NULLs and displays in INR,
            # which is what it is.
            fx_leg = parse_fx_leg(raw_narration or clean_narration or "",
                                  booked_minor=debit or credit)
            if fx_leg is not None:
                txn.original_currency = fx_leg.currency
                txn.original_amount_minor = fx_leg.amount_minor
                txn.fx_rate = derive_rate(fx_leg, debit or credit)

            rows.append(txn)
            # Keep each row paired with the source dict it came from. Indexing back
            # into processed_txns by position is wrong because rows above are skipped
            # (the `continue` for zero-value rows), which shifts every later index and
            # attaches predictions to the wrong transaction.
            row_sources.append(t)

        if rows:
            db.add_all(rows)
            db.flush()
            # Store one Prediction per transaction carrying the FULL provenance:
            # how the category was decided, what each half of the hybrid proposed,
            # the top-3 alternatives, the human-readable reason, and whether it
            # needs review. Without requires_review the review queue stays empty on
            # real ingested data no matter what the classifier decided.
            for txn_obj, result in zip(rows, hybrid_results):
                db.add(Prediction(
                    id=uuid.uuid4(),
                    transaction_id=txn_obj.id,
                    category_id=txn_obj.category_id,
                    predicted_category=result.category,
                    confidence=result.classification_confidence,
                    rule_used=result.classification_rule,
                    model_version="v1.0.0",
                    classification_method=result.classification_method,
                    model_name=result.model_name,
                    model_confidence=result.model_confidence,
                    rule_score=result.rule_score,
                    rule_category=result.rule_category,
                    ml_category=result.ml_category,
                    top_3=[[c, round(p, 4)] for c, p in result.top_3],
                    explanation=result.explanation,
                    requires_review=result.requires_review,
                ))
            db.flush()


            if parsed_account_id:
                try:
                    from app.services.deduplication_engine import DeduplicationEngine
                    dedup = DeduplicationEngine(db=db, user_id=parsed_user_id, account_id=parsed_account_id)
                    dedup.run_deduplication()
                except Exception as e:
                    logger.warning(f"Automatic deduplication failed after store_transactions: {e}")
        return rows


def resolve_account_id(db: Session, user_id: Any, processed_txns: List[Dict[str, Any]]) -> Optional[Any]:
    """Best-effort resolution of the account a parsed statement belongs to.

    Only returns an account when the choice is unambiguous. Previously this took
    whichever account the database happened to return first, including
    soft-deleted ones, so a user holding several accounts could have an ICICI
    statement filed against their HDFC account. Guessing wrong misattributes real
    money; returning None leaves the transactions unbound for explicit mapping,
    which is recoverable.
    """
    from app.models.account import Account
    parsed_user_id = uuid.UUID(str(user_id)) if isinstance(user_id, str) else user_id

    active_accounts = db.query(Account).filter(
        Account.user_id == parsed_user_id,
        Account.deleted_at == None,
    ).order_by(Account.created_at.asc()).all()

    if len(active_accounts) == 1:
        return active_accounts[0].id

    if not active_accounts:
        logger.warning(
            f"[Account Resolution] User '{parsed_user_id}' has no active bank account; "
            "transactions will be stored without an account binding."
        )
        return None

    logger.warning(
        f"[Account Resolution] User '{parsed_user_id}' has {len(active_accounts)} active accounts "
        "and the statement carries no account hint; refusing to guess. "
        "Pass account_id explicitly to bind these transactions."
    )
    return None


def store_transactions(
    db: Session,
    parsed_user_id: Any,
    parsed_account_id: Optional[Any],
    parsed_entity_id: Optional[Any],
    parsed_statement_id: Optional[Any],
    source_channel: str,
    processed_txns: List[Dict[str, Any]]
) -> Dict[str, Any]:
    txns = TransactionStorageService().store_transactions(
        db=db,
        processed_txns=processed_txns,
        user_id=parsed_user_id,
        account_id=parsed_account_id,
        statement_id=parsed_statement_id,
        entity_id=parsed_entity_id,
        source_channel=source_channel
    )
    return {"persisted_count": len(txns), "transactions": txns}


