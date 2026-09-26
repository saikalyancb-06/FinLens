"""ONE definition of "this row still needs a person".

WHY THIS FILE EXISTS, AND IT IS A BUG REPORT.

The Categories page said **221 counterparties to decide**. The Review Queue, on
the same data, in the same session, said **0**. Both were reading their own
column and neither was wrong on its own terms:

    Review Queue   ->  the FLAT axis:  Prediction.requires_review,
                                       Transaction.category_id IS NULL,
                                       Prediction.predicted_category = 'Uncategorized'
    Categories     ->  the TREE axis:  Transaction.category_confidence

Those are two different classifiers with two different opinions, and nothing
made them agree. A row the flat engine answered confidently — often from a
saved counterparty decision, or from a rail-shaped category like "UPI Transfer"
that names how the money moved rather than what it was for — disappears from the
queue while the tree still has no purpose for it. The user is then told there is
work to do and shown an empty screen to do it on.

So the predicate lives here, once, in two spellings of the same rule (Python for
row objects, SQLAlchemy for queries), and every screen imports it. If they ever
disagree again it will be because someone changed this file, which is the point.

THE RULE IS A UNION, DELIBERATELY.

A row needs a person if EITHER axis is unsure. Not both. An intersection would
let a false confidence on one side silence a real doubt on the other, which is
exactly the failure above — just pointing the other way.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import and_, or_

from app.categorization.deep import REVIEW_THRESHOLD
from app.categorization.taxonomy import UNCATEGORIZED as FLAT_UNCATEGORIZED

__all__ = [
    "REVIEW_THRESHOLD",
    "needs_decision",
    "needs_decision_clause",
    "purpose_is_established",
    "decision_weight",
]


def purpose_is_established(txn: Any) -> bool:
    """Does the TREE say what this money was for, or only how it moved?

    `Cash > ATM Withdrawal` is a true statement about a row reading `SELF 4471`
    and it is not a purpose. The residual placements carry a confidence below
    the review threshold precisely so this question has an answer.
    """
    conf = getattr(txn, "category_confidence", None)
    if conf is not None:
        return float(conf) >= REVIEW_THRESHOLD
    # A legacy row with no confidence recorded is judged the old way: it counts
    # as settled if it carries any category at all.
    return bool(getattr(txn, "category", None) or getattr(txn, "legacy_category", None))


def needs_decision(txn: Any, pred: Optional[Any] = None) -> bool:
    """The single question both screens ask.

    `pred` is optional because not every caller has joined the prediction; when
    it is absent only the tree axis is consulted, which is the conservative
    half — it can say "needs a person" when the flat engine had already
    resolved it, but it can never wrongly say "settled".
    """
    if not purpose_is_established(txn):
        return True
    if getattr(txn, "category_id", None) is None:
        return True
    if pred is not None:
        if getattr(pred, "requires_review", False):
            return True
        if getattr(pred, "predicted_category", None) == FLAT_UNCATEGORIZED:
            return True
    return False


def needs_decision_clause(transaction_model, prediction_model):
    """The same rule as a SQL expression, for `.filter(...)`.

    Written against an OUTER join, so every prediction test has to survive
    `pred IS NULL`. `col == True` is NULL rather than false for a missing row,
    which is why the tree-side tests carry the weight here.
    """
    T, P = transaction_model, prediction_model
    return or_(
        # --- the flat axis ---
        P.requires_review.is_(True),
        P.predicted_category == FLAT_UNCATEGORIZED,
        T.category_id.is_(None),
        # --- the tree axis ---
        T.category_confidence < REVIEW_THRESHOLD,
        and_(T.category_confidence.is_(None), T.category.is_(None)),
    )


def decision_weight(transaction_count: int, total_paise: int) -> tuple:
    """How much a single decision is worth, for ranking the queue.

    The user's constraint is a hard ceiling of 50 questions and a target of
    30-35, so the queue cannot simply list everything it is unsure about — on a
    real statement that was 221 parties, most of them someone paid once. The
    ones worth asking about are the ones that clear the most rows and the most
    money per answer.

    Rows dominate and money breaks ties, and it is returned as a TUPLE so that
    is exactly what happens — a single ₹9 lakh payment must not outrank a party
    seen forty times, because the forty-times decision is also remembered for
    every future statement. An arithmetic score could not keep that promise:
    paise are large enough to swamp any row multiplier you pick.
    """
    return (int(transaction_count or 0), abs(int(total_paise or 0)))
