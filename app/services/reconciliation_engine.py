import re
import logging
import difflib
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Tuple, Optional, Set
from uuid import UUID
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_, Integer

from app.models.reconciliation import (
    ReconciliationRun, ReconciliationMatch, ReconciliationMatchLine,
    ReconciliationItem, BookEntry, ImportBatch, ReconciliationStatusEnum,
    RunVerdictEnum, MatchTierEnum, MatchStatusEnum, BRSSideEnum, DirectionEnum
)
from app.models.transaction import Transaction
from app.models.statement import Statement
from app.models.duplicate_match import DuplicateMatch, MatchStatus

logger = logging.getLogger(__name__)

STOPWORDS = {
    "NEFT", "UPI", "IMPS", "RTGS", "TRANSFER", "PAYMENT", "DISBURSEMENT",
    "CREDIT", "DEBIT", "SELF", "FOR", "PVT", "LTD", "RESILIENT", "INNOVATIONS",
    "EBANK", "COASTAL", "TO", "OWN", "ACCOUNT", "FEE", "FY", "LOAN", "RECOVERY",
    "FINAL", "STUDIO", "TEST", "DUPLICATE", "BY", "WITH", "FROM", "AND", "OR",
    "BHARATPE", "PAYOUTS", "PAYOUT", "YES", "BANK", "HDFC", "ICICI", "SBI",
    "AXIS", "KOTAK", "ONLINE", "INF", "CORP", "CHG", "CHARGE", "CHARGES"
}


def extract_reference_tokens(text: Optional[str]) -> Set[str]:
    """Extract candidate UTR numbers, reference codes, numeric sequences, and key tokens."""
    if not text:
        return set()
    s = str(text).strip().upper()
    clean_text = re.sub(r"[^A-Z0-9\/\-]", " ", s)
    tokens = clean_text.split()

    result = set()
    for tok in tokens:
        tok_clean = tok.strip("-/").upper()
        if not tok_clean:
            continue

        utr_m = re.search(r"([A-Z]{2,6}\d{8,14})", tok_clean)
        if utr_m:
            result.add(utr_m.group(1))

        digits_only = re.sub(r"[^0-9]", "", tok_clean).lstrip("0")
        if len(digits_only) >= 5:
            result.add(digits_only)

        alnum = re.sub(r"[^A-Z0-9]", "", tok_clean)
        if alnum and len(alnum) >= 4 and alnum not in STOPWORDS:
            result.add(alnum)

    return result


def compare_references(refs1: Set[str], refs2: Set[str]) -> Tuple[bool, float]:
    if not refs1 or not refs2:
        return False, 0.0
    common = refs1.intersection(refs2)
    if common:
        return True, 1.0
    return False, 0.0


def calculate_text_similarity(str1: Optional[str], str2: Optional[str]) -> float:
    if not str1 or not str2:
        return 0.0
    s1 = re.sub(r"[^A-Z0-9]", "", str(str1).upper())
    s2 = re.sub(r"[^A-Z0-9]", "", str(str2).upper())
    if not s1 or not s2:
        return 0.0
    return difflib.SequenceMatcher(None, s1, s2).ratio()


def normalize_ref(ref: Optional[str]) -> str:
    """Uppercase, strip non-alphanumeric, trim leading zeros for cheque numbers."""
    if not ref:
        return ""
    s = str(ref).strip().upper()
    m = re.search(r"(\b[A-Z]{2,6}\d{9,14}\b|\b\d{6,10}\b)", s)
    if m:
        s = m.group(1)
    cleaned = re.sub(r"[^A-Z0-9]", "", s)
    if cleaned.isdigit():
        cleaned = cleaned.lstrip("0")
    return cleaned


class ReconciliationMatchingEngine:
    """
    Tiered Reconciliation Matching Engine.
    Strictly enforces Amount + Direction constraints and Canonical Single-Bucket Mutually Exclusive Rules.
    """

    def __init__(self, db: Session, user_id: UUID, account_id: UUID, period_from: Any, period_to: Any):
        self.db = db
        self.user_id = user_id
        self.account_id = account_id
        if isinstance(period_from, str):
            self.period_from = datetime.strptime(period_from, "%Y-%m-%d").date()
        else:
            self.period_from = period_from

        if isinstance(period_to, str):
            self.period_to = datetime.strptime(period_to, "%Y-%m-%d").date()
        else:
            self.period_to = period_to

    def check_preflight_blockers(self, force: bool = False) -> Tuple[bool, Optional[str]]:
        """Verify statements are reconciled and no duplicate matches are pending review."""
        unrec_stmts = self.db.query(Statement).filter(
            Statement.user_id == self.user_id,
            Statement.account_id == self.account_id,
            Statement.reconciled == False
        ).first()

        if unrec_stmts and not force:
            return False, "Unreconciled statements present covering this period. Resolve or pass force=True."

        pending_dups = self.db.query(DuplicateMatch).filter(
            DuplicateMatch.user_id == self.user_id,
            DuplicateMatch.status == MatchStatus.PENDING_REVIEW.value
        ).join(
            Transaction, Transaction.id == DuplicateMatch.duplicate_txn_id
        ).filter(
            Transaction.account_id == self.account_id
        ).first()

        if pending_dups and not force:
            return False, "Duplicate matches pending review exist for this account. Resolve or pass force=True."

        return True, None

    def execute_run(self, import_batch_id: Optional[UUID] = None, force: bool = False) -> ReconciliationRun:
        """Execute full matching run idempotently."""
        can_run, reason = self.check_preflight_blockers(force=force)
        if not can_run:
            raise ValueError(reason)

        previous_run = self.db.query(ReconciliationRun).filter(
            ReconciliationRun.user_id == self.user_id,
            ReconciliationRun.account_id == self.account_id,
            ReconciliationRun.period_from == self.period_from,
            ReconciliationRun.period_to == self.period_to
        ).order_by(ReconciliationRun.version.desc()).first()

        self.run_version = (previous_run.version + 1) if (previous_run and previous_run.version) else 1
        self.supersedes_run_id = previous_run.id if previous_run else None

        # Scope strictly to selected account (NO NULL account_id fallback)
        bank_txns = self.db.query(Transaction).filter(
            Transaction.user_id == self.user_id,
            Transaction.account_id == self.account_id,
            Transaction.txn_date >= self.period_from,
            Transaction.txn_date <= self.period_to,
            Transaction.superseded_by_id == None
        ).all()

        import_batch = None
        if import_batch_id:
            import_batch = self.db.query(ImportBatch).filter(
                ImportBatch.id == import_batch_id,
                ImportBatch.account_id == self.account_id
            ).first()

        if not import_batch:
            import_batch = self.db.query(ImportBatch).filter(
                ImportBatch.user_id == self.user_id,
                ImportBatch.account_id == self.account_id
            ).order_by(ImportBatch.imported_at.desc()).first()

        resolved_batch_id = import_batch.id if import_batch else import_batch_id

        # Scope BookEntry rows strictly to resolved_batch_id or self.account_id
        # In both branches: also apply date filtering using the user's period window.
        # A small look-back tolerance (3 days before period_from) is included so that
        # date-mismatch matching near the period boundary still works (e.g. a ledger
        # entry dated 28-Mar can match a bank entry dated 29-Mar when period is Mar).
        date_from_tolerance = self.period_from - timedelta(days=3)

        if resolved_batch_id is not None:
            book_entries_query = self.db.query(BookEntry).filter(
                BookEntry.user_id == self.user_id,
                BookEntry.import_batch_id == resolved_batch_id,
                BookEntry.entry_date >= date_from_tolerance,   # ← filter by user period
                BookEntry.entry_date <= self.period_to,        # ← filter by user period
                BookEntry.reconciliation_status != ReconciliationStatusEnum.WRITTEN_OFF.value,
            )
        else:
            book_entries_query = self.db.query(BookEntry).filter(
                BookEntry.user_id == self.user_id,
                BookEntry.account_id == self.account_id,
                BookEntry.entry_date >= date_from_tolerance,
                BookEntry.entry_date <= self.period_to,
                BookEntry.reconciliation_status != ReconciliationStatusEnum.WRITTEN_OFF.value,
            )

        book_entries_raw = book_entries_query.all()

        seen_book_keys = set()
        book_entries = []
        for b in book_entries_raw:
            dedup_key = (b.instrument_no, b.voucher_no, b.entry_date, b.money_in_paise, b.money_out_paise)
            if dedup_key not in seen_book_keys:
                seen_book_keys.add(dedup_key)
                book_entries.append(b)

        # Guard: if an import batch was explicitly requested and contains book entries in DB,
        # but NONE fall within the selected period, abort with a clear error.
        if not book_entries and import_batch_id is not None:
            batch_has_rows = self.db.query(BookEntry).filter(
                BookEntry.import_batch_id == import_batch_id
            ).first() is not None

            if batch_has_rows:
                raise ValueError(
                    f"No ledger transactions found within the selected reconciliation period "
                    f"({self.period_from} to {self.period_to}). "
                    f"Please check the uploaded ledger file covers this date range, "
                    f"or adjust the period dates."
                )

        has_explicit_book_opening = bool(import_batch and import_batch.book_opening_paise is not None and import_batch.book_opening_paise != 0)
        book_opening_paise = import_batch.book_opening_paise if has_explicit_book_opening else 0

        # Always compute closing balance strictly from IN-PERIOD filtered entries (period_from <= entry_date <= period_to).
        # Any look-back tolerance entries (entry_date < period_from) must NOT be included in period accounting totals.
        in_period_entries = [b for b in book_entries if self.period_from <= b.entry_date <= self.period_to]
        tot_in = sum(b.money_in_paise for b in in_period_entries)
        tot_out = sum(b.money_out_paise for b in in_period_entries)
        net_book_movement = tot_in - tot_out

        # If no explicit book opening balance was supplied, determine bank opening balance immediately preceding period_from
        bank_opening_paise = 0
        if not has_explicit_book_opening:
            prev_bank_txn = self.db.query(Transaction).filter(
                Transaction.user_id == self.user_id,
                Transaction.account_id == self.account_id,
                Transaction.txn_date < self.period_from,
                Transaction.superseded_by_id == None,
                Transaction.balance_paise != None
            ).order_by(
                Transaction.txn_date.desc(),
                Transaction.row_index.desc().nullslast(),
                Transaction.created_at.desc()
            ).first()

            if prev_bank_txn and prev_bank_txn.balance_paise is not None:
                try:
                    bank_opening_paise = int(prev_bank_txn.balance_paise)
                except Exception:
                    bank_opening_paise = 0
            else:
                stmt_prev = self.db.query(Statement).filter(
                    Statement.user_id == self.user_id,
                    Statement.account_id == self.account_id,
                    Statement.period_to < self.period_from
                ).order_by(Statement.period_to.desc()).first()
                if stmt_prev and stmt_prev.closing_balance_paise is not None:
                    bank_opening_paise = stmt_prev.closing_balance_paise
                else:
                    # Fallback: derive opening balance from earliest in-period transaction running balance
                    first_in_period_txn = self.db.query(Transaction).filter(
                        Transaction.user_id == self.user_id,
                        Transaction.account_id == self.account_id,
                        Transaction.txn_date >= self.period_from,
                        Transaction.txn_date <= self.period_to,
                        Transaction.superseded_by_id == None,
                        Transaction.balance_paise != None
                    ).order_by(
                        Transaction.txn_date.asc(),
                        Transaction.row_index.asc().nullslast(),
                        Transaction.created_at.asc()
                    ).first()

                    if first_in_period_txn and first_in_period_txn.balance_paise is not None:
                        first_bal = int(first_in_period_txn.balance_paise)
                        if first_in_period_txn.credit_paise and first_in_period_txn.credit_paise > 0:
                            amt = int(first_in_period_txn.credit_paise)
                            bank_opening_paise = first_bal - amt
                        else:
                            amt = int(first_in_period_txn.debit_paise or 0)
                            bank_opening_paise = first_bal + amt

        book_closing_paise = (book_opening_paise if has_explicit_book_opening else bank_opening_paise) + net_book_movement


        # Bank closing balance must correspond strictly to period_to.
        # Check for the last bank transaction on or before period_to with a valid balance_paise.
        last_bank_txn = self.db.query(Transaction).filter(
            Transaction.user_id == self.user_id,
            Transaction.account_id == self.account_id,
            Transaction.txn_date <= self.period_to,
            Transaction.superseded_by_id == None,
            Transaction.balance_paise != None
        ).order_by(
            Transaction.txn_date.desc(),
            Transaction.row_index.desc().nullslast(),
            Transaction.created_at.desc()
        ).first()

        if last_bank_txn and last_bank_txn.balance_paise is not None:
            try:
                bank_closing_paise = int(last_bank_txn.balance_paise)
            except Exception:
                bank_closing_paise = None
        else:
            # Fallback to statement object only if statement.period_to <= period_to
            stmt_obj = self.db.query(Statement).filter(
                Statement.user_id == self.user_id,
                Statement.account_id == self.account_id,
                Statement.period_to <= self.period_to
            ).order_by(Statement.period_to.desc(), Statement.uploaded_at.desc()).first()

            if stmt_obj and stmt_obj.closing_balance_paise is not None:
                bank_closing_paise = stmt_obj.closing_balance_paise
            else:
                bank_closing_paise = None

        run = ReconciliationRun(
            user_id=self.user_id,
            account_id=self.account_id,
            import_batch_id=import_batch_id,
            period_from=self.period_from,
            period_to=self.period_to,
            book_opening_paise=(book_opening_paise if has_explicit_book_opening else bank_opening_paise),
            book_closing_paise=book_closing_paise,
            bank_closing_paise=bank_closing_paise,
            forced=force,
            engine_version="v2.0",
            status="processing",
            verdict=RunVerdictEnum.UNRECONCILED.value,
            residual_paise=0,
            version=getattr(self, "run_version", 1),
            supersedes_run_id=getattr(self, "supersedes_run_id", None)
        )
        run.opening_balance_paise = (book_opening_paise if has_explicit_book_opening else bank_opening_paise)
        run.net_movement_paise = net_book_movement
        self.db.add(run)
        self.db.flush()

        debug_log = []

        matched_book_ids = set()
        matched_bank_ids = set()
        consumed_bank_ids = set()
        duplicate_book_ids = set()
        pending_review_matches_count = 0

        book_refs_map = {}
        for b in book_entries:
            text = f"{b.instrument_no or ''} {b.voucher_no or ''} {b.narration or ''} {b.party_name or ''}"
            book_refs_map[b.id] = extract_reference_tokens(text)

        bank_refs_map = {}
        for t in bank_txns:
            text = f"{t.reference_no or ''} {t.narration_raw or ''} {t.narration_clean or ''} {t.counterparty or ''}"
            bank_refs_map[t.id] = extract_reference_tokens(text)

        # Priority 1: Exact Amount + Same Direction + Same/Normalized Reference (EXACT_MATCH or DATE_MISMATCH)
        for b in book_entries:
            if b.id in matched_book_ids:
                continue
            is_credit = b.money_in_paise > 0
            book_amt = b.money_in_paise if is_credit else b.money_out_paise
            b_refs = book_refs_map[b.id]

            for t in bank_txns:
                if t.id in matched_bank_ids:
                    continue
                t_is_credit = bool(t.credit_paise and t.credit_paise > 0)
                if is_credit != t_is_credit:
                    continue
                t_amt = int(t.credit_paise) if t_is_credit else int(t.debit_paise or 0)
                if book_amt != t_amt:
                    continue

                t_refs = bank_refs_map[t.id]
                has_ref, _ = compare_references(b_refs, t_refs)
                if has_ref:
                    is_date_mismatch = (b.entry_date != t.txn_date)
                    status_str = "DATE_MISMATCH" if is_date_mismatch else "EXACT_MATCH"
                    class_str = "MATCHED_WITH_DATE_DIFFERENCE" if is_date_mismatch else "MATCHED"
                    reason_str = f"Date mismatch ({t.txn_date} vs {b.entry_date}) with matching reference and amount" if is_date_mismatch else "Exact reference, direction and amount match"

                    match = ReconciliationMatch(
                        run_id=run.id,
                        user_id=self.user_id,
                        tier=MatchTierEnum.TIER_1.value,
                        confidence=0.95 if is_date_mismatch else 1.0,
                        status=MatchStatusEnum.AUTO_MATCHED.value,
                        reason=reason_str
                    )
                    self.db.add(match)
                    self.db.flush()
                    self.db.add(ReconciliationMatchLine(match_id=match.id, book_entry_id=b.id))
                    self.db.add(ReconciliationMatchLine(match_id=match.id, bank_txn_id=t.id))
                    matched_book_ids.add(b.id)
                    matched_bank_ids.add(t.id)
                    consumed_bank_ids.add(t.id)
                    b.reconciliation_status = ReconciliationStatusEnum.MATCHED.value

                    debug_log.append({
                        "bank_transaction_id": str(t.id),
                        "ledger_transaction_id": str(b.id),
                        "bank_date": str(t.txn_date),
                        "ledger_date": str(b.entry_date),
                        "bank_amount": t_amt / 100.0,
                        "ledger_amount": book_amt / 100.0,
                        "bank_direction": "CREDIT" if t_is_credit else "DEBIT",
                        "ledger_direction": "CREDIT" if is_credit else "DEBIT",
                        "reference_similarity": 1.0,
                        "description_similarity": calculate_text_similarity(t.narration_raw, b.narration),
                        "match_score": 0.95 if is_date_mismatch else 1.0,
                        "match_status": status_str,
                        "classification": class_str,
                        "reason": reason_str
                    })
                    break

        # Priority 2: Exact Amount + Same Direction + Same Date (EXACT_MATCH)
        for b in book_entries:
            if b.id in matched_book_ids:
                continue
            is_credit = b.money_in_paise > 0
            book_amt = b.money_in_paise if is_credit else b.money_out_paise

            for t in bank_txns:
                if t.id in matched_bank_ids:
                    continue
                t_is_credit = bool(t.credit_paise and t.credit_paise > 0)
                if is_credit != t_is_credit:
                    continue
                t_amt = int(t.credit_paise) if t_is_credit else int(t.debit_paise or 0)
                if book_amt != t_amt:
                    continue
                if b.entry_date != t.txn_date:
                    continue

                match = ReconciliationMatch(
                    run_id=run.id,
                    user_id=self.user_id,
                    tier=MatchTierEnum.TIER_2.value,
                    confidence=0.98,
                    status=MatchStatusEnum.AUTO_MATCHED.value,
                    reason="Exact date, direction and amount match"
                )
                self.db.add(match)
                self.db.flush()
                self.db.add(ReconciliationMatchLine(match_id=match.id, book_entry_id=b.id))
                self.db.add(ReconciliationMatchLine(match_id=match.id, bank_txn_id=t.id))
                matched_book_ids.add(b.id)
                matched_bank_ids.add(t.id)
                consumed_bank_ids.add(t.id)
                b.reconciliation_status = ReconciliationStatusEnum.MATCHED.value

                debug_log.append({
                    "bank_transaction_id": str(t.id),
                    "ledger_transaction_id": str(b.id),
                    "bank_date": str(t.txn_date),
                    "ledger_date": str(b.entry_date),
                    "bank_amount": t_amt / 100.0,
                    "ledger_amount": book_amt / 100.0,
                    "bank_direction": "CREDIT" if t_is_credit else "DEBIT",
                    "ledger_direction": "CREDIT" if is_credit else "DEBIT",
                    "reference_similarity": 0.5,
                    "description_similarity": calculate_text_similarity(t.narration_raw, b.narration),
                    "match_score": 0.98,
                    "match_status": "EXACT_MATCH",
                    "classification": "MATCHED",
                    "reason": "Priority 2: exact amount + direction + date"
                })
                break

        # Priority 3: Date Mismatch (Same Ref + Amount + Direction, Date Difference <= 15 days)
        for b in book_entries:
            if b.id in matched_book_ids:
                continue
            is_credit = b.money_in_paise > 0
            book_amt = b.money_in_paise if is_credit else b.money_out_paise
            b_refs = book_refs_map[b.id]

            for t in bank_txns:
                if t.id in matched_bank_ids:
                    continue
                t_is_credit = bool(t.credit_paise and t.credit_paise > 0)
                if is_credit != t_is_credit:
                    continue
                t_amt = int(t.credit_paise) if t_is_credit else int(t.debit_paise or 0)
                if book_amt != t_amt:
                    continue

                t_refs = bank_refs_map[t.id]
                has_ref, _ = compare_references(b_refs, t_refs)
                days_diff = abs((b.entry_date - t.txn_date).days)
                if has_ref and days_diff <= 15:
                    match = ReconciliationMatch(
                        run_id=run.id,
                        user_id=self.user_id,
                        tier=MatchTierEnum.TIER_2.value,
                        confidence=0.95,
                        status=MatchStatusEnum.AUTO_MATCHED.value,
                        reason=f"Date mismatch: reference & amount matched (bank {t.txn_date} vs book {b.entry_date})"
                    )
                    self.db.add(match)
                    self.db.flush()
                    self.db.add(ReconciliationMatchLine(match_id=match.id, book_entry_id=b.id))
                    self.db.add(ReconciliationMatchLine(match_id=match.id, bank_txn_id=t.id))
                    matched_book_ids.add(b.id)
                    matched_bank_ids.add(t.id)
                    consumed_bank_ids.add(t.id)
                    b.reconciliation_status = ReconciliationStatusEnum.MATCHED.value

                    debug_log.append({
                        "bank_transaction_id": str(t.id),
                        "ledger_transaction_id": str(b.id),
                        "bank_date": str(t.txn_date),
                        "ledger_date": str(b.entry_date),
                        "bank_amount": t_amt / 100.0,
                        "ledger_amount": book_amt / 100.0,
                        "bank_direction": "CREDIT" if t_is_credit else "DEBIT",
                        "ledger_direction": "CREDIT" if is_credit else "DEBIT",
                        "reference_similarity": 1.0,
                        "description_similarity": calculate_text_similarity(t.narration_raw, b.narration),
                        "match_score": 0.95,
                        "match_status": "DATE_MISMATCH",
                        "classification": "MATCHED_WITH_DATE_DIFFERENCE",
                        "reason": f"Priority 3: date mismatch ({t.txn_date} vs {b.entry_date}) with matching reference and amount"
                    })
                    break

        # Priority 4: 1:1 Heuristic Exact Amount Match (Same Direction, Date Window <= 15 days)
        for b in book_entries:
            if b.id in matched_book_ids:
                continue
            is_credit = b.money_in_paise > 0
            book_amt = b.money_in_paise if is_credit else b.money_out_paise

            candidates = []
            for t in bank_txns:
                if t.id in matched_bank_ids:
                    continue
                t_is_credit = bool(t.credit_paise and t.credit_paise > 0)
                if is_credit != t_is_credit:
                    continue
                t_amt = int(t.credit_paise) if t_is_credit else int(t.debit_paise or 0)
                if book_amt == t_amt:
                    days_diff = abs((b.entry_date - t.txn_date).days)
                    if days_diff <= 15:
                        candidates.append((days_diff, t))

            if len(candidates) == 1:
                _, t = candidates[0]
                match = ReconciliationMatch(
                    run_id=run.id,
                    user_id=self.user_id,
                    tier=MatchTierEnum.TIER_2.value,
                    confidence=0.90,
                    status=MatchStatusEnum.AUTO_MATCHED.value,
                    reason="1:1 Heuristic exact amount match within date window"
                )
                self.db.add(match)
                self.db.flush()
                self.db.add(ReconciliationMatchLine(match_id=match.id, book_entry_id=b.id))
                self.db.add(ReconciliationMatchLine(match_id=match.id, bank_txn_id=t.id))
                matched_book_ids.add(b.id)
                matched_bank_ids.add(t.id)
                consumed_bank_ids.add(t.id)
                b.reconciliation_status = ReconciliationStatusEnum.MATCHED.value

                debug_log.append({
                    "bank_transaction_id": str(t.id),
                    "ledger_transaction_id": str(b.id),
                    "bank_date": str(t.txn_date),
                    "ledger_date": str(b.entry_date),
                    "bank_amount": (int(t.credit_paise) if is_credit else int(t.debit_paise or 0)) / 100.0,
                    "ledger_amount": book_amt / 100.0,
                    "bank_direction": "CREDIT" if is_credit else "DEBIT",
                    "ledger_direction": "CREDIT" if is_credit else "DEBIT",
                    "reference_similarity": 0.0,
                    "description_similarity": calculate_text_similarity(t.narration_raw, b.narration),
                    "match_score": 0.90,
                    "match_status": "EXACT_MATCH",
                    "classification": "MATCHED",
                    "reason": "Priority 4: 1:1 heuristic amount match"
                })

        # Priority 5: Amount Mismatch (Requirement 3 & 6: Same Direction + Shared Ref or High Desc Sim, Different Amount)
        for b in book_entries:
            if b.id in matched_book_ids:
                continue
            is_credit = b.money_in_paise > 0
            book_amt = b.money_in_paise if is_credit else b.money_out_paise
            b_refs = book_refs_map[b.id]

            for t in bank_txns:
                if t.id in matched_bank_ids or t.id in consumed_bank_ids:
                    continue
                t_is_credit = bool(t.credit_paise and t.credit_paise > 0)
                if is_credit != t_is_credit:
                    continue
                t_amt = int(t.credit_paise) if t_is_credit else int(t.debit_paise or 0)
                if book_amt == t_amt:
                    continue

                t_refs = bank_refs_map[t.id]
                has_ref, _ = compare_references(b_refs, t_refs)
                desc_sim = calculate_text_similarity(t.narration_raw, b.narration)
                same_date = (b.entry_date == t.txn_date)

                if has_ref or (same_date and desc_sim >= 0.7):
                    match = ReconciliationMatch(
                        run_id=run.id,
                        user_id=self.user_id,
                        tier=MatchTierEnum.TIER_1.value,
                        confidence=0.80,
                        status=MatchStatusEnum.PENDING_REVIEW.value,
                        reason="amount_mismatch_on_reference"
                    )
                    self.db.add(match)
                    self.db.flush()
                    self.db.add(ReconciliationMatchLine(match_id=match.id, book_entry_id=b.id))
                    self.db.add(ReconciliationMatchLine(match_id=match.id, bank_txn_id=t.id))

                    matched_book_ids.add(b.id)
                    matched_bank_ids.add(t.id)
                    consumed_bank_ids.add(t.id)
                    pending_review_matches_count += 1

                    debug_log.append({
                        "bank_transaction_id": str(t.id),
                        "ledger_transaction_id": str(b.id),
                        "bank_date": str(t.txn_date),
                        "ledger_date": str(b.entry_date),
                        "bank_amount": t_amt / 100.0,
                        "ledger_amount": book_amt / 100.0,
                        "bank_direction": "CREDIT" if t_is_credit else "DEBIT",
                        "ledger_direction": "CREDIT" if is_credit else "DEBIT",
                        "reference_similarity": 1.0 if has_ref else desc_sim,
                        "description_similarity": desc_sim,
                        "match_score": 0.80,
                        "match_status": "AMOUNT_MISMATCH",
                        "classification": "AMOUNT_MISMATCH",
                        "reason": f"Priority 5: amount mismatch (bank={t_amt/100:.2f} vs ledger={book_amt/100:.2f})"
                    })
                    break

        # Priority 6: Tier 3 Group Match (1:N Subset Sum)
        remaining_books = [b for b in book_entries if b.id not in matched_book_ids]
        for t in bank_txns:
            if t.id in consumed_bank_ids:
                continue
            t_is_credit = bool(t.credit_paise and t.credit_paise > 0)
            t_amt = int(t.credit_paise) if t_is_credit else int(t.debit_paise or 0)

            eligible_books = [
                b for b in remaining_books
                if ((b.money_in_paise > 0) if t_is_credit else (b.money_out_paise > 0))
                and abs((b.entry_date - t.txn_date).days) <= 15
            ]

            from itertools import combinations
            found_subset = None
            for r in range(2, min(6, len(eligible_books) + 1)):
                for combo in combinations(eligible_books, r):
                    combo_sum = sum(b.money_in_paise if t_is_credit else b.money_out_paise for b in combo)
                    if combo_sum == t_amt:
                        found_subset = combo
                        break
                if found_subset:
                    break

            if found_subset:
                match = ReconciliationMatch(
                    run_id=run.id,
                    user_id=self.user_id,
                    tier=MatchTierEnum.TIER_3_GROUP.value,
                    confidence=0.85,
                    status=MatchStatusEnum.PENDING_REVIEW.value,
                    reason="Tier 3 group match (1:N exact sum match within date window)"
                )
                self.db.add(match)
                self.db.flush()
                self.db.add(ReconciliationMatchLine(match_id=match.id, bank_txn_id=t.id))
                consumed_bank_ids.add(t.id)
                matched_bank_ids.add(t.id)
                pending_review_matches_count += 1
                for b in found_subset:
                    self.db.add(ReconciliationMatchLine(match_id=match.id, book_entry_id=b.id))
                    matched_book_ids.add(b.id)

                remaining_books = [b for b in remaining_books if b.id not in matched_book_ids]

        # Priority 7: Duplicate Ledger Row Detection (STRICT MANDATORY RULES)
        # Rule 1: Bank transaction must ALREADY be matched in matched_bank_ids.
        # Rule 2: Direction MUST match.
        # Rule 3: Amount MUST match exactly (duplicate.amount == canonical_match.amount).
        # Rule 4: Reference OR date identity must establish genuine equivalence.
        for b in book_entries:
            if b.id in matched_book_ids or b.id in duplicate_book_ids:
                continue
            is_credit = b.money_in_paise > 0
            book_amt = b.money_in_paise if is_credit else b.money_out_paise
            b_refs = book_refs_map[b.id]

            for t in bank_txns:
                if t.id not in matched_bank_ids:
                    continue
                t_is_credit = bool(t.credit_paise and t.credit_paise > 0)
                if is_credit != t_is_credit:
                    continue
                t_amt = int(t.credit_paise) if t_is_credit else int(t.debit_paise or 0)
                
                # MANDATORY RULE 3: Amount MUST match exactly!
                if book_amt != t_amt:
                    continue

                t_refs = bank_refs_map[t.id]
                has_ref, _ = compare_references(b_refs, t_refs)

                if has_ref or (b.entry_date == t.txn_date):
                    duplicate_book_ids.add(b.id)
                    debug_log.append({
                        "bank_transaction_id": str(t.id),
                        "ledger_transaction_id": str(b.id),
                        "bank_date": str(t.txn_date),
                        "ledger_date": str(b.entry_date),
                        "bank_amount": t_amt / 100.0,
                        "ledger_amount": book_amt / 100.0,
                        "bank_direction": "CREDIT" if t_is_credit else "DEBIT",
                        "ledger_direction": "CREDIT" if is_credit else "DEBIT",
                        "reference_similarity": 1.0 if has_ref else 0.5,
                        "description_similarity": calculate_text_similarity(t.narration_raw, b.narration),
                        "match_score": 0.0,
                        "match_status": "DUPLICATE",
                        "classification": "DUPLICATE_LEDGER_ROW",
                        "reason": "Priority 7: duplicate ledger row for already-matched bank transaction"
                    })
                    break

        run.debug_log = debug_log
        run.debug_log_json = debug_log
        run.pending_review_count = pending_review_matches_count

        logger.info(f"[RECON ENGINE] Completed run {run.id}: {len(matched_book_ids)} matched book entries, {len(duplicate_book_ids)} duplicates, {pending_review_matches_count} pending review.")

        return self._generate_brs_results(
            run, import_batch_id, book_entries, bank_txns,
            matched_book_ids, matched_bank_ids, duplicate_book_ids,
            bank_closing_paise_raw=bank_closing_paise
        )

    def _generate_brs_results(
        self,
        run: ReconciliationRun,
        import_batch_id: Optional[UUID],
        book_entries: List[BookEntry],
        bank_txns: List[Transaction],
        matched_book_ids: set,
        matched_bank_ids: set,
        duplicate_book_ids: set,
        bank_closing_paise_raw: Optional[int] = None,
    ) -> ReconciliationRun:
        book_closing_paise = run.book_closing_paise
        items_to_persist = []

        # 1. Outstanding Book Entries (UNMATCHED & NOT DUPLICATE & STRICTLY IN-PERIOD)
        unmatched_books = [
            b for b in book_entries
            if b.id not in matched_book_ids
            and b.id not in duplicate_book_ids
            and self.period_from <= b.entry_date <= self.period_to
        ]

        for b in unmatched_books:
            is_carried = b.entry_date < self.period_from
            age = (self.period_to - b.entry_date).days
            flag = False
            flag_reason = None

            if b.money_out_paise > 0:
                cat = "unpresented_cheque"
                direction = DirectionEnum.ADD.value
                amount = b.money_out_paise
                if age > 90:
                    flag = True
                    flag_reason = "stale_cheque_write_back_required"
            else:
                cat = "uncleared_deposit"
                direction = DirectionEnum.SUBTRACT.value
                amount = b.money_in_paise
                if age > 7:
                    flag = True
                    flag_reason = "deposit_in_transit_delayed"

            item = ReconciliationItem(
                run_id=run.id,
                user_id=self.user_id,
                side=BRSSideEnum.BOOK.value,
                brs_category=cat,
                amount_paise=amount,
                direction=direction,
                age_days=age,
                exception_flag=flag,
                exception_reason=flag_reason,
                book_entry_id=b.id
            )
            self.db.add(item)
            items_to_persist.append((item, is_carried, BRSSideEnum.BOOK))

        # 1b. Duplicate Book Entries — extra ledger rows that were matched against an
        #     already-claimed bank transaction.  They inflate the books, so the bridge
        #     must carry the inverse sign of the entry:
        #       duplicate credit  (money_in > 0)  → SUBTRACT  (books too high)
        #       duplicate debit   (money_out > 0)  → ADD       (books show more paid)
        duplicate_books = [b for b in book_entries if b.id in duplicate_book_ids]
        for b in duplicate_books:
            age = (self.period_to - b.entry_date).days
            if b.money_in_paise > 0:
                dup_cat = "duplicate_ledger_credit"
                dup_direction = DirectionEnum.SUBTRACT.value
                dup_amount = b.money_in_paise
            else:
                dup_cat = "duplicate_ledger_debit"
                dup_direction = DirectionEnum.ADD.value
                dup_amount = b.money_out_paise

            dup_item = ReconciliationItem(
                run_id=run.id,
                user_id=self.user_id,
                side=BRSSideEnum.BOOK.value,
                brs_category=dup_cat,
                amount_paise=dup_amount,
                direction=dup_direction,
                age_days=age,
                exception_flag=True,
                exception_reason="duplicate_ledger_row",
                book_entry_id=b.id
            )
            self.db.add(dup_item)
            items_to_persist.append((dup_item, False, BRSSideEnum.BOOK))

        # 2. Unmatched Bank Transactions
        unmatched_banks = [t for t in bank_txns if t.id not in matched_bank_ids]

        for bank_tx in unmatched_banks:
            age = (self.period_to - bank_tx.txn_date).days
            narration = ((bank_tx.narration_raw or "") + " " + (bank_tx.narration_clean or "")).upper()
            bank_debit = int(bank_tx.debit_paise) if bank_tx.debit_paise else 0
            bank_credit = int(bank_tx.credit_paise) if bank_tx.credit_paise else 0

            flag = False
            flag_reason = None

            if bank_debit > 0:
                amount = bank_debit
                direction = DirectionEnum.SUBTRACT.value
                if any(kw in narration for kw in [
                    "CHARGES", "FEE", "TAX", "GST",
                    " CHG", "CHRG", "LEVY", "PENALTY",
                    "COMMISSION", "MAINT", "SMS CHG", "SMSCHG",
                    "DEMAT", "FOLIO", "ANNUAL", "LOCKER",
                ]):
                    cat = "bank_charge"
                    flag = True
                    flag_reason = "journal_entry_required"
                elif any(kw in narration for kw in ["EMI", "LOAN", "ACH"]):
                    cat = "standing_instruction"
                elif any(kw in narration for kw in ["RETURN", "RTN", "DISHONOUR", "CHQ RET", "INSUFF"]):
                    cat = "dishonoured_cheque"
                else:
                    cat = "UNMATCHED_BANK_TRANSACTION"
            else:
                amount = bank_credit
                direction = DirectionEnum.ADD.value
                if any(kw in narration for kw in ["INT.PD", "INTEREST CREDIT", "CR INT", "INT.CR", "1NT.CR", "INT.CR:"]):
                    cat = "interest_credit"
                    flag = True
                    flag_reason = "journal_entry_required"
                elif any(kw in narration for kw in ["NEFT", "UPI", "IMPS", "RTGS"]):
                    cat = "direct_credit"
                else:
                    cat = "UNMATCHED_BANK_TRANSACTION"

            item = ReconciliationItem(
                run_id=run.id,
                user_id=self.user_id,
                side=BRSSideEnum.BANK.value,
                brs_category=cat,
                amount_paise=amount,
                direction=direction,
                age_days=age,
                exception_flag=flag,
                exception_reason=flag_reason,
                bank_txn_id=bank_tx.id
            )
            self.db.add(item)
            items_to_persist.append((item, False, BRSSideEnum.BANK))

        self.db.flush()

        book_side_items = [
            item for item, _carried, side in items_to_persist
            if side == BRSSideEnum.BOOK
        ]
        bank_side_items = [
            item for item, _carried, side in items_to_persist
            if side == BRSSideEnum.BANK
        ]

        bridge_add_paise = sum(
            item.amount_paise for item in book_side_items
            if item.direction in (DirectionEnum.ADD.value, DirectionEnum.ADD)
        )
        bridge_sub_paise = sum(
            item.amount_paise for item in book_side_items
            if item.direction in (DirectionEnum.SUBTRACT.value, DirectionEnum.SUBTRACT)
        )

        bank_add_paise = sum(
            item.amount_paise for item in bank_side_items
            if item.direction in (DirectionEnum.ADD.value, DirectionEnum.ADD)
        )
        bank_sub_paise = sum(
            item.amount_paise for item in bank_side_items
            if item.direction in (DirectionEnum.SUBTRACT.value, DirectionEnum.SUBTRACT)
        )

        # Computed bank position incorporates book-side timing/duplicate items as well as bank-only items
        computed_bank_closing = book_closing_paise + bridge_add_paise - bridge_sub_paise + bank_add_paise - bank_sub_paise
        run.computed_bank_closing_paise = computed_bank_closing

        if bank_closing_paise_raw is None:
            run.bank_closing_paise = computed_bank_closing
            run.residual_paise = 0
            has_exceptions = (
                len(book_side_items) > 0 or
                (run.pending_review_count or 0) > 0 or
                any(item.exception_flag for item in book_side_items)
            )
            run.verdict = (
                RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value
                if has_exceptions
                else RunVerdictEnum.RECONCILED_CLEAN.value
            )
            run.status = "completed_no_bank_statement"
            run.matched_count = len(matched_book_ids)
            run.unmatched_book_count = len(book_side_items)
            run.unmatched_bank_count = len(unmatched_banks)
            run.completed_at = datetime.utcnow()
            self.db.commit()
            self.db.refresh(run)
            return run

        run.bank_closing_paise = bank_closing_paise_raw
        run.residual_paise = bank_closing_paise_raw - computed_bank_closing

        book_item_net = sum(
            item.amount_paise if item.direction in (DirectionEnum.ADD.value, DirectionEnum.ADD)
            else -item.amount_paise
            for item in book_side_items
        )
        bank_item_net = sum(
            item.amount_paise if item.direction in (DirectionEnum.ADD.value, DirectionEnum.ADD)
            else -item.amount_paise
            for item in bank_side_items
        )
        assert run.computed_bank_closing_paise is not None, "Assertion Failed: computed_bank_closing_paise is None"
        assert run.book_closing_paise is not None, "Assertion Failed: book_closing_paise is None"
        assert run.bank_closing_paise is not None, "Assertion Failed: bank_closing_paise is None"
        assert run.residual_paise == (run.bank_closing_paise - run.computed_bank_closing_paise), "Assertion Failed: residual_paise formula mismatch"
        assert run.computed_bank_closing_paise == run.book_closing_paise + book_item_net + bank_item_net, (
            f"bridge mismatch: computed={run.computed_bank_closing_paise} "
            f"book={run.book_closing_paise} book_item_net={book_item_net} bank_item_net={bank_item_net}"
        )

        has_outstanding = (
            len(unmatched_banks) > 0 or
            len(book_side_items) > 0 or
            (run.pending_review_count or 0) > 0 or
            any(item.exception_flag for item in book_side_items + bank_side_items)
        )
        if run.residual_paise == 0:
            run.verdict = (
                RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value
                if has_outstanding
                else RunVerdictEnum.RECONCILED_CLEAN.value
            )
        else:
            run.verdict = RunVerdictEnum.UNRECONCILED.value

        run.matched_count = len(matched_book_ids)
        run.unmatched_book_count = len(book_side_items)
        run.unmatched_bank_count = len(unmatched_banks)
        run.status = "completed"
        run.completed_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(run)
        return run
