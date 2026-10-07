"""Bank Reconciliation Statement (BRS) engine — books ledger vs bank statement.

What one run does, for one bank account and one period:

1. Book OPENING balance (never guessed from the bank):
     typed by the user (run request or import screen)  -> "manual"
     else closing of the previous, contiguous run        -> "previous_run"
     else the ledger file's own "Opening Balance" row    -> "ledger_file"
     else stop with BookOpeningRequired so the UI can ask for it.
   Book CLOSING = opening + every ledger movement dated inside the period.

2. Pools of entries to pair:
     books : ledger entries dated inside the period (selected import batch)
             + outstanding book items carried from the previous run
     bank  : bank transactions dated inside the period
             + outstanding bank items carried from the previous run
   Nothing is de-duplicated or dropped: two identical ledger rows are two rows.

3. Matching (same direction always; earlier user decisions win):
     CONFIRMED pairs from earlier runs are re-applied; REJECTED pairs are never
     proposed again.
     A  auto     same amount + shared transaction ID (cheque/instrument no,
                 UTR, voucher no) within 7 days, or 90 days when the shared ID
                 is the ledger's cheque number
     B  auto     same amount, same date
     C  auto     same amount within 7 days, the only candidate on BOTH sides
     D  review   shared transaction ID, different amount -> paired, and the
                 difference becomes its own BRS line
     E  review   same amount within 30 days, linked only by a shared party word
                 or by nearest date — a suggestion, never an auto-match
     F  review   one bank entry = sum of 2-5 ledger entries, and one ledger
                 entry = sum of 2-5 bank entries (bounded search)
     Leftover ledger rows that repeat a matched row (same date, amount,
     direction) are listed as POSSIBLE duplicates, never removed.

4. Bridge:  computed bank closing = book closing
              + payments in books not yet in bank      (ADD)
              - receipts in books not yet in bank      (SUBTRACT)
              + credits in bank not in books           (ADD)
              - debits in bank not in books            (SUBTRACT)
              +/- amount differences of paired entries
            residual = bank closing (statement) - computed bank closing.
   Verdict: residual 0 and nothing outstanding -> reconciled_clean;
            residual 0 with items/reviews       -> reconciled_with_exceptions;
            residual != 0                        -> unreconciled.

Confirming or rejecting a suggested pair rebuilds the run's bridge in place
(rebuild_run), so totals and verdict always reflect the decisions made.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.duplicate_match import DuplicateMatch, MatchStatus
from app.models.reconciliation import (
    BookEntry, BRSSideEnum, DirectionEnum, ImportBatch, MatchStatusEnum,
    MatchTierEnum, ReconciliationItem, ReconciliationMatch,
    ReconciliationMatchLine, ReconciliationRun, ReconciliationStatusEnum,
    RunVerdictEnum,
)
from app.models.statement import Statement
from app.models.transaction import SourceType, Transaction

logger = logging.getLogger(__name__)

ENGINE_VERSION = "v3.0"
SUPPORTED_ENGINE_VERSIONS = ("v2.0", "v3.0")

AUTO_WINDOW_DAYS = 7          # electronic payments: auto-match only this close
CHEQUE_WINDOW_DAYS = 90       # cheques: validity period, matched on cheque number
SUGGEST_WINDOW_DAYS = 30      # review-only suggestions
GROUP_MAX_PARTS = 5
GROUP_CANDIDATES = 30         # nearest-by-date entries considered per group search
GROUP_NODE_BUDGET = 50_000    # hard cap on search steps per group search

TIER_ID = MatchTierEnum.TIER_1.value
TIER_AMOUNT = MatchTierEnum.TIER_2.value
TIER_GROUP = MatchTierEnum.TIER_3_GROUP.value
TIER_AMOUNT_DIFF = "amount_difference"
TIER_SUGGESTED = "suggested"

ACTIVE_MATCH_STATUSES = (
    MatchStatusEnum.AUTO_MATCHED.value,
    MatchStatusEnum.PENDING_REVIEW.value,
    MatchStatusEnum.CONFIRMED.value,
)


class BookOpeningRequired(ValueError):
    """No book opening balance is known for the period; the caller must supply one."""

    code = "BOOK_OPENING_REQUIRED"


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #

STOPWORDS = {
    "NEFT", "UPI", "IMPS", "RTGS", "TRANSFER", "PAYMENT", "DISBURSEMENT",
    "CREDIT", "DEBIT", "SELF", "FOR", "PVT", "LTD", "PRIVATE", "LIMITED",
    "EBANK", "TO", "OWN", "ACCOUNT", "FEE", "FY", "BY", "WITH", "FROM", "AND",
    "THE", "BANK", "ONLINE", "INF", "CORP", "CHG", "CHARGE", "CHARGES", "CHQ",
    "CHEQUE", "PAID", "RECEIVED", "RECEIPT", "PAYMENTS", "INWARD", "OUTWARD",
    "CLEARING", "CASH", "DEPOSIT", "WITHDRAWAL", "TRF", "TXN", "REF", "NO",
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
    "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER", "JAN", "FEB", "MAR", "APR",
    "JUN", "JUL", "AUG", "SEP", "SEPT", "OCT", "NOV", "DEC",
}

_DATE_FORMS = ("%d%m%Y", "%Y%m%d")


def _looks_like_date(digits: str) -> bool:
    # Only 8-digit forms: a 6-digit token is far more often a cheque number
    # (Indian cheques are 6 digits) than a ddmmyy date, and treating cheque
    # numbers as dates would throw away the strongest ID there is.
    if len(digits) != 8:
        return False
    for fmt in _DATE_FORMS:
        try:
            d = datetime.strptime(digits, fmt)
        except ValueError:
            continue
        if 2000 <= d.year <= 2099:
            return True
    return False


def strong_ids(fields: Iterable[Optional[str]], amount_paise: int = 0) -> Set[str]:
    """Transaction identifiers only: cheque/instrument numbers, UTRs, RRNs,
    voucher numbers. Words, names, dates and the entry's own amount are NOT
    identifiers — sharing them proves nothing about two entries being one.
    """
    out: Set[str] = set()
    rupees = {str(amount_paise // 100), str(amount_paise)} if amount_paise else set()
    for raw in fields:
        if not raw:
            continue
        for tok in re.split(r"[^A-Z0-9]+", str(raw).upper()):
            if not tok:
                continue
            digits = re.sub(r"\D", "", tok)
            if tok.isdigit():
                if len(tok) < 6 or _looks_like_date(tok) or tok.lstrip("0") in rupees:
                    continue
                out.add(tok.lstrip("0") or "0")
            elif len(digits) >= 6 and 10 <= len(tok) <= 22:
                out.add(tok)                       # UTR / alphanumeric reference
    return out


def _norm_id(value: Optional[str]) -> str:
    if not value:
        return ""
    s = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    return (s.lstrip("0") or "0") if s.isdigit() else s


def party_words(fields: Iterable[Optional[str]]) -> Set[str]:
    """Significant words (4+ letters, not banking boilerplate) — evidence for a
    review SUGGESTION only."""
    out: Set[str] = set()
    for raw in fields:
        if not raw:
            continue
        for tok in re.split(r"[^A-Z]+", str(raw).upper()):
            if len(tok) >= 4 and tok not in STOPWORDS:
                out.add(tok)
    return out


# Kept for callers/tests that import them.
def extract_reference_tokens(text: Optional[str]) -> Set[str]:
    return strong_ids([text])


def compare_references(refs1: Set[str], refs2: Set[str]) -> Tuple[bool, float]:
    if refs1 and refs2 and refs1 & refs2:
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
    return _norm_id(ref)


# --------------------------------------------------------------------------- #
# Uniform views over the two sides
# --------------------------------------------------------------------------- #

@dataclass
class _Entry:
    side: str                  # "book" | "bank"
    id: UUID
    obj: Any
    day: date
    credit: bool               # money in
    amount: int                # paise, > 0
    ids: Set[str]
    cheque_no: str             # book instrument no (normalised), "" if none
    words: Set[str]
    text: str
    order: Tuple

    @property
    def signed(self) -> int:
        return self.amount if self.credit else -self.amount


def _book_view(b: BookEntry) -> _Entry:
    credit = (b.money_in_paise or 0) > 0
    amount = int(b.money_in_paise if credit else b.money_out_paise)
    ids = strong_ids([b.narration], amount)
    for v in (b.instrument_no, b.voucher_no):
        n = _norm_id(v)
        if n and n not in ("NONE", "NAN") and len(n) >= 3:
            ids.add(n)
    return _Entry("book", b.id, b, b.entry_date, credit, amount, ids,
                  _norm_id(b.instrument_no) if b.instrument_no else "",
                  party_words([b.narration, b.party_name]),
                  f"{b.narration or ''} {b.party_name or ''}".strip(),
                  (b.entry_date, b.row_index or 0, str(b.id)))


def _bank_view(t: Transaction) -> _Entry:
    credit = bool(t.credit_paise and t.credit_paise > 0)
    amount = int(t.credit_paise if credit else (t.debit_paise or 0))
    ids = strong_ids([t.reference_no, t.narration_raw], amount)
    n = _norm_id(t.reference_no)
    if n and len(n) >= 3:
        ids.add(n)
    return _Entry("bank", t.id, t, t.txn_date, credit, amount, ids, "",
                  party_words([t.narration_raw, t.narration_clean, t.counterparty]),
                  f"{t.narration_raw or ''}".strip(),
                  (t.txn_date, t.row_index if t.row_index is not None else 0,
                   str(t.created_at or ""), str(t.id)))


@dataclass
class _Pair:
    books: List[_Entry]
    banks: List[_Entry]
    tier: str
    status: str
    confidence: float
    reason: str
    kind: str                 # for the debug log / counts
    prior_match_id: Optional[UUID] = None


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class ReconciliationMatchingEngine:

    def __init__(self, db: Session, user_id: UUID, account_id: UUID, period_from: Any, period_to: Any):
        self.db = db
        self.user_id = user_id
        self.account_id = account_id
        self.period_from = (datetime.strptime(period_from, "%Y-%m-%d").date()
                            if isinstance(period_from, str) else period_from)
        self.period_to = (datetime.strptime(period_to, "%Y-%m-%d").date()
                          if isinstance(period_to, str) else period_to)
        if self.period_from > self.period_to:
            raise ValueError("Period start is after period end.")

    # ---------------------------------------------------------------- checks
    def check_preflight_blockers(self, force: bool = False) -> Tuple[bool, Optional[str]]:
        if force:
            return True, None
        unrec = self.db.query(Statement).filter(
            Statement.user_id == self.user_id,
            Statement.account_id == self.account_id,
            Statement.reconciled == False,  # noqa: E712
            Statement.period_from <= self.period_to,
            Statement.period_to >= self.period_from,
        ).first()
        if unrec:
            return False, ("Unreconciled statements present covering this period: a bank statement "
                           "has not passed its own balance check. Resolve it or pass force=True.")
        pending = self.db.query(DuplicateMatch).filter(
            DuplicateMatch.user_id == self.user_id,
            DuplicateMatch.status == MatchStatus.PENDING_REVIEW.value,
        ).join(Transaction, Transaction.id == DuplicateMatch.duplicate_txn_id).filter(
            Transaction.account_id == self.account_id,
        ).first()
        if pending:
            return False, "Duplicate bank transactions are pending review for this account. Resolve or pass force=True."
        return True, None

    # ------------------------------------------------------------ main entry
    def execute_run(self, import_batch_id: Optional[UUID] = None, force: bool = False,
                    book_opening_paise: Optional[int] = None) -> ReconciliationRun:
        ok, reason = self.check_preflight_blockers(force=force)
        if not ok:
            raise ValueError(reason)

        batch = self._resolve_batch(import_batch_id)
        previous = self._previous_run()

        # ---- books in the period
        q = self.db.query(BookEntry).filter(
            BookEntry.user_id == self.user_id,
            BookEntry.account_id == self.account_id,
            BookEntry.entry_date >= self.period_from,
            BookEntry.entry_date <= self.period_to,
            BookEntry.reconciliation_status != ReconciliationStatusEnum.WRITTEN_OFF.value,
        )
        if batch is not None:
            q = q.filter(BookEntry.import_batch_id == batch.id)
        period_books = q.order_by(BookEntry.entry_date, BookEntry.row_index, BookEntry.id).all()

        if batch is not None and not period_books:
            if self.db.query(BookEntry.id).filter(BookEntry.import_batch_id == batch.id).first():
                raise ValueError(
                    f"No ledger transactions found within the selected reconciliation period "
                    f"({self.period_from} to {self.period_to}). Check that the ledger file covers "
                    "this period, or adjust the period dates.")

        # ---- opening / closing
        opening, opening_source = self._book_opening(book_opening_paise, batch, previous)
        movement = sum((b.money_in_paise or 0) - (b.money_out_paise or 0) for b in period_books)
        book_closing = opening + movement
        bank_closing = self._bank_closing()
        bank_opening = self._bank_opening()

        # ---- bank in the period
        period_banks = self.db.query(Transaction).filter(
            Transaction.user_id == self.user_id,
            Transaction.account_id == self.account_id,
            Transaction.txn_date >= self.period_from,
            Transaction.txn_date <= self.period_to,
            Transaction.superseded_by_id == None,  # noqa: E711
            Transaction.source_type == SourceType.STATEMENT,   # the bank statement, not alerts
        ).order_by(Transaction.txn_date, Transaction.row_index.nullsfirst(),
                   Transaction.created_at, Transaction.id).all()

        carried_books, carried_banks = self._carried_items(previous)

        prior = self.db.query(ReconciliationRun).filter(
            ReconciliationRun.user_id == self.user_id,
            ReconciliationRun.account_id == self.account_id,
            ReconciliationRun.period_from == self.period_from,
            ReconciliationRun.period_to == self.period_to,
        ).order_by(ReconciliationRun.version.desc()).first()

        run = ReconciliationRun(
            user_id=self.user_id,
            account_id=self.account_id,
            import_batch_id=batch.id if batch is not None else import_batch_id,
            period_from=self.period_from,
            period_to=self.period_to,
            book_opening_paise=opening,
            book_opening_source=opening_source,
            book_closing_paise=book_closing,
            bank_opening_paise=bank_opening or 0,
            bank_closing_paise=bank_closing if bank_closing is not None else 0,
            carried_from_run_id=previous.id if previous is not None else None,
            forced=force,
            engine_version=ENGINE_VERSION,
            status="processing",
            verdict=RunVerdictEnum.UNRECONCILED.value,
            residual_paise=0,
            version=(prior.version + 1) if prior and prior.version else 1,
            supersedes_run_id=prior.id if prior else None,
        )
        self.db.add(run)
        self.db.flush()

        books = [_book_view(b) for b in carried_books + period_books if (b.money_in_paise or b.money_out_paise)]
        banks = [_bank_view(t) for t in carried_banks + period_banks if (t.credit_paise or t.debit_paise)]
        books.sort(key=lambda e: e.order)
        banks.sort(key=lambda e: e.order)

        pairs = self._match(books, banks)
        debug = self._persist_pairs(run, pairs)
        run.debug_log_json = debug
        # Transient conveniences for callers (not columns).
        run.debug_log = debug
        run.opening_balance_paise = opening
        run.net_movement_paise = movement

        build_bridge(self.db, run, books, banks, has_bank_balance=bank_closing is not None,
                     period_to=self.period_to)
        logger.info("[RECON ENGINE] run %s: %s matched book entries, %s pending review, verdict %s",
                    run.id, run.matched_count, run.pending_review_count, run.verdict)
        return run

    # ------------------------------------------------------------- resolving
    def _resolve_batch(self, import_batch_id: Optional[UUID]) -> Optional[ImportBatch]:
        if import_batch_id:
            b = self.db.query(ImportBatch).filter(
                ImportBatch.id == import_batch_id,
                ImportBatch.user_id == self.user_id,
                ImportBatch.account_id == self.account_id,
            ).first()
            if b is not None:
                return b
        return self.db.query(ImportBatch).filter(
            ImportBatch.user_id == self.user_id,
            ImportBatch.account_id == self.account_id,
        ).order_by(ImportBatch.imported_at.desc()).first()

    def _previous_run(self) -> Optional[ReconciliationRun]:
        """Latest completed run for this account that ended before this period."""
        return self.db.query(ReconciliationRun).filter(
            ReconciliationRun.user_id == self.user_id,
            ReconciliationRun.account_id == self.account_id,
            ReconciliationRun.period_to < self.period_from,
            ReconciliationRun.status.in_(("completed", "completed_no_bank_statement")),
            ReconciliationRun.is_archived == False,  # noqa: E712
        ).order_by(ReconciliationRun.period_to.desc(), ReconciliationRun.version.desc(),
                   ReconciliationRun.created_at.desc()).first()

    def _book_opening(self, typed: Optional[int], batch: Optional[ImportBatch],
                      previous: Optional[ReconciliationRun]) -> Tuple[int, str]:
        if typed is not None:
            return int(typed), "manual"
        if batch is not None and batch.book_opening_paise is not None and batch.book_opening_source == "manual" \
                and (batch.period_from is None or batch.period_from >= self.period_from):
            return int(batch.book_opening_paise), "manual"
        if previous is not None and previous.period_to == self.period_from - timedelta(days=1):
            return int(previous.book_closing_paise), "previous_run"
        if batch is not None and batch.book_opening_paise is not None:
            # The file's opening is the balance at the file's first date; roll it
            # forward through any of the file's entries dated before this period.
            start = batch.period_from or self.period_from
            if start <= self.period_from:
                earlier = self.db.query(BookEntry).filter(
                    BookEntry.import_batch_id == batch.id,
                    BookEntry.entry_date < self.period_from,
                ).all()
                rolled = sum((b.money_in_paise or 0) - (b.money_out_paise or 0) for b in earlier)
                return int(batch.book_opening_paise) + rolled, "ledger_file"
        if previous is not None:
            raise BookOpeningRequired(
                f"The last reconciliation for this account ended on {previous.period_to}, not on "
                f"{self.period_from - timedelta(days=1)}, so its closing cannot be used as this "
                "period's opening. Enter the book opening balance.")
        raise BookOpeningRequired(
            "Enter the book (ledger) opening balance for this account as at "
            f"{self.period_from}. It is needed once; later periods continue from this reconciliation.")

    def _bank_closing(self) -> Optional[int]:
        t = self.db.query(Transaction).filter(
            Transaction.user_id == self.user_id,
            Transaction.account_id == self.account_id,
            Transaction.txn_date <= self.period_to,
            Transaction.superseded_by_id == None,  # noqa: E711
            Transaction.source_type == SourceType.STATEMENT,   # the bank statement, not alerts
            Transaction.balance_paise != None,  # noqa: E711
        ).order_by(Transaction.txn_date.desc(), Transaction.row_index.desc().nullslast(),
                   Transaction.created_at.desc()).first()
        if t is not None:
            return int(t.balance_paise)
        s = self.db.query(Statement).filter(
            Statement.user_id == self.user_id,
            Statement.account_id == self.account_id,
            Statement.period_to <= self.period_to,
            Statement.closing_balance_paise != None,  # noqa: E711
        ).order_by(Statement.period_to.desc(), Statement.uploaded_at.desc()).first()
        return int(s.closing_balance_paise) if s is not None else None

    def _bank_opening(self) -> Optional[int]:
        t = self.db.query(Transaction).filter(
            Transaction.user_id == self.user_id,
            Transaction.account_id == self.account_id,
            Transaction.txn_date < self.period_from,
            Transaction.superseded_by_id == None,  # noqa: E711
            Transaction.source_type == SourceType.STATEMENT,   # the bank statement, not alerts
            Transaction.balance_paise != None,  # noqa: E711
        ).order_by(Transaction.txn_date.desc(), Transaction.row_index.desc().nullslast(),
                   Transaction.created_at.desc()).first()
        return int(t.balance_paise) if t is not None else None

    def _carried_items(self, previous: Optional[ReconciliationRun]) -> Tuple[List[BookEntry], List[Transaction]]:
        if previous is None:
            return [], []
        items = self.db.query(ReconciliationItem).filter(ReconciliationItem.run_id == previous.id).all()
        book_ids = {i.book_entry_id for i in items
                    if i.book_entry_id is not None and i.bank_txn_id is None}
        bank_ids = {i.bank_txn_id for i in items
                    if i.bank_txn_id is not None and i.book_entry_id is None}
        books = self.db.query(BookEntry).filter(
            BookEntry.id.in_(book_ids),
            BookEntry.entry_date < self.period_from,
            BookEntry.reconciliation_status != ReconciliationStatusEnum.WRITTEN_OFF.value,
        ).all() if book_ids else []
        banks = self.db.query(Transaction).filter(
            Transaction.id.in_(bank_ids),
            Transaction.txn_date < self.period_from,
            Transaction.superseded_by_id == None,  # noqa: E711
            Transaction.source_type == SourceType.STATEMENT,   # the bank statement, not alerts
        ).all() if bank_ids else []
        return books, banks

    def _decisions(self) -> Tuple[Set[Tuple[UUID, UUID]], List[ReconciliationMatch]]:
        """Pairs a person rejected (never propose again) and matches a person
        confirmed (re-apply), across every earlier run of this account."""
        rows = self.db.query(ReconciliationMatch).join(
            ReconciliationRun, ReconciliationRun.id == ReconciliationMatch.run_id
        ).filter(
            ReconciliationRun.user_id == self.user_id,
            ReconciliationRun.account_id == self.account_id,
            ReconciliationMatch.status.in_((MatchStatusEnum.REJECTED.value, MatchStatusEnum.CONFIRMED.value)),
        ).order_by(ReconciliationMatch.reviewed_at.desc().nullslast()).all()
        rejected: Set[Tuple[UUID, UUID]] = set()
        confirmed: List[ReconciliationMatch] = []
        for m in rows:
            if m.status == MatchStatusEnum.REJECTED.value:
                bks = [l.book_entry_id for l in m.lines if l.book_entry_id]
                bns = [l.bank_txn_id for l in m.lines if l.bank_txn_id]
                rejected.update((b, t) for b in bks for t in bns)
            else:
                confirmed.append(m)
        return rejected, confirmed

    # -------------------------------------------------------------- matching
    def _match(self, books: List[_Entry], banks: List[_Entry]) -> List[_Pair]:
        rejected, confirmed = self._decisions()
        used_b: Set[UUID] = set()
        used_t: Set[UUID] = set()
        pairs: List[_Pair] = []
        book_by_id = {e.id: e for e in books}
        bank_by_id = {e.id: e for e in banks}

        # Indexes so every pass looks only at plausible partners (a month of a
        # busy account is thousands of rows on each side).
        bank_by_amt: Dict[Tuple[bool, int], List[_Entry]] = {}
        book_by_amt: Dict[Tuple[bool, int], List[_Entry]] = {}
        bank_by_id_tok: Dict[str, List[_Entry]] = {}
        for t in banks:
            bank_by_amt.setdefault((t.credit, t.amount), []).append(t)
            for tok in t.ids:
                bank_by_id_tok.setdefault(tok, []).append(t)
        for b in books:
            book_by_amt.setdefault((b.credit, b.amount), []).append(b)

        def free(b: _Entry, t: _Entry) -> bool:
            return (b.id not in used_b and t.id not in used_t and b.credit == t.credit
                    and (b.id, t.id) not in rejected)

        def take(p: _Pair) -> None:
            pairs.append(p)
            used_b.update(e.id for e in p.books)
            used_t.update(e.id for e in p.banks)

        def gap(b: _Entry, t: _Entry) -> int:
            return abs((b.day - t.day).days)

        def window(b: _Entry, shared: Set[str]) -> int:
            return CHEQUE_WINDOW_DAYS if (b.cheque_no and b.cheque_no in shared) else AUTO_WINDOW_DAYS

        def id_partners(b: _Entry) -> List[_Entry]:
            seen: Dict[UUID, _Entry] = {}
            for tok in b.ids:
                for t in bank_by_id_tok.get(tok, ()):
                    seen[t.id] = t
            return sorted(seen.values(), key=lambda e: e.order)

        # 0. decisions a reviewer confirmed in earlier runs
        for m in confirmed:
            bks = [book_by_id.get(l.book_entry_id) for l in m.lines if l.book_entry_id]
            bns = [bank_by_id.get(l.bank_txn_id) for l in m.lines if l.bank_txn_id]
            if not bks or not bns or None in bks or None in bns:
                continue
            if any(e.id in used_b for e in bks) or any(e.id in used_t for e in bns):
                continue
            take(_Pair(bks, bns, m.tier, MatchStatusEnum.CONFIRMED.value, 1.0,
                       "Confirmed by a reviewer in an earlier run", "CONFIRMED", m.id))

        # A. same amount + shared transaction ID (auto)
        for b in books:
            if b.id in used_b or not b.ids:
                continue
            best = None
            for t in id_partners(b):
                if not free(b, t) or t.amount != b.amount:
                    continue
                shared = b.ids & t.ids
                g = gap(b, t)
                if g <= window(b, shared) and (best is None or g < best[0]):
                    best = (g, t, shared)
            if best:
                g, t, shared = best
                take(_Pair([b], [t], TIER_ID, MatchStatusEnum.AUTO_MATCHED.value,
                           1.0 if g == 0 else 0.97,
                           f"Same amount and transaction ID {sorted(shared)[0]}"
                           + (f"; dates {g} day(s) apart" if g else ""),
                           "DATE_MISMATCH" if g else "EXACT_MATCH"))

        # B. same amount, same date (auto; identical entries are interchangeable)
        for b in books:
            if b.id in used_b:
                continue
            for t in bank_by_amt.get((b.credit, b.amount), ()):
                if free(b, t) and t.day == b.day:
                    take(_Pair([b], [t], TIER_AMOUNT, MatchStatusEnum.AUTO_MATCHED.value, 0.98,
                               "Same amount, direction and date", "EXACT_MATCH"))
                    break

        # C. same amount within the window, the only candidate on BOTH sides (auto)
        changed = True
        while changed:
            changed = False
            for b in books:
                if b.id in used_b:
                    continue
                cands = [t for t in bank_by_amt.get((b.credit, b.amount), ())
                         if free(b, t) and gap(b, t) <= AUTO_WINDOW_DAYS]
                if len(cands) != 1:
                    continue
                t = cands[0]
                rivals = [x for x in book_by_amt.get((t.credit, t.amount), ())
                          if x.id not in used_b and (x.id, t.id) not in rejected
                          and gap(x, t) <= AUTO_WINDOW_DAYS]
                if len(rivals) != 1:
                    continue
                g = gap(b, t)
                take(_Pair([b], [t], TIER_AMOUNT, MatchStatusEnum.AUTO_MATCHED.value, 0.9,
                           f"Same amount and direction, {g} day(s) apart, no other candidate on either side",
                           "DATE_MISMATCH"))
                changed = True

        # D. shared transaction ID, different amount (review; difference listed)
        for b in books:
            if b.id in used_b or not b.ids:
                continue
            best = None
            for t in id_partners(b):
                if not free(b, t) or t.amount == b.amount:
                    continue
                shared = b.ids & t.ids
                g = gap(b, t)
                key = (g, abs(t.amount - b.amount))
                if g <= window(b, shared) and (best is None or key < best[0]):
                    best = (key, t, shared)
            if best:
                _, t, shared = best
                take(_Pair([b], [t], TIER_AMOUNT_DIFF, MatchStatusEnum.PENDING_REVIEW.value, 0.8,
                           f"Same transaction ID {sorted(shared)[0]} but amounts differ "
                           f"(books {b.amount / 100:,.2f}, bank {t.amount / 100:,.2f})",
                           "AMOUNT_MISMATCH"))

        # F. groups (review): one bank = several ledger rows; one ledger row = several bank rows
        for t in sorted(banks, key=lambda e: (-e.amount, e.order)):
            if t.id in used_t:
                continue
            pool = [b for b in books if free(b, t) and b.amount < t.amount
                    and gap(b, t) <= AUTO_WINDOW_DAYS]
            combo = _subset_sum(t, pool)
            if combo:
                take(_Pair(combo, [t], TIER_GROUP, MatchStatusEnum.PENDING_REVIEW.value, 0.85,
                           f"One bank entry equals the sum of {len(combo)} ledger entries", "GROUP"))
        for b in sorted(books, key=lambda e: (-e.amount, e.order)):
            if b.id in used_b:
                continue
            pool = [t for t in banks if free(b, t) and t.amount < b.amount
                    and gap(b, t) <= AUTO_WINDOW_DAYS]
            combo = _subset_sum(b, pool)
            if combo:
                take(_Pair([b], combo, TIER_GROUP, MatchStatusEnum.PENDING_REVIEW.value, 0.85,
                           f"One ledger entry equals the sum of {len(combo)} bank entries", "GROUP"))

        # E. suggestions (review): same amount, shared party word or nearest date
        for b in books:
            if b.id in used_b:
                continue
            best = None
            for t in bank_by_amt.get((b.credit, b.amount), ()):
                if not free(b, t):
                    continue
                g = gap(b, t)
                if g > SUGGEST_WINDOW_DAYS:
                    continue
                common = len(b.words & t.words)
                if common == 0 and g > AUTO_WINDOW_DAYS:
                    continue
                key = (-common, g, t.order)
                if best is None or key < best[0]:
                    best = (key, t, common, g)
            if best:
                _, t, common, g = best
                why = (f"shares the name {sorted(b.words & t.words)[0]}" if common
                       else "nearest entry with the same amount")
                take(_Pair([b], [t], TIER_SUGGESTED, MatchStatusEnum.PENDING_REVIEW.value, 0.6,
                           f"Suggested: same amount, {g} day(s) apart, {why}", "SUGGESTED"))
        return pairs

    def _persist_pairs(self, run: ReconciliationRun, pairs: List[_Pair]) -> List[Dict[str, Any]]:
        debug = []
        for p in pairs:
            m = ReconciliationMatch(run_id=run.id, user_id=self.user_id, tier=p.tier,
                                    confidence=p.confidence, status=p.status, reason=p.reason)
            if p.status == MatchStatusEnum.CONFIRMED.value:
                m.reviewed_at = datetime.utcnow()
            self.db.add(m)
            self.db.flush()
            for e in p.books:
                self.db.add(ReconciliationMatchLine(match_id=m.id, book_entry_id=e.id))
            for e in p.banks:
                self.db.add(ReconciliationMatchLine(match_id=m.id, bank_txn_id=e.id))
            if p.status == MatchStatusEnum.AUTO_MATCHED.value or p.status == MatchStatusEnum.CONFIRMED.value:
                for e in p.books:
                    e.obj.reconciliation_status = ReconciliationStatusEnum.MATCHED.value
            b0, t0 = p.books[0], p.banks[0]
            debug.append({
                "match_id": str(m.id),
                "ledger_transaction_ids": [str(e.id) for e in p.books],
                "bank_transaction_ids": [str(e.id) for e in p.banks],
                "ledger_transaction_id": str(b0.id),
                "bank_transaction_id": str(t0.id),
                "ledger_date": str(b0.day), "bank_date": str(t0.day),
                "ledger_amount": sum(e.amount for e in p.books) / 100.0,
                "bank_amount": sum(e.amount for e in p.banks) / 100.0,
                "direction": "CREDIT" if b0.credit else "DEBIT",
                "description_similarity": round(calculate_text_similarity(t0.text, b0.text), 3),
                "match_score": p.confidence,
                "match_status": p.kind,
                "status": p.status,
                "reason": p.reason,
            })
        self.db.flush()
        return debug


def _subset_sum(target: _Entry, pool: Sequence[_Entry]) -> Optional[List[_Entry]]:
    """Smallest set of 2..GROUP_MAX_PARTS entries from `pool` whose amounts sum
    exactly to target.amount. Bounded: at most GROUP_CANDIDATES entries (nearest
    by date) and GROUP_NODE_BUDGET search steps, so it can never stall a run."""
    if len(pool) < 2:
        return None
    pool = sorted(pool, key=lambda e: (abs((e.day - target.day).days), e.order))[:GROUP_CANDIDATES]
    items = sorted(pool, key=lambda e: -e.amount)
    amounts = [e.amount for e in items]
    budget = [GROUP_NODE_BUDGET]

    for size in range(2, min(GROUP_MAX_PARTS, len(items)) + 1):
        chosen: List[int] = []

        def dfs(start: int, remaining: int, left: int) -> bool:
            if budget[0] <= 0:
                return False
            budget[0] -= 1
            if left == 0:
                return remaining == 0
            for i in range(start, len(items) - left + 1):
                a = amounts[i]
                if a > remaining:
                    continue
                # the largest `left` amounts from i onward must be able to reach remaining
                if sum(amounts[i:i + left]) < remaining:
                    break
                chosen.append(i)
                if dfs(i + 1, remaining - a, left - 1):
                    return True
                chosen.pop()
            return False

        if dfs(0, target.amount, size):
            return [items[i] for i in chosen]
        if budget[0] <= 0:
            break
    return None


# --------------------------------------------------------------------------- #
# Bridge (shared by a fresh run and by rebuild after a review decision)
# --------------------------------------------------------------------------- #

BANK_CHARGE_WORDS = ("CHARGES", "FEE", " GST", "GST ", "TAX", " CHG", "CHRG", "LEVY", "PENALTY",
                     "COMMISSION", "MAINT", "SMS CHG", "SMSCHG", "DEMAT", "FOLIO", "LOCKER")
STANDING_WORDS = ("EMI", "LOAN", "ACH", "NACH", "ECS", "SI ")
RETURN_WORDS = ("RETURN", "RTN", "DISHONOUR", "CHQ RET", "INSUFF", "BOUNCE")
INTEREST_WORDS = ("INT.PD", "INTEREST", "CR INT", "INT.CR", "1NT.CR", "INT CR")


def _bank_category(t: Transaction, credit: bool) -> Tuple[str, bool, Optional[str]]:
    text = " " + ((t.narration_raw or "") + " " + (t.narration_clean or "")).upper() + " "
    if not credit:
        if any(k in text for k in RETURN_WORDS):
            return "dishonoured_cheque", True, "journal_entry_required"
        if any(k in text for k in BANK_CHARGE_WORDS):
            return "bank_charge", True, "journal_entry_required"
        if any(k in text for k in STANDING_WORDS):
            return "standing_instruction", True, "journal_entry_required"
        return "UNMATCHED_BANK_TRANSACTION", False, None
    if any(k in text for k in INTEREST_WORDS):
        return "interest_credit", True, "journal_entry_required"
    if any(k in text for k in RETURN_WORDS):
        return "dishonoured_cheque", True, "journal_entry_required"
    if any(k in text for k in ("NEFT", "UPI", "IMPS", "RTGS")):
        return "direct_credit", False, None
    return "UNMATCHED_BANK_TRANSACTION", False, None


def build_bridge(db: Session, run: ReconciliationRun, books: List[_Entry], banks: List[_Entry],
                 has_bank_balance: bool, period_to: date,
                 overrides: Optional[Dict[Tuple, Tuple]] = None) -> None:
    """(Re)create the run's BRS items from its active matches, then totals and verdict.

    `overrides` carries categories a person set on items (classify endpoint),
    keyed by (book_entry_id, bank_txn_id), so a rebuild never discards them.
    """
    overrides = overrides or {}
    db.query(ReconciliationItem).filter(ReconciliationItem.run_id == run.id).delete(synchronize_session=False)
    matches = db.query(ReconciliationMatch).filter(ReconciliationMatch.run_id == run.id).all()
    active = [m for m in matches if m.status in ACTIVE_MATCH_STATUSES]
    book_by_id = {e.id: e for e in books}
    bank_by_id = {e.id: e for e in banks}

    matched_b: Set[UUID] = set()
    matched_t: Set[UUID] = set()
    items: List[ReconciliationItem] = []

    def add_item(**kw) -> None:
        it = ReconciliationItem(run_id=run.id, user_id=run.user_id, **kw)
        ov = overrides.get((kw.get("book_entry_id"), kw.get("bank_txn_id")))
        if ov:
            it.brs_category, it.overridden_by_user, it.overridden_by, it.overridden_at = ov[0], True, ov[1], ov[2]
        db.add(it)
        items.append(it)

    for m in active:
        bks = [book_by_id[l.book_entry_id] for l in m.lines if l.book_entry_id in book_by_id]
        bns = [bank_by_id[l.bank_txn_id] for l in m.lines if l.bank_txn_id in bank_by_id]
        matched_b.update(e.id for e in bks)
        matched_t.update(e.id for e in bns)
        diff = sum(e.signed for e in bns) - sum(e.signed for e in bks)
        if diff and bks:
            add_item(side=BRSSideEnum.BOOK.value, brs_category="amount_difference",
                     amount_paise=abs(diff),
                     direction=(DirectionEnum.ADD if diff > 0 else DirectionEnum.SUBTRACT).value,
                     age_days=max(0, (period_to - bks[0].day).days),
                     exception_flag=True, exception_reason="book_amount_differs_from_bank",
                     book_entry_id=bks[0].id, bank_txn_id=bns[0].id if bns else None)

    # Ledger rows left over that repeat a matched row: possible duplicates.
    matched_keys: Dict[Tuple, int] = {}
    for e in books:
        if e.id in matched_b:
            k = (e.day, e.credit, e.amount)
            matched_keys[k] = matched_keys.get(k, 0) + 1

    for e in books:
        if e.id in matched_b:
            continue
        age = max(0, (period_to - e.day).days)
        k = (e.day, e.credit, e.amount)
        if matched_keys.get(k):
            matched_keys[k] -= 1
            add_item(side=BRSSideEnum.BOOK.value,
                     brs_category="duplicate_ledger_credit" if e.credit else "duplicate_ledger_debit",
                     amount_paise=e.amount,
                     direction=(DirectionEnum.SUBTRACT if e.credit else DirectionEnum.ADD).value,
                     age_days=age, exception_flag=True, exception_reason="possible_duplicate_ledger_row",
                     book_entry_id=e.id)
            continue
        if e.credit:
            add_item(side=BRSSideEnum.BOOK.value, brs_category="uncleared_deposit",
                     amount_paise=e.amount, direction=DirectionEnum.SUBTRACT.value, age_days=age,
                     exception_flag=age > 7, exception_reason="deposit_in_transit_delayed" if age > 7 else None,
                     book_entry_id=e.id)
        else:
            add_item(side=BRSSideEnum.BOOK.value, brs_category="unpresented_cheque",
                     amount_paise=e.amount, direction=DirectionEnum.ADD.value, age_days=age,
                     exception_flag=age > 90, exception_reason="stale_cheque_write_back_required" if age > 90 else None,
                     book_entry_id=e.id)

    for e in banks:
        if e.id in matched_t:
            continue
        cat, flag, why = _bank_category(e.obj, e.credit)
        add_item(side=BRSSideEnum.BANK.value, brs_category=cat, amount_paise=e.amount,
                 direction=(DirectionEnum.ADD if e.credit else DirectionEnum.SUBTRACT).value,
                 age_days=max(0, (period_to - e.day).days), exception_flag=flag,
                 exception_reason=why, bank_txn_id=e.id)

    db.flush()
    net = sum(i.amount_paise if i.direction == DirectionEnum.ADD.value else -i.amount_paise for i in items)
    computed = int(run.book_closing_paise) + net
    run.computed_bank_closing_paise = computed

    book_items = [i for i in items if i.side == BRSSideEnum.BOOK.value]
    bank_items = [i for i in items if i.side == BRSSideEnum.BANK.value]
    pending = sum(1 for m in active if m.status == MatchStatusEnum.PENDING_REVIEW.value)
    run.pending_review_count = pending
    run.matched_count = len(matched_b)
    run.unmatched_book_count = sum(1 for i in book_items if i.brs_category != "amount_difference")
    run.unmatched_bank_count = len(bank_items)

    outstanding = bool(items) or pending > 0
    if has_bank_balance:
        run.residual_paise = int(run.bank_closing_paise) - computed
        run.status = "completed"
        if run.residual_paise != 0:
            run.verdict = RunVerdictEnum.UNRECONCILED.value
        else:
            run.verdict = (RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value if outstanding
                           else RunVerdictEnum.RECONCILED_CLEAN.value)
    else:
        # No bank balance on file: the bridge cannot be checked against a real
        # closing, so never call it reconciled.
        run.bank_closing_paise = computed
        run.residual_paise = 0
        run.status = "completed_no_bank_statement"
        run.verdict = RunVerdictEnum.UNRECONCILED.value
    run.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(run)


def rebuild_run(db: Session, run: ReconciliationRun) -> ReconciliationRun:
    """Recompute a run's bridge after a match is confirmed or rejected.

    The run's pools are exactly the entries in its match lines and items, so the
    rebuild needs no access to the period's files and cannot pick up entries
    uploaded after the run.
    """
    lines = db.query(ReconciliationMatchLine).join(
        ReconciliationMatch, ReconciliationMatch.id == ReconciliationMatchLine.match_id
    ).filter(ReconciliationMatch.run_id == run.id).all()
    items = db.query(ReconciliationItem).filter(ReconciliationItem.run_id == run.id).all()
    overrides = {(i.book_entry_id, i.bank_txn_id): (i.brs_category, i.overridden_by, i.overridden_at)
                 for i in items if i.overridden_by_user}
    book_ids = {l.book_entry_id for l in lines if l.book_entry_id} | {i.book_entry_id for i in items if i.book_entry_id}
    bank_ids = {l.bank_txn_id for l in lines if l.bank_txn_id} | {i.bank_txn_id for i in items if i.bank_txn_id}
    books = [_book_view(b) for b in db.query(BookEntry).filter(BookEntry.id.in_(book_ids)).all()] if book_ids else []
    banks = [_bank_view(t) for t in db.query(Transaction).filter(Transaction.id.in_(bank_ids)).all()] if bank_ids else []
    books.sort(key=lambda e: e.order)
    banks.sort(key=lambda e: e.order)

    status = {m.id: m.status for m in db.query(ReconciliationMatch).filter(ReconciliationMatch.run_id == run.id)}
    for b in books:
        b.obj.reconciliation_status = ReconciliationStatusEnum.UNMATCHED.value
    active_books = {l.book_entry_id for l in lines if l.book_entry_id
                    and status.get(l.match_id) in (MatchStatusEnum.AUTO_MATCHED.value, MatchStatusEnum.CONFIRMED.value)}
    for b in books:
        if b.id in active_books:
            b.obj.reconciliation_status = ReconciliationStatusEnum.MATCHED.value

    build_bridge(db, run, books, banks,
                 has_bank_balance=run.status != "completed_no_bank_statement",
                 period_to=run.period_to, overrides=overrides)
    return run
