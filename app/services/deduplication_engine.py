import logging
import re
import uuid
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional, Tuple
from uuid import UUID
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.models.transaction import Transaction, SourceType
from app.models.duplicate_match import DuplicateMatch, DuplicateTier, MatchStatus

logger = logging.getLogger(__name__)


def normalize_reference(ref: Optional[str]) -> Optional[str]:
    """Clean and normalize reference numbers for matching."""
    if not ref or not str(ref).strip():
        return None
    cleaned = re.sub(r'[^A-Z0-9]', '', str(ref).upper().strip())
    # Ignore generic short, empty or non-unique description words slipped through as references
    if len(cleaned) < 5 or cleaned in ("PAYMENT", "RECEIPT", "TRANSFER", "CHQ", "CHEQUE", "CUSTOMER", "CHARGES", "HANDLING", "SERVICE"):
        return None

    # Handle standard Indian bank NEFT/UPI UTR prefixes (e.g. YE5CB vs YESCB)
    if cleaned.startswith("YE5") and len(cleaned) > 5:
        cleaned = "YES" + cleaned[3:]
    return cleaned


def tokenize_narration(text: Optional[str]) -> set:
    """Extract significant uppercase tokens from narration for similarity matching."""
    if not text:
        return set()
    tokens = re.findall(r'[A-Z0-9]{3,}', str(text).upper())
    # Filter out common transaction noise words
    noise = {"UPI", "NEFT", "RTGS", "IMPS", "BARBZ", "BARB", "YESB", "BARB0", "YESB0", "TRANSFER", "PAYMENT", "DR", "CR", "TO", "BY", "FOR"}
    return {t for t in tokens if t not in noise}


def compute_token_similarity(text1: Optional[str], text2: Optional[str]) -> float:
    """Jaccard similarity coefficient between narration tokens."""
    t1 = tokenize_narration(text1)
    t2 = tokenize_narration(text2)
    if not t1 or not t2:
        return 0.0
    intersection = len(t1.intersection(t2))
    union = len(t1.union(t2))
    return intersection / union if union > 0 else 0.0


def select_kept_and_duplicate(t1: Transaction, t2: Transaction) -> Tuple[Transaction, Transaction]:
    """
    Determines which transaction to keep (canonical) and which to mark duplicate.
    Hierarchy:
      1. Statement > Email Alert / SMS
      2. Lowest row_index (if both statement)
      3. Earliest created_at timestamp
    """
    # Source type priority
    if t1.source_type == SourceType.STATEMENT and t2.source_type != SourceType.STATEMENT:
        return t1, t2
    if t2.source_type == SourceType.STATEMENT and t1.source_type != SourceType.STATEMENT:
        return t2, t1

    # Row index priority for statements
    if t1.row_index is not None and t2.row_index is not None:
        if t1.row_index <= t2.row_index:
            return t1, t2
        else:
            return t2, t1

    # Created at timestamp fallback
    if t1.created_at and t2.created_at:
        if t1.created_at <= t2.created_at:
            return t1, t2
        else:
            return t2, t1

    return t1, t2


class DeduplicationEngine:
    def __init__(self, db: Session, user_id: UUID, account_id: UUID):
        self.db = db
        self.user_id = user_id
        self.account_id = account_id

    def run_deduplication(self) -> Dict[str, Any]:
        """
        Scans all active (non-superseded) transactions for user_id/account_id.
        Evaluates Tier 1 (Exact Reference) and Tier 2 (Heuristic Date+Amount+Narration).
        """
        # Fetch active transactions
        active_txns: List[Transaction] = self.db.query(Transaction).filter(
            Transaction.user_id == self.user_id,
            Transaction.account_id == self.account_id,
            Transaction.superseded_by_id == None
        ).order_by(Transaction.txn_date.asc(), Transaction.row_index.asc()).all()

        auto_merged_count = 0
        pending_review_count = 0
        pairs_evaluated = 0

        superseded_in_this_run = set()

        kept_in_this_run = set()

        # -----------------------------------------------------------------------
        # TIER 1: Exact Reference Number + Direction + Amount Match
        # -----------------------------------------------------------------------
        ref_map: Dict[str, List[Transaction]] = {}
        for t in active_txns:
            ref = normalize_reference(t.reference_no)
            if ref:
                ref_map.setdefault(ref, []).append(t)

        for ref, txns in ref_map.items():
            if len(txns) < 2:
                continue

            # Group transactions by (direction, amount_paise)
            grouped: Dict[Tuple[str, int], List[Transaction]] = {}
            for t in txns:
                amt = t.debit_paise if t.direction == "debit" else t.credit_paise
                if amt is not None:
                    grouped.setdefault((t.direction.value if hasattr(t.direction, 'value') else str(t.direction), int(amt)), []).append(t)

            for (direction, amt), group in grouped.items():
                if len(group) < 2:
                    continue

                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        t_a = group[i]
                        t_b = group[j]

                        if t_a.id in superseded_in_this_run or t_b.id in superseded_in_this_run:
                            continue
                        if t_a.id in kept_in_this_run or t_b.id in kept_in_this_run:
                            continue

                        # Tier 1 exact reference deduplication requires transactions to fall within a 3-day window
                        # to prevent merging separate monthly/weekly recurring transactions sharing the same reference format
                        days_diff = abs((t_a.txn_date - t_b.txn_date).days)
                        if days_diff > 3:
                            continue
                        # Same reference but a different running balance: two rows
                        # (e.g. a payment and its reversal-and-repay), not one.
                        if (t_a.balance_paise is not None and t_b.balance_paise is not None
                                and t_a.balance_paise != t_b.balance_paise):
                            continue

                        kept_txn, dup_txn = select_kept_and_duplicate(t_a, t_b)

                        existing = self.db.query(DuplicateMatch).filter(
                            DuplicateMatch.user_id == self.user_id,
                            DuplicateMatch.duplicate_txn_id == dup_txn.id,
                            DuplicateMatch.kept_txn_id == kept_txn.id
                        ).first()

                        if not existing:
                            match_rec = DuplicateMatch(
                                id=uuid.uuid4(),
                                user_id=self.user_id,
                                duplicate_txn_id=dup_txn.id,
                                kept_txn_id=kept_txn.id,
                                tier=DuplicateTier.TIER_1,
                                confidence=1.0,
                                status=MatchStatus.AUTO_MERGED
                            )
                            self.db.add(match_rec)
                            dup_txn.superseded_by_id = kept_txn.id
                            superseded_in_this_run.add(dup_txn.id)
                            kept_in_this_run.add(kept_txn.id)
                            auto_merged_count += 1
                        pairs_evaluated += 1

        self.db.flush()

        # Re-fetch remaining active transactions post-Tier 1
        remaining_txns: List[Transaction] = [t for t in active_txns if t.id not in superseded_in_this_run]

        # -----------------------------------------------------------------------
        # TIER 2: Heuristic Date Window (<= 3 days) + Amount Match
        # -----------------------------------------------------------------------
        amt_groups: Dict[Tuple[str, int], List[Transaction]] = {}
        for t in remaining_txns:
            amt = t.debit_paise if t.direction == "debit" else t.credit_paise
            if amt is not None:
                amt_groups.setdefault((t.direction.value if hasattr(t.direction, 'value') else str(t.direction), int(amt)), []).append(t)

        for (direction, amt), group in amt_groups.items():
            if len(group) < 2:
                continue

            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    t_a = group[i]
                    t_b = group[j]

                    if t_a.id in superseded_in_this_run or t_b.id in superseded_in_this_run:
                        continue
                    if t_a.id in kept_in_this_run or t_b.id in kept_in_this_run:
                        continue

                    # Intra-statement check: Never auto-merge distinct rows within the same uploaded statement file in Tier 2
                    if t_a.statement_id and t_a.statement_id == t_b.statement_id:
                        continue

                    # Different running balances on the same account mean two
                    # different rows of the bank's ledger, whatever the
                    # narration says: a duplicate repeats the row INCLUDING its
                    # balance. (Same-day repeat purchases were being superseded
                    # here, understating every total.)
                    if (t_a.balance_paise is not None and t_b.balance_paise is not None
                            and t_a.balance_paise != t_b.balance_paise):
                        continue

                    days_diff = abs((t_a.txn_date - t_b.txn_date).days)
                    if days_diff > 3:
                        continue

                    kept_txn, dup_txn = select_kept_and_duplicate(t_a, t_b)

                    ref_a = normalize_reference(t_a.reference_no)
                    ref_b = normalize_reference(t_b.reference_no)

                    # Reference mismatch check (#6): If both carry different reference numbers, NEVER auto-merge -> pending_review
                    if ref_a and ref_b and ref_a != ref_b:
                        status = MatchStatus.PENDING_REVIEW
                        score = 0.60
                    else:
                        base_score = 0.85 if days_diff == 0 else 0.70
                        narr_a = (t_a.narration_clean or t_a.narration_raw or "")
                        narr_b = (t_b.narration_clean or t_b.narration_raw or "")
                        similarity = compute_token_similarity(narr_a, narr_b)
                        score = min(0.95, base_score + (similarity * 0.10))
                        status = MatchStatus.AUTO_MERGED if score >= 0.85 else MatchStatus.PENDING_REVIEW

                    pairs_evaluated += 1

                    existing = self.db.query(DuplicateMatch).filter(
                        DuplicateMatch.user_id == self.user_id,
                        DuplicateMatch.duplicate_txn_id == dup_txn.id,
                        DuplicateMatch.kept_txn_id == kept_txn.id
                    ).first()

                    if existing:
                        continue

                    match_rec = DuplicateMatch(
                        id=uuid.uuid4(),
                        user_id=self.user_id,
                        duplicate_txn_id=dup_txn.id,
                        kept_txn_id=kept_txn.id,
                        tier=DuplicateTier.TIER_2,
                        confidence=round(score, 2),
                        status=status
                    )
                    self.db.add(match_rec)

                    if status == MatchStatus.AUTO_MERGED:
                        dup_txn.superseded_by_id = kept_txn.id
                        superseded_in_this_run.add(dup_txn.id)
                        kept_in_this_run.add(kept_txn.id)
                        auto_merged_count += 1
                    else:
                        pending_review_count += 1

        self.db.commit()

        return {
            "user_id": str(self.user_id),
            "account_id": str(self.account_id),
            "pairs_evaluated": pairs_evaluated,
            "auto_merged": auto_merged_count,
            "pending_review": pending_review_count,
            "total_superseded": len(superseded_in_this_run)
        }
