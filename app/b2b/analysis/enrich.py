"""Give every canonical row a category, a flow, a rail and a counterparty.

This runs before any aggregate is computed, because almost every figure in the
response is a sum over a category. It reuses the classifiers the product already
ships rather than growing a second, quietly different taxonomy for the API:

* `hybrid.classify_transaction` — the rule + ML decision layer. Its answer is a
  single flat legacy category and, importantly, its own opinion on whether a
  human should look at the row.
* `deep.classify_deep` — places the row in the 22-root hierarchy and returns a
  path, a flow type, a rail and a merchant. This is the authority for
  `category`, `category_path` and `category_confidence`.
* `flow.detect_flow` / `flow.detect_method` — consulted directly so the row's
  flow and rail get the *category-aware* answer, which `classify_deep` computes
  before it knows the final path.

Why deep is the authority and hybrid is not: hybrid answers "Uncategorized" with
`requires_review=True` on plenty of rows the tree places confidently — an EMI
line and an ATM withdrawal are both examples. Taking hybrid's abstention as the
final word would drop those rows out of every category total and out of the
classification-coverage term of `overall_confidence`, understating both. Hybrid's
answer is still carried, as `legacy_category` and `classification_rule`, because
it is the auditable rule trail.

`resolve_path` from the deep classifier is deliberately not called: it is that
module's only database dependency. The path is carried as a `" > "`-joined
string instead, which is what the response returns anyway.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from app.b2b.canonical import CanonicalTxn, from_minor
from app.b2b.metrics import (
    W_CLASSIFIER_DEGRADED,
    W_UNCLASSIFIED_MAJORITY,
    WarningCollector,
)
from app.b2b.analysis.util import narration_of

logger = logging.getLogger(__name__)

# A row is "placed" when the tree is at least as sure of it as the deep
# classifier's own review threshold (0.60). Below that the classifier is saying
# it wants a human, and counting it as classified would inflate coverage.
from app.categorization.deep import REVIEW_THRESHOLD  # noqa: E402

PLACED_THRESHOLD = REVIEW_THRESHOLD


def enrich(txns: List[CanonicalTxn], warnings: WarningCollector) -> None:
    """Classify every row in place. Returns nothing; mutates the rows."""
    if not txns:
        return

    from app.categorization.deep import classify_deep
    from app.categorization.flow import detect_flow, detect_method
    from app.categorization.hierarchy import format_path
    from app.categorization.hybrid import classify_transaction

    ml_degraded = _ml_is_degraded()
    hybrid_failures = 0

    for idx, t in enumerate(txns):
        narration = narration_of(t)
        amount_major = from_minor(t.amount_paise)
        direction = t.direction

        # ---- hybrid: the auditable rule trail, and the legacy flat category
        hybrid = None
        try:
            hybrid = classify_transaction(narration, amount=amount_major,
                                          direction=direction)
        except Exception as exc:  # pragma: no cover - defensive
            # A model file that will not load must not take the whole analysis
            # down. The tree classifier below is pure Python and always answers.
            hybrid_failures += 1
            logger.warning("[b2b] hybrid classifier failed on row %s: %s", idx, exc)

        # ---- deep: the hierarchy path, and the authority for the category
        deep = classify_deep(
            narration,
            direction=direction,
            amount=amount_major,
            declared_method=t.transaction_method,
            upstream_category=(hybrid.category if hybrid else None),
            upstream_confidence=(hybrid.classification_confidence if hybrid else 0.0),
            upstream_requires_review=(hybrid.requires_review if hybrid else False),
        )

        # ---- flow and rail, now that the path is known
        flow = detect_flow(direction, narration, category_path=deep.path)
        method = detect_method(narration, t.transaction_method)

        t.category_path = format_path(deep.path) if deep.path else None
        t.category = deep.path[0] if deep.path else None
        t.category_confidence = round(float(deep.confidence), 4)
        t.legacy_category = hybrid.category if hybrid else None
        t.flow_type = flow.flow_type
        t.transaction_method = method.method
        t.merchant = deep.merchant or t.merchant
        t.counterparty = deep.counterparty or t.counterparty
        t.requires_review = bool(deep.confidence < PLACED_THRESHOLD)
        t.classification_rule = hybrid.classification_rule if hybrid else None
        t.classification_method = _method_label(deep, hybrid)

        # The compliance detectors fingerprint their findings on the
        # transaction id. Canonical rows arrive with `id=None` because nothing
        # was persisted, and a None id collapses every finding of one type onto
        # a single fingerprint — three separate bounce charges become one. A
        # stable synthetic id per row fixes that without inventing an account.
        if t.id is None:
            t.id = f"row-{t.row_index if t.row_index is not None else idx}"

    if ml_degraded or hybrid_failures:
        warnings.add(
            W_CLASSIFIER_DEGRADED,
            "The machine-learning classifier was unavailable; categories came "
            "from deterministic rules and the category tree alone. Rule-based "
            "answers are unaffected, but rows that only the model would have "
            "recognised are marked for review.",
            severity="warning",
            detail={"ml_available": not ml_degraded,
                    "hybrid_failures": hybrid_failures},
        )

    unplaced = sum(1 for t in txns if t.requires_review)
    if unplaced * 2 > len(txns):
        warnings.add(
            W_UNCLASSIFIED_MAJORITY,
            f"{unplaced} of {len(txns)} rows could not be placed in the category "
            "tree with confidence. Category totals below cover a minority of the "
            "statement and should not be read as complete.",
            severity="critical",
            detail={"unplaced": unplaced, "total": len(txns)},
        )


def classification_coverage(txns: Sequence[CanonicalTxn]) -> Optional[float]:
    """Share of rows the tree placed at or above its own review threshold."""
    if not txns:
        return None
    placed = sum(1 for t in txns
                 if (t.category_confidence or 0.0) >= PLACED_THRESHOLD)
    return placed / len(txns)


def _method_label(deep, hybrid) -> str:
    """How this row's category was decided, in one string.

    Deep's `source` ("concept", "merchant", "memory", "rule", ...) when the tree
    was decisive, because that is what actually produced the category that got
    used. Hybrid's method otherwise, so a row that only the rules or the model
    reached is still attributable.
    """
    if deep is not None and deep.confidence >= PLACED_THRESHOLD:
        return f"tree:{deep.source}"
    if hybrid is not None:
        return hybrid.classification_method
    return "none"


def _ml_is_degraded() -> bool:
    """Is the ML half of the hybrid classifier actually usable?

    Asked once per run rather than per row: `ml_service.is_available()` is cheap
    but the answer cannot change mid-analysis, and a per-row check would emit the
    same warning thousands of times.
    """
    try:
        from app.categorization.ml_service import ml_service
        return not bool(ml_service.is_available)
    except Exception:  # pragma: no cover - import-time failure is itself degraded
        return True
