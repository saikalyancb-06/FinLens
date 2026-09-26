"""Re-run categorisation over transactions that are already in the ledger.

A transaction keeps whatever category it was given at upload time. That is
correct for a manual decision — nothing should silently overwrite a human — but
it means an improvement to the rules only ever helps the NEXT upload. A user who
imported 1,823 rows before a rule existed is stuck with the old answer, and the
only way out was to clear the account and re-upload, which is destructive and
loses every review decision already made.

This module closes that loop: it re-classifies the rows that are still unresolved
and applies the counterparty memory to them, in place.

WHAT IT WILL NEVER TOUCH
------------------------
Rows a human has decided on (`classification_method='manual'`) and rows that
already carry a category are left exactly as they are, unless `force=True` is
passed explicitly. Re-categorisation is for filling in blanks, not for
second-guessing the user. Losing a morning of review work to a background
re-scan would be a far worse bug than a stale category.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.categorization.counterparty_memory import load_memory, lookup
from app.categorization.hybrid import (
    METHOD_MANUAL, classify_transaction, memory_should_override,
)
from app.models.category import Category
from app.models.prediction import Prediction
from app.models.transaction import Transaction
from app.categorization.deep import classify_deep
from app.services.category_seeder import (
    normalize_category_name, resolve_path, seed_categories, seed_category_tree,
)

logger = logging.getLogger(__name__)

METHOD_COUNTERPARTY = "counterparty_memory"


@dataclass
class RecategorizeResult:
    examined: int = 0
    categorized: int = 0
    # Of `categorized`, how many carry a category that still wants confirming.
    provisional: int = 0
    still_unresolved: int = 0
    skipped_manual: int = 0
    # Rows given a place in the category tree. Counted separately from
    # `categorized` because the two can differ in both directions: a row the
    # flat classifier abstained on can still land in the tree (an ATM
    # withdrawal), and a row it labelled can fail to map if its label names a
    # payment rail rather than a purpose.
    hierarchy_placed: int = 0
    by_method: Dict[str, int] = field(default_factory=dict)
    unresolved_samples: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "examined": self.examined,
            "categorized": self.categorized,
            "provisional": self.provisional,
            "still_unresolved": self.still_unresolved,
            "skipped_manual": self.skipped_manual,
            "hierarchy_placed": self.hierarchy_placed,
            "by_method": self.by_method,
            "unresolved_samples": self.unresolved_samples,
        }


def _candidates(db: Session, user_id, account_id=None, force: bool = False):
    """Rows eligible for re-categorisation.

    Default scope is deliberately narrow: rows with no category, or that a
    classifier flagged for review, or that were explicitly labelled
    Uncategorized. `force` widens it to every row EXCEPT manual decisions, which
    are never in scope at any setting.
    """
    q = (
        db.query(Transaction, Prediction)
        .outerjoin(Prediction, Prediction.transaction_id == Transaction.id)
        .filter(
            Transaction.user_id == user_id,
            Transaction.superseded_by_id.is_(None),
        )
    )
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    if not force:
        q = q.filter(
            or_(
                Transaction.category_id.is_(None),
                Prediction.requires_review.is_(True),
                Prediction.predicted_category == "Uncategorized",
            )
        )
    return q.all()


def recategorize(
    db: Session,
    user_id,
    account_id=None,
    force: bool = False,
    sample_limit: int = 25,
) -> RecategorizeResult:
    """Re-classify unresolved rows in place and report what changed."""
    result = RecategorizeResult()
    cat_map = seed_categories(db)
    tree_map = seed_category_tree(db)
    memory = load_memory(db, user_id)

    # Account type per account, not per run. A whole-user re-categorisation
    # passes no account_id, and reading the type from the run would then leave
    # every row looking like a personal account — which is exactly the
    # distinction the trade-name rule needs to tell a restaurant's fish
    # SUPPLIER from a household's grocery shopping.
    account_types = {}
    try:
        from app.models.account import Account
        for acct in db.query(Account).filter(Account.user_id == user_id).all():
            account_types[acct.id] = getattr(acct, "account_type", None)
    except Exception:  # pragma: no cover - a missing account is not fatal
        account_types = {}
    now = datetime.now(timezone.utc)

    rows = _candidates(db, user_id, account_id, force=force)
    result.examined = len(rows)

    for tx, pred in rows:
        # A human decision is final. Not even force overrides it.
        if pred is not None and pred.classification_method == METHOD_MANUAL:
            result.skipped_manual += 1
            continue

        narration = tx.narration_clean or tx.narration_raw or ""
        if not narration:
            result.still_unresolved += 1
            continue

        amount_paise = int(tx.debit_paise or tx.credit_paise or 0)
        account_type = account_types.get(tx.account_id)
        outcome = classify_transaction(
            narration=narration,
            amount=amount_paise / 100.0,
            direction="CREDIT" if tx.credit_paise else "DEBIT",
            account_type=account_type,
        )

        method = outcome.classification_method
        confidence = outcome.classification_confidence
        explanation = outcome.explanation
        category_name = outcome.category

        mem_hit = lookup(memory, narration)

        # The rules read narration shape; they cannot know what business the
        # other party is in. Only consulted when the engine abstained, so a rule
        # that actually fired keeps its answer.
        # Same rule as ingestion: a saved decision beats a trade guess. A
        # keyword must never overrule a person who already answered.
        if memory_should_override(outcome.classification_rule, outcome.requires_review):
            hit = mem_hit
            if hit:
                category_name = hit.category
                method = METHOD_COUNTERPARTY
                confidence = 0.95
                explanation = (
                    f"Categorised as {hit.category} because you have already "
                    f"categorised {hit.display} that way "
                    f"({hit.times_confirmed} time"
                    f"{'s' if hit.times_confirmed != 1 else ''})."
                )
                outcome.requires_review = False

        # PROVISIONAL ANSWERS ARE WRITTEN, NOT DISCARDED.
        #
        # The engine has three outcomes, not two: confident, provisional (a
        # narration pattern matched, but the evidence is weaker than an exact
        # merchant rule), and nothing. Provisional answers used to be computed
        # and then thrown away — on a real 1,823-row statement that discarded
        # 1,183 answers, 65% of the file, and the user saw them all as
        # uncategorised.
        #
        # A provisional answer now writes its category AND keeps
        # requires_review=True, so the row appears in reports instead of a void
        # while still showing up in the review queue for confirmation. That is
        # what requires_review was always for: "here is a category, please
        # confirm it" — not "there is no category".
        provisional = bool(outcome.requires_review)

        # ---- Hierarchy -------------------------------------------------------
        # Written for EVERY row that reaches here, including the ones the flat
        # classifier gives up on. `ATM WDL 1234` has no flat category the rule
        # engine will commit to, and it still has a perfectly good place in the
        # tree — plus a flow type and a payment rail, which are facts about the
        # row rather than opinions about it. Refusing to record those because a
        # different classifier abstained would be throwing away what we know.
        deep = classify_deep(
            narration,
            direction="credit" if tx.credit_paise else "debit",
            amount=amount_paise / 100.0,
            declared_method=tx.payment_method,
            upstream_category=category_name,
            upstream_confidence=confidence or 0.0,
            upstream_requires_review=provisional,
            memory_category=mem_hit.category if mem_hit else None,
            memory_confirmations=mem_hit.times_confirmed if mem_hit else 0,
            account_type=account_type,
        )
        _deep_node_id, deep_path = resolve_path(db, deep.path, tree_map)

        tx.flow_type = deep.flow_type
        tx.transaction_method = deep.transaction_method
        if deep.merchant:
            tx.merchant = deep.merchant
        if deep.counterparty and not tx.counterparty:
            tx.counterparty = deep.counterparty
        # Written unconditionally. This used to skip rows the tree gave up on,
        # which left `category` NULL and made a quarter of a statement render as
        # `Other / Uncategorized` — a node that no longer exists. The tree now
        # always has a true answer (the rail and the direction are facts even
        # when the purpose is not), so there is nothing left to skip.
        if deep.path:
            tx.category = deep.category
            tx.category_path = deep_path
            tx.category_confidence = round(deep.confidence, 3)
            result.hierarchy_placed += 1

        if not category_name or category_name == "Uncategorized":
            result.still_unresolved += 1
            if len(result.unresolved_samples) < sample_limit:
                result.unresolved_samples.append(narration[:120])
            continue

        canonical = normalize_category_name(category_name)
        cat_id = cat_map.get(canonical.lower()) or cat_map.get(category_name.lower())
        if not cat_id:
            # The classifier named a category with no row behind it. Writing the
            # prediction anyway would leave category_id NULL while the UI reports
            # the row as handled — the exact mismatch that stranded rows before.
            logger.warning(
                "[Recategorize] '%s' has no Category row; leaving %s unresolved",
                canonical, tx.id,
            )
            result.still_unresolved += 1
            continue

        # The flat axis. `tx.category` above is the tree's level 1 and is
        # written separately; the two are different questions, and `category_id`
        # answers the flat one because that is what every existing reader
        # resolves a category name through.
        tx.category_id = cat_id
        tx.legacy_category = canonical

        if pred is None:
            pred = Prediction(
                id=uuid.uuid4(),
                transaction_id=tx.id,
                predicted_category=canonical,
                confidence=confidence or 0.0,
            )
            db.add(pred)

        pred.category_id = cat_id
        pred.predicted_category = canonical
        pred.confidence = confidence or 0.0
        pred.classification_method = method
        pred.requires_review = provisional
        # Only a settled row is stamped as reviewed. A provisional one has not
        # been looked at by anyone, and claiming otherwise would hide it.
        pred.reviewed_at = None if provisional else now
        pred.explanation = (
            f"{explanation} This is a suggestion from the narration and has not "
            f"been confirmed — open it in the review queue to accept or change it."
            if provisional else explanation
        )

        result.categorized += 1
        if provisional:
            result.provisional += 1
        key = (method or "unknown") + ("_provisional" if provisional else "")
        result.by_method[key] = result.by_method.get(key, 0) + 1

    db.flush()
    logger.info(
        "[Recategorize] user=%s account=%s force=%s: examined=%d categorized=%d "
        "(%d provisional) still_unresolved=%d skipped_manual=%d",
        user_id, account_id, force, result.examined, result.categorized,
        result.provisional, result.still_unresolved, result.skipped_manual,
    )
    return result
