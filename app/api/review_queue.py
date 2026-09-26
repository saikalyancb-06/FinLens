"""Review queue: surface and correct transactions the classifier could not decide.

Before this module there was no way to see or fix an uncategorised transaction.
The /review-queue page in the UI only listed deduplication and reconciliation
matches, and the transactions API had no endpoint for changing a category at
all, so anything the classifier abstained on was invisible and unfixable.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.categorization.hybrid import METHOD_MANUAL, classify_transaction
from app.categorization.counterparty import (
    group_for as group_for_review,
    grouping_health,
)
from app.categorization.counterparty_memory import (
    forget as forget_counterparty_memory,
    remember as remember_counterparty,
    suggest_merges as suggest_counterparty_merges,
)
from app.categorization.dual_taxonomy import (
    EVENT_TYPES, PURPOSES, is_valid_event_type, is_valid_purpose,
)
from app.categorization.decisions import (
    decision_weight, needs_decision_clause,
)
from collections import OrderedDict

from app.entity_resolution import EntityMention, cluster_narrations
from app.models.entity_link import EntityLink
from app.categorization.taxonomy import CATEGORIES, UNCATEGORIZED, normalize_category
from app.database.session import get_db
from app.models.category import Category
from app.services.category_seeder import apply_decision
from app.models.counterparty_memory import CounterpartyMemory
from app.models.prediction import Prediction
from app.models.transaction import Transaction
from app.models.user import User
from app.services.recategorize import recategorize as recategorize_transactions
from app.utils.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/review-queue", tags=["Categorization Review Queue"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ReviewItem(BaseModel):
    transaction_id: uuid.UUID
    account_id: Optional[uuid.UUID] = None
    txn_date: Optional[datetime.date] = None
    narration: str
    amount: float
    direction: str
    current_category: str

    category: Optional[str] = None
    # Mirrors `category`. Kept populated so an older client that reads `purpose`
    # keeps working; new code should read `category`.
    purpose: Optional[str] = None
    event_type: Optional[str] = None
    classification_method: Optional[str] = None
    classification_confidence: Optional[float] = None
    rule_score: Optional[int] = None
    model_name: Optional[str] = None
    top_3: Optional[List[Any]] = None
    # `explanation` was removed from this response. The classifier's reasoning
    # is still written to `Prediction.explanation` at ingest and is still read
    # by the classification-health view — it just is not part of the review
    # queue's payload, because the screen no longer shows it and shipping a
    # paragraph per row to render nothing is cost with no reader.
    suggestions: List[Dict[str, Any]] = Field(default_factory=list)

    # Who the row is with, rather than the string the bank printed.
    #
    # `narration` is kept — this is a financial tool and the reviewer must be
    # able to see the verbatim text and its reference number — but it is not
    # what a person recognises. "NEFT-YESAP50900722527-RESILIENT INNOVATIONS
    # PVT LT" and the same supplier's next payment differ only in the reference,
    # so a queue that leads with the narration reads as a list of unrelated
    # strangers when it is really one supplier twice.
    #
    # These come from the SAME `group_for()` used by the counterparty view, so
    # the label a row shows here is exactly the group it would be decided in
    # there. Two screens deriving the name two ways is how they drift.
    counterparty: Optional[str] = None      # display name, or the charge kind
    group_key: Optional[str] = None         # what a decision would collapse on
    group_kind: Optional[str] = None        # 'counterparty' | 'pattern'
    group_channel: Optional[str] = None     # rail the name was read off: neft/upi/...
    group_size: Optional[int] = None        # rows in the queue sharing that key


class ReclassifyRequest(BaseModel):
    # Two-axis labelling. `category` is what the money was for and leads reports;
    # `event_type` is what kind of business event and may legitimately be absent
    # (a bank fee has a category but no commercial counterparty).
    category: Optional[str] = Field(None, description="One of the canonical categories")
    event_type: Optional[str] = Field(None, description="Optional business event type")
    # Accepted as an alias so callers written against the older field name keep
    # working. `category` wins if both are sent.
    purpose: Optional[str] = Field(None, description="Deprecated alias for category")
    note: Optional[str] = None
    # Teach the counterparty memory from this decision, and apply it to the
    # other rows naming the same party that are still awaiting review. Default
    # True because the whole point of reviewing KUMAR FISH once is not having
    # to review the other eleven; pass False to categorise this row alone.
    apply_to_similar: bool = Field(
        True, description="Also apply this category to unreviewed rows with the same counterparty",
    )


class ReclassifyResponse(BaseModel):
    transaction_id: uuid.UUID
    previous_category: str
    new_category: str
    classification_method: str
    reviewed_at: datetime.datetime
    # What the decision taught, and what it cleared. Both are reported so the
    # UI can say "also categorised 11 other KUMAR FISH transactions" rather
    # than silently changing rows the user did not look at.
    counterparty: Optional[str] = None
    also_updated: int = 0


class QueueSummary(BaseModel):
    total_requiring_review: int
    uncategorized: int
    # The queue holds two different jobs and they must not be shown as one
    # number. `needs_category` rows have NO category and are the real backlog;
    # `awaiting_confirmation` rows already carry a category the engine derived
    # from the narration and only want a yes/no. Lumping them together made a
    # run that categorised 1,224 rows look like it had done nothing.
    needs_category: int = 0
    awaiting_confirmation: int = 0
    low_confidence: int
    # How many DISTINCT parties the queue is asking about, and how many of them
    # the screen will actually show. These differ on purpose: past about 50
    # questions a person leaves, so the list is capped and the remainder are
    # left at the placement the classifier could defend. Reporting both is what
    # keeps that a stated trade-off instead of a silent truncation.
    counterparties_total: int = 0
    counterparties_shown: int = 0
    # Split, because they are not the same job and the tile was showing their
    # sum. A group where every row already carries a category wants a nod; a
    # group with no category at all is the actual backlog. Showing 135 when 107
    # of them are nods reads as "you have 135 questions" and is why the ceiling
    # looked breached when it was not.
    counterparties_needing_category: int = 0
    counterparties_confirm_only: int = 0
    by_method: Dict[str, int]
    # The business taxonomy — Sales Income, Cost of Goods, Bank Fees. This
    # previously carried the LEGACY personal-finance list (Groceries, Shopping,
    # Entertainment), which is why the UI had to prefer a second field to avoid
    # offering a restaurant "Entertainment" for a supplier payment. One list now.
    available_categories: List[str] = Field(default_factory=list)
    # Mirrors `available_categories` for clients written against the old name.
    available_purposes: List[str] = Field(default_factory=list)
    available_event_types: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _category_name(db: Session, tx: Transaction, pred: Optional[Prediction]) -> str:
    if tx.category_id:
        cat = db.query(Category).filter(Category.id == tx.category_id).first()
        if cat:
            return cat.name
    if pred and pred.predicted_category:
        return pred.predicted_category
    return UNCATEGORIZED


def _amount_of(tx: Transaction) -> float:
    if tx.debit_paise:
        return tx.debit_paise / 100.0
    if tx.credit_paise:
        return tx.credit_paise / 100.0
    return 0.0


# Past roughly this many questions a person abandons the screen, so this is
# where the counterparty list is cut. Named once and shared by the endpoint and
# the summary so they can never disagree about what was shown.
COUNTERPARTY_PAGE = 50


def _entity_decisions(db: Session, user: User):
    """STAGE 15. Every merge a person has confirmed or rejected, for the resolver.

    Both directions are loaded. Without the rejections the resolver re-derives
    the same medium-confidence suggestion on every upload and asks a question
    the user has already answered — which reads as the feature being broken.
    """
    same: List[tuple] = []
    different: List[tuple] = []
    for link in db.query(EntityLink).filter(EntityLink.user_id == user.id).all():
        (same if link.same else different).append((link.key_a, link.key_b))
    return same, different


# Resolution is the expensive step on this screen: 760 pending rows take ~1.6s,
# and the page calls it twice (once for the tiles, once for the list). A small
# cache keyed on the exact input makes the second call free and a page refresh
# free, without ever serving a stale answer — the key includes every narration
# and every saved merge decision, so any change to either misses.
_PARTY_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_PARTY_CACHE_MAX = 8


def _resolve_parties(db: Session, user: User, rows):
    """One party per real-world entity, not one per spelling.

    Replaces the old per-narration counterparty key. On a real statement that
    key produced nine groups for one chicken supplier and eleven for one monthly
    POS charge, because it compared strings and the bank writes the same party
    differently every month. The resolver compares ENTITIES.

    Returns a lookup from raw narration to the cluster it belongs to, plus the
    resolution report so callers can surface the medium-confidence suggestions.
    """
    mentions = []
    for tx, _pred in rows:
        narration = tx.narration_clean or tx.narration_raw or ""
        if not narration:
            continue
        mentions.append(EntityMention(
            raw=narration,
            row_id=str(tx.id),
            direction="credit" if tx.credit_paise else "debit",
            method=getattr(tx, "transaction_method", None),
            utr=getattr(tx, "reference_no", None),
            amount=(tx.debit_paise or tx.credit_paise or 0) / 100.0,
            date=tx.txn_date,
        ))
    known_same, known_different = _entity_decisions(db, user)

    cache_key = (
        str(user.id),
        hash(tuple(m.raw for m in mentions)),
        hash(tuple(sorted(known_same))),
        hash(tuple(sorted(known_different))),
    )
    cached = _PARTY_CACHE.get(cache_key)
    if cached is not None:
        _PARTY_CACHE.move_to_end(cache_key)
        return cached

    result = cluster_narrations(mentions, known_same=known_same,
                                known_different=known_different)
    _PARTY_CACHE[cache_key] = result
    while len(_PARTY_CACHE) > _PARTY_CACHE_MAX:
        _PARTY_CACHE.popitem(last=False)
    return result


def _review_query(db: Session, user: User, account_id: Optional[uuid.UUID]):
    """Transactions needing human attention.

    THE FILTER LIVES IN `app.categorization.decisions`, NOT HERE, AND THAT IS
    THE WHOLE POINT. This function used to spell out its own condition against
    the FLAT axis — `Prediction.requires_review`, a NULL `category_id`, the
    `Uncategorized` sentinel — while the Categories page asked the TREE axis
    (`category_confidence`). On real data that produced a screen saying "221
    counterparties to decide" next to a queue saying "0", because a row can be
    answered confidently by the flat engine and still have no purpose in the
    tree. Sharing one predicate is what stops that happening again.
    """
    q = (
        db.query(Transaction, Prediction)
        .outerjoin(Prediction, Prediction.transaction_id == Transaction.id)
        .filter(
            Transaction.user_id == user.id,
            Transaction.superseded_by_id == None,
        )
    )
    if account_id:
        q = q.filter(Transaction.account_id == account_id)

    return q.filter(needs_decision_clause(Transaction, Prediction))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/summary", response_model=QueueSummary)
def get_queue_summary(
    account_id: Optional[uuid.UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Counts for the review queue badge and dashboard tile."""
    rows = _review_query(db, current_user, account_id).all()

    by_method: Dict[str, int] = {}
    uncategorized = 0
    low_confidence = 0

    needs_category = 0
    awaiting_confirmation = 0
    parties = set()
    parties_needing = set()
    # Resolved ENTITIES, not narration spellings. Same call the counterparty
    # screen makes, so the tile and the list cannot disagree about the count.
    party_of, _report = _resolve_parties(db, current_user, rows)
    for tx, pred in rows:
        method = (pred.classification_method if pred else None) or "unclassified"
        by_method[method] = by_method.get(method, 0) + 1
        narration = tx.narration_clean or tx.narration_raw or ""
        assignment = party_of.get(narration.strip())
        if assignment:
            parties.add(assignment.cluster_key)
            # One row with no category anywhere in the group makes the whole
            # group a real question rather than a confirmation.
            if not tx.category_id:
                parties_needing.add(assignment.cluster_key)
        if not tx.category_id:
            uncategorized += 1
            needs_category += 1
        else:
            awaiting_confirmation += 1
        if pred and pred.requires_review:
            low_confidence += 1

    return QueueSummary(
        total_requiring_review=len(rows),
        uncategorized=uncategorized,
        needs_category=needs_category,
        awaiting_confirmation=awaiting_confirmation,
        low_confidence=low_confidence,
        counterparties_total=len(parties),
        counterparties_shown=min(len(parties), COUNTERPARTY_PAGE),
        counterparties_needing_category=len(parties_needing),
        counterparties_confirm_only=len(parties) - len(parties_needing),
        by_method=by_method,
        available_categories=PURPOSES,
        available_purposes=PURPOSES,
        available_event_types=EVENT_TYPES,
    )


@router.get("", response_model=List[ReviewItem])
@router.get("/", response_model=List[ReviewItem])
def list_review_queue(
    account_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_suggestions: bool = Query(True, description="Run the classifier to propose categories"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List transactions awaiting manual categorisation, newest first."""
    rows = (
        _review_query(db, current_user, account_id)
        .order_by(Transaction.txn_date.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items: List[ReviewItem] = []
    # Group every row first, so each item can say how many others a decision on
    # it would also settle. `group_for` is the same function the counterparty
    # view groups on, so the two screens cannot disagree about what a row is.
    groups_by_txn: Dict[Any, Any] = {}
    group_counts: Dict[str, int] = {}
    for tx, _pred in rows:
        _narr = tx.narration_clean or tx.narration_raw or ""
        try:
            grp = group_for_review(_narr)
        except Exception:          # noqa: BLE001 - never break the queue over a label
            grp = None
        groups_by_txn[tx.id] = grp
        if grp is not None:
            group_counts[grp.group_key] = group_counts.get(grp.group_key, 0) + 1

    for tx, pred in rows:
        narration = tx.narration_clean or tx.narration_raw or ""
        amount = _amount_of(tx)
        grp = groups_by_txn.get(tx.id)

        suggestions: List[Dict[str, Any]] = []
        if include_suggestions:
            # Re-run the classifier live so the reviewer sees the current
            # model's opinion and its reasoning, rather than a stale label.
            result = classify_transaction(
                narration, amount=amount,
                direction=tx.direction.value if tx.direction else None,
            )
            suggestions = [
                {"category": cat, "confidence": round(prob, 4)}
                for cat, prob in result.top_3
            ]
            if not suggestions and result.rule_category:
                suggestions = [{"category": result.rule_category,
                                "confidence": result.classification_confidence}]

        items.append(ReviewItem(
            transaction_id=tx.id,
            account_id=tx.account_id,
            txn_date=tx.txn_date,
            narration=narration,
            amount=amount,
            direction=tx.direction.value if tx.direction else "debit",
            current_category=_category_name(db, tx, pred),
            category=tx.legacy_category,
            purpose=tx.legacy_category,
            event_type=tx.event_type,
            classification_method=pred.classification_method if pred else None,
            classification_confidence=pred.confidence if pred else None,
            rule_score=pred.rule_score if pred else None,
            model_name=pred.model_name if pred else None,
            top_3=pred.top_3 if pred else None,
            suggestions=suggestions,
            # A stored counterparty is a decision something already made about
            # this row, so it wins over re-deriving one from the text.
            counterparty=(tx.counterparty or (grp.display if grp else None)),
            group_key=(grp.group_key if grp else None),
            group_kind=(grp.kind if grp else None),
            group_channel=(grp.channel if grp else None),
            group_size=(group_counts.get(grp.group_key) if grp else None),
        ))

    return items


METHOD_COUNTERPARTY = "counterparty_memory"


def _apply_to_similar(
    db: Session,
    *,
    user: User,
    counterparty_key: str,
    # The ORM row and the name are both needed: the row for the foreign key, the
    # name for the denormalised string column and the explanation text.
    category_row,
    category_name: str,
    event_type: Optional[str],
    exclude_txn_id: Optional[uuid.UUID],
    now: datetime.datetime,
    reviewer_id: uuid.UUID,
) -> int:
    """Apply one review decision to the user's other rows naming the same party.

    Scope is deliberately narrow. Only rows that are STILL IN THE REVIEW QUEUE
    are touched: a transaction the user (or a rule) already categorised is a
    settled decision, and a bulk action taken on a different row must not
    silently overwrite it. The recorded method is `counterparty_memory`, not
    `manual`, so the audit trail distinguishes "the user chose this row" from
    "this row inherited the user's choice about another row".
    """
    rows = _review_query(db, user, account_id=None).all()

    updated = 0
    for other_tx, other_pred in rows:
        if other_tx.id == exclude_txn_id:
            continue
        cp = group_for_review(
            other_tx.narration_clean or other_tx.narration_raw or ""
        )
        if not cp or cp.key != counterparty_key:
            continue

        other_tx.category_id = category_row.id
        apply_decision(db, other_tx, category_name)
        if event_type is not None:
            other_tx.event_type = event_type

        if other_pred is None:
            other_pred = Prediction(
                id=uuid.uuid4(),
                transaction_id=other_tx.id,
                predicted_category=category_row.name,
                confidence=0.95,
            )
            db.add(other_pred)

        other_pred.category_id = category_row.id
        other_pred.predicted_category = category_row.name
        other_pred.confidence = 0.95
        other_pred.classification_method = METHOD_COUNTERPARTY
        other_pred.requires_review = False
        other_pred.reviewed_at = now
        other_pred.reviewed_by_user_id = reviewer_id
        other_pred.explanation = (
            f"Categorised as {category_row.name} from your decision about "
            f"{cp.display}. Change any one of these rows to change the rest."
        )
        updated += 1

    if updated:
        db.flush()
    return updated


@router.patch("/{transaction_id}", response_model=ReclassifyResponse)
def reclassify_transaction(
    transaction_id: uuid.UUID,
    payload: ReclassifyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Assign a category manually and clear the review flag.

    A manual decision is recorded with classification_method='manual' so it is
    never mistaken for a model output and is not overwritten by a future
    reclassification run.
    """
    # `category` is the primary axis; `purpose` is accepted as an alias so
    # callers written against the older field name keep working.
    raw_category = payload.category or payload.purpose
    if not raw_category:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A category is required. Must be one of: {', '.join(PURPOSES)}",
        )

    purpose = raw_category.strip()
    if not is_valid_purpose(purpose):
        # Fall back to the legacy taxonomy so an old client sending
        # "Food & Dining" is still understood rather than rejected.
        legacy = normalize_category(purpose)
        if legacy and legacy != UNCATEGORIZED:
            purpose = legacy
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid category '{raw_category}'. Must be one of: {', '.join(PURPOSES)}",
            )

    event_type = (payload.event_type or "").strip() or None
    if not is_valid_event_type(event_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid event type '{event_type}'. Must be one of: {', '.join(EVENT_TYPES)}",
        )

    canonical = purpose

    tx = db.query(Transaction).filter(
        Transaction.id == transaction_id,
        Transaction.user_id == current_user.id,
    ).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    pred = db.query(Prediction).filter(Prediction.transaction_id == tx.id).first()
    previous = _category_name(db, tx, pred)

    # `name` stopped being unique when this table became a tree — there are
    # three nodes called `Interest`. The review queue's vocabulary is the flat
    # one, so the lookup is scoped to legacy and level-1 rows; without that,
    # `.first()` can return `Loans & Credit > Credit Card > Interest`.
    category = (
        db.query(Category)
        .filter(Category.name == canonical,
                or_(Category.level.is_(None), Category.level == 1))
        .first()
    )
    if not category:
        category = Category(id=uuid.uuid4(), name=canonical)
        db.add(category)
        db.flush()

    tx.category_id = category.id
    apply_decision(db, tx, purpose)
    if event_type is not None:
        tx.event_type = event_type

    now = datetime.datetime.utcnow()
    if pred is None:
        pred = Prediction(
            id=uuid.uuid4(),
            transaction_id=tx.id,
            predicted_category=canonical,
            confidence=1.0,
        )
        db.add(pred)

    pred.category_id = category.id
    pred.predicted_category = canonical
    pred.confidence = 1.0
    pred.classification_method = METHOD_MANUAL
    pred.requires_review = False
    pred.reviewed_at = now
    pred.reviewed_by_user_id = current_user.id
    pred.explanation = (
        f"Manually categorised as {canonical} by user review"
        + (f": {payload.note}" if payload.note else ".")
    )

    # Teach the counterparty memory. A decision about "who was paid" transfers
    # to every other row naming that party; a decision about a bank charge does
    # not, and remember() returns None for those rather than storing a key that
    # would later misfire.
    counterparty_name: Optional[str] = None
    also_updated = 0
    memory_row = remember_counterparty(
        db,
        current_user.id,
        tx.narration_clean or tx.narration_raw or "",
        category=purpose,
        event_type=event_type,
        source="manual",
    )
    if memory_row is not None:
        counterparty_name = memory_row.display_name
        if payload.apply_to_similar:
            also_updated = _apply_to_similar(
                db,
                user=current_user,
                counterparty_key=memory_row.counterparty_key,
                category_row=category,
                category_name=purpose,
                event_type=event_type,
                exclude_txn_id=tx.id,
                now=now,
                reviewer_id=current_user.id,
            )

    db.commit()

    logger.info(
        f"[Review Queue] User '{current_user.id}' reclassified transaction {tx.id}: "
        f"'{previous}' -> '{canonical}'"
        + (f"; counterparty '{counterparty_name}' learned, {also_updated} similar "
           f"row(s) updated" if counterparty_name else "")
    )

    return ReclassifyResponse(
        transaction_id=tx.id,
        previous_category=previous,
        new_category=canonical,
        classification_method=METHOD_MANUAL,
        reviewed_at=now,
        counterparty=counterparty_name,
        also_updated=also_updated,
    )


@router.get("/categories", response_model=List[str])
def list_categories():
    """The canonical category list the UI should offer in its dropdown.

    Returns the BUSINESS taxonomy (Sales Income, Cost of Goods, Bank Fees), not
    the legacy personal-finance one (Groceries, Shopping, Entertainment). The
    legacy list is still understood on input for old data, but offering it here
    let a restaurant file a supplier payment under "Entertainment".
    """
    return PURPOSES


class RecategorizeResponse(BaseModel):
    examined: int
    categorized: int
    provisional: int = 0
    still_unresolved: int
    skipped_manual: int
    by_method: Dict[str, int] = Field(default_factory=dict)
    unresolved_samples: List[str] = Field(default_factory=list)


@router.post("/recategorize", response_model=RecategorizeResponse)
def recategorize_existing(
    account_id: Optional[uuid.UUID] = Query(None),
    force: bool = Query(
        False,
        description="Also re-examine rows that already carry a category. "
                    "Manual decisions are never overwritten at any setting.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-run categorisation over transactions already in the ledger.

    A transaction keeps the category it was given at upload time, so an
    improvement to the rules only ever helped the NEXT upload — a statement
    imported before a rule existed stayed uncategorised, and the only way to
    benefit was to clear the account and re-import, destroying every review
    decision already made.

    This applies the current rules AND the counterparty memory in place. Rows a
    human decided on are skipped and reported separately, so a re-run can never
    cost the user their review work.
    """
    result = recategorize_transactions(
        db, current_user.id, account_id=account_id, force=force
    )
    db.commit()
    return RecategorizeResponse(**result.as_dict())


@router.get("/grouping-health")
def review_grouping_health(
    account_id: Optional[uuid.UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """How well this statement's narration format is understood.

    Coverage alone does not answer that question. The shape fallback groups
    whatever the counterparty patterns miss, so coverage stays near 100% on a
    bank this code has never seen — while every supplier on it is being grouped
    by narration text and appearing in the queue several times.

    This reports the signal that actually degrades: how many rows are grouped by
    shape when they look like they name somebody. A statement that trips it is
    one to tell the user about rather than quietly hand them bad groups.
    """
    rows = _review_query(db, current_user, account_id).all()
    narrations = [
        (tx.narration_clean or tx.narration_raw or "") for tx, _pred in rows
    ]
    return grouping_health(narrations).as_dict()


# ---------------------------------------------------------------------------
# Counterparty grouping
#
# The review queue lists TRANSACTIONS, but the decision a user actually makes is
# about a PARTY: once they know KUMAR FISH is a food supplier, all twelve of
# those rows are settled. Reviewing row-by-row asks the same question twelve
# times. These endpoints ask it once.
# ---------------------------------------------------------------------------


class CounterpartyGroup(BaseModel):
    counterparty_key: str
    display_name: str
    # 'counterparty' — a party the user trades with.
    # 'pattern'      — a shape of narration with no counterparty (a bank charge,
    #                  POS rent, interest). Grouped so 273 identical charge rows
    #                  are ONE decision instead of 273.
    kind: str = "counterparty"
    fuzzy_key: Optional[str] = None
    # The other spellings folded into this group. `SUSHMITA H SHETTY` and
    # `SUSHMITHA SHETTY` are one person written three ways, and asking three
    # times is three chances for someone to abandon the queue. Listed so the
    # merge is visible: if two genuinely different parties collapsed together,
    # this is where it shows, before a decision is applied to both.
    also_known_as: List[str] = []
    channel: str
    transaction_count: int
    # Of transaction_count, how many carry NO category at all. A group where
    # this is zero is only awaiting confirmation, not a blank.
    uncategorized_count: int = 0
    # The category the group's rows already carry, when they agree on one.
    # Shown so the user confirms or corrects a real suggestion instead of
    # choosing blind, and so they can see a group is already handled.
    current_category: Optional[str] = None
    credit_count: int
    debit_count: int
    total_credit: float
    total_debit: float
    first_seen: Optional[datetime.date] = None
    last_seen: Optional[datetime.date] = None
    sample_narrations: List[str]
    suggested_category: Optional[str] = None
    # What the user already decided about this party, if anything.
    known_category: Optional[str] = None


class BulkCategorizeRequest(BaseModel):
    category: Optional[str] = Field(None, description="One of the canonical categories")
    # Accepted as an alias for older callers. `category` wins if both are sent.
    purpose: Optional[str] = Field(None, description="Deprecated alias for category")
    event_type: Optional[str] = None
    note: Optional[str] = None


class BulkCategorizeResponse(BaseModel):
    counterparty_key: str
    display_name: str
    category: str
    # Mirrors `category` for clients written against the old name.
    purpose: str
    transactions_updated: int


@router.get("/counterparties", response_model=List[CounterpartyGroup])
def list_counterparty_groups(
    account_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(COUNTERPARTY_PAGE, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pending review items collapsed to one row per counterparty.

    The default limit is 50 because that is the point past which a person
    abandons the screen. `/summary` reports how many groups exist in total, so
    the UI can say what was left out rather than quietly truncating.

    Sorted by transaction count, so the party that clears the most rows per
    decision is at the top. Rows whose narration yields no counterparty (bank
    charges, cash movements) are excluded — those are the rule engine's job and
    grouping them by name would be meaningless.
    """
    rows = _review_query(db, current_user, account_id).all()

    memory = {
        m.counterparty_key: m
        for m in db.query(CounterpartyMemory)
        .filter(CounterpartyMemory.user_id == current_user.id)
        .all()
    }

    # ENTITY RESOLUTION, not string grouping. The old key compared narrations,
    # so one chicken supplier arrived as nine questions and one monthly POS
    # charge as eleven — the bank writes the same party differently every time.
    # This resolves the spellings into parties first and asks once per party.
    party_of, _report = _resolve_parties(db, current_user, rows)

    groups: Dict[str, Dict[str, Any]] = {}
    for tx, _pred in rows:
        narration = tx.narration_clean or tx.narration_raw or ""
        # Counterparty first; narration shape when there is none. Only a row
        # that is nothing but a reference number falls through, and there is
        # genuinely nothing to group it on.
        cp = group_for_review(narration)
        if not cp:
            continue
        assignment = party_of.get(narration.strip())

        # Grouped on `group_key`, which folds spelling variants together —
        # `SUSHMITHA H SHETTY`, `SUSHMITA H SHETTY` and `SUSHMITHA SHETTY` are
        # one person and were three separate questions. `counterparty_key`
        # stays the exact key, because that is what a decision is stored
        # against; the alternates are listed below so a wrong merge is visible
        # before anyone acts on it.
        # Keyed on the resolved ENTITY. `counterparty_key` stays the extractor's
        # key because that is what a saved decision is stored against, and
        # changing it would orphan every answer the user has already given.
        group_key = assignment.cluster_key if assignment else cp.group_key
        g = groups.setdefault(group_key, {
            "counterparty_key": cp.key,
            "display_name": assignment.canonical if assignment else cp.display,
            "spellings": {},
            "kind": cp.kind,
            "fuzzy_key": cp.fuzzy_key,
            "channel": cp.channel,
            "transaction_count": 0,
            "uncategorized_count": 0,
            "categories": set(),
            "credit_count": 0,
            "debit_count": 0,
            "total_credit": 0,
            "total_debit": 0,
            "first_seen": None,
            "last_seen": None,
            "sample_narrations": [],
        })
        g["transaction_count"] += 1
        # Which spelling is the canonical one is decided by weight of rows: the
        # form the bank used most is the form the user will recognise.
        g["spellings"][cp.display] = g["spellings"].get(cp.display, 0) + 1
        if g["spellings"][cp.display] > g["spellings"].get(g["display_name"], 0):
            g["display_name"] = cp.display
            g["counterparty_key"] = cp.key
        if tx.category_id is None:
            g["uncategorized_count"] += 1
        if tx.legacy_category:
            g["categories"].add(tx.legacy_category)
        credit = int(tx.credit_paise or 0)
        debit = int(tx.debit_paise or 0)
        if credit:
            g["credit_count"] += 1
            g["total_credit"] += credit
        if debit:
            g["debit_count"] += 1
            g["total_debit"] += debit
        if tx.txn_date:
            if g["first_seen"] is None or tx.txn_date < g["first_seen"]:
                g["first_seen"] = tx.txn_date
            if g["last_seen"] is None or tx.txn_date > g["last_seen"]:
                g["last_seen"] = tx.txn_date
        if len(g["sample_narrations"]) < 3 and narration not in g["sample_narrations"]:
            g["sample_narrations"].append(narration)

    out: List[CounterpartyGroup] = []
    for g in groups.values():
        known = memory.get(g["counterparty_key"])
        out.append(CounterpartyGroup(
            counterparty_key=g["counterparty_key"],
            display_name=g["display_name"],
            also_known_as=sorted(
                name for name in g["spellings"] if name != g["display_name"]
            ),
            kind=g["kind"],
            fuzzy_key=g["fuzzy_key"],
            channel=g["channel"],
            transaction_count=g["transaction_count"],
            uncategorized_count=g["uncategorized_count"],
            current_category=(
                next(iter(g["categories"])) if len(g["categories"]) == 1 else None
            ),
            credit_count=g["credit_count"],
            debit_count=g["debit_count"],
            # Paise are the storage unit; the API speaks rupees.
            total_credit=round(g["total_credit"] / 100.0, 2),
            total_debit=round(g["total_debit"] / 100.0, 2),
            first_seen=g["first_seen"],
            last_seen=g["last_seen"],
            sample_narrations=g["sample_narrations"],
            known_category=known.category if known else None,
            # The rules' own answer for these rows, when they agree. Not a
            # guess invented here — a real derivation the user can accept or
            # overrule. None when the rows disagree, because presenting one of
            # several answers as "the" suggestion would be misleading.
            suggested_category=(
                next(iter(g["categories"])) if len(g["categories"]) == 1 else None
            ),
        ))

    # RANKED BY WHAT THE ANSWER IS WORTH, then cut to a length a person will
    # actually finish. On the real statement this query found 221 parties;
    # answering 221 questions to file one month is worse than doing it by hand,
    # which is the thing this feature exists to prevent. Most of that tail is
    # someone paid once, and a decision about them clears exactly one row.
    #
    # So: rows the tree could not place at all first, then the parties whose
    # single answer clears the most rows, then the most money. Everything below
    # the cut stays exactly where the classifier honestly put it — filed by how
    # the money moved, flagged, and reachable from the Transactions tab. It is
    # not lost, it is just not worth a question.
    out.sort(key=lambda x: (
        x.uncategorized_count == 0,
        tuple(-v for v in decision_weight(
            x.transaction_count,
            int(round((x.total_debit + x.total_credit) * 100)))),
        x.display_name,
    ))
    return out[:limit]


@router.post("/counterparties/{counterparty_key:path}",
             response_model=BulkCategorizeResponse)
def categorize_counterparty(
    counterparty_key: str,
    payload: BulkCategorizeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Categorise every pending transaction for one counterparty, and remember it.

    The memory is written even if zero transactions are currently pending, so a
    party the user categorises pre-emptively is recognised on the next upload.
    """
    purpose = (payload.category or payload.purpose or "").strip()
    if not is_valid_purpose(purpose):
        legacy = normalize_category(purpose)
        if legacy and legacy != UNCATEGORIZED:
            purpose = legacy
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid category '{payload.category or payload.purpose}'. "
                       f"Must be one of: {', '.join(PURPOSES)}",
            )

    event_type = (payload.event_type or "").strip() or None
    if not is_valid_event_type(event_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid event type '{event_type}'. Must be one of: {', '.join(EVENT_TYPES)}",
        )

    key = counterparty_key.strip().upper()

    category = (
        db.query(Category)
        .filter(Category.name == purpose,
                or_(Category.level.is_(None), Category.level == 1))
        .first()
    )
    if not category:
        category = Category(id=uuid.uuid4(), name=purpose)
        db.add(category)
        db.flush()

    now = datetime.datetime.utcnow()

    # Write the memory directly rather than through remember(): there is no
    # single narration to derive the key from, the key IS the input.
    row = (
        db.query(CounterpartyMemory)
        .filter(
            CounterpartyMemory.user_id == current_user.id,
            CounterpartyMemory.counterparty_key == key,
        )
        .first()
    )
    display = key.title()
    if row is None:
        row = CounterpartyMemory(
            id=uuid.uuid4(),
            user_id=current_user.id,
            counterparty_key=key,
            fuzzy_key=None,
            display_name=display,
            # Set from a real narration in the backfill below when one exists;
            # a key that matches nothing yet is assumed to be a counterparty,
            # which is the only thing a user would type in by hand.
            kind="counterparty",
            category=purpose,
            event_type=event_type,
            times_confirmed=1,
            source="bulk",
        )
        db.add(row)
    else:
        if row.category == purpose and row.event_type == event_type:
            row.times_confirmed = (row.times_confirmed or 1) + 1
        else:
            row.category = purpose
            row.event_type = event_type
            row.times_confirmed = 1
        display = row.display_name
    row.last_applied_at = now
    db.flush()

    updated = _apply_to_similar(
        db,
        user=current_user,
        counterparty_key=key,
        category_row=category,
        category_name=purpose,
        event_type=event_type,
        # No row is excluded: every pending transaction for this party is the
        # target of the action.
        exclude_txn_id=None,
        now=now,
        reviewer_id=current_user.id,
    )

    # Fill in the display name and fuzzy key from a real narration if this is a
    # new memory row and any transaction for the party exists.
    if row.fuzzy_key is None:
        sample = (
            db.query(Transaction)
            .filter(Transaction.user_id == current_user.id,
                    Transaction.superseded_by_id == None)
            .limit(2000)
            .all()
        )
        for tx in sample:
            cp = group_for_review(tx.narration_clean or tx.narration_raw or "")
            if cp and cp.key == key:
                row.fuzzy_key = cp.fuzzy_key
                row.display_name = cp.display
                row.kind = cp.kind
                display = cp.display
                break

    db.commit()

    logger.info(
        f"[Review Queue] User '{current_user.id}' categorised counterparty "
        f"'{key}' as '{purpose}'; {updated} transaction(s) updated"
    )

    return BulkCategorizeResponse(
        counterparty_key=key,
        display_name=display,
        category=purpose,
        purpose=purpose,
        transactions_updated=updated,
    )


class MemoryEntry(BaseModel):
    counterparty_key: str
    display_name: str
    kind: str = "counterparty"
    category: str
    event_type: Optional[str] = None
    times_confirmed: int
    source: str


@router.get("/memory", response_model=List[MemoryEntry])
def list_counterparty_memory(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Everything the system has learned about this user's counterparties."""
    rows = (
        db.query(CounterpartyMemory)
        .filter(CounterpartyMemory.user_id == current_user.id)
        .order_by(CounterpartyMemory.display_name)
        .all()
    )
    return [
        MemoryEntry(
            counterparty_key=r.counterparty_key,
            display_name=r.display_name,
            kind=r.kind or "counterparty",
            category=r.category,
            event_type=r.event_type,
            times_confirmed=r.times_confirmed or 1,
            source=r.source or "manual",
        )
        for r in rows
    ]


@router.get("/memory/merge-suggestions")
def list_merge_suggestions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Counterparties that look like the same party spelled two ways.

    Reported, never merged automatically — the system has no way to be sure
    NARASIMHAIAH CHIKEN and NARASIMHA CHIKEN are one supplier, and merging two
    real parties silently books their spend together.
    """
    return suggest_counterparty_merges(db, current_user.id)


@router.delete("/memory/{counterparty_key:path}")
def forget_counterparty(
    counterparty_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Forget one learned mapping.

    Transactions already categorised from it are left alone — undoing the
    learning is not the same as undoing decisions the user has since seen and
    accepted in their reports.
    """
    removed = forget_counterparty_memory(
        db, current_user.id, counterparty_key.strip().upper()
    )
    if not removed:
        raise HTTPException(status_code=404, detail="No such counterparty in memory")
    db.commit()
    return {"counterparty_key": counterparty_key.strip().upper(), "forgotten": True}


# ---------------------------------------------------------------------------
# STAGE 15 — "these may be the same entity"
# ---------------------------------------------------------------------------

class EntitySuggestion(BaseModel):
    """A pair the resolver is not confident enough to merge on its own."""
    key_a: str
    key_b: str
    display_a: str
    display_b: str
    score: float
    signals: Dict[str, float] = Field(default_factory=dict)
    conflicts: List[str] = Field(default_factory=list)


class EntityLinkRequest(BaseModel):
    key_a: str
    key_b: str
    same: bool
    display_a: Optional[str] = None
    display_b: Optional[str] = None


@router.get("/entity-suggestions", response_model=List[EntitySuggestion])
def list_entity_suggestions(
    account_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pairs the resolver thinks MIGHT be one party, for a person to settle.

    Only medium confidence appears here. High confidence has already been
    merged — asking about it would be theatre — and low confidence is not worth
    anyone's attention. Pairs the user has already answered, either way, are
    filtered out by the resolver before this is built.
    """
    from app.entity_resolution.normalize import representations as _reps

    rows = _review_query(db, current_user, account_id).all()
    _party_of, report = _resolve_parties(db, current_user, rows)

    out: List[EntitySuggestion] = []
    for display_a, display_b, verdict in report.suggestions[:limit]:
        out.append(EntitySuggestion(
            key_a=_reps(display_a).compact,
            key_b=_reps(display_b).compact,
            display_a=display_a,
            display_b=display_b,
            score=round(verdict.score, 4),
            signals={k: v for k, v in sorted(
                verdict.signals.items(), key=lambda kv: -kv[1])[:6]},
            conflicts=verdict.conflicts,
        ))
    return out


@router.post("/entity-links", status_code=status.HTTP_201_CREATED)
def record_entity_link(
    payload: EntityLinkRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save a person's answer to "are these the same party?" — either way.

    Storing the NO matters as much as the YES. Without it the resolver derives
    the same suggestion from the same evidence on every upload and asks a
    question that has already been answered.
    """
    key_a, key_b = EntityLink.ordered(payload.key_a, payload.key_b)
    if not key_a or not key_b or key_a == key_b:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Two different entity keys are required.")

    existing = (db.query(EntityLink)
                .filter(EntityLink.user_id == current_user.id,
                        EntityLink.key_a == key_a,
                        EntityLink.key_b == key_b)
                .first())
    if existing:
        # A person changing their mind replaces the answer rather than adding a
        # second, contradictory one.
        existing.same = payload.same
        existing.display_a = payload.display_a or existing.display_a
        existing.display_b = payload.display_b or existing.display_b
    else:
        db.add(EntityLink(
            id=uuid.uuid4(), user_id=current_user.id,
            key_a=key_a, key_b=key_b, same=payload.same,
            display_a=payload.display_a, display_b=payload.display_b,
        ))
    db.commit()
    return {"key_a": key_a, "key_b": key_b, "same": payload.same}


@router.get("/entity-report")
def entity_resolution_report(
    account_id: Optional[uuid.UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """STAGE 16. How much duplication the resolver actually removed.

    Reported rather than targeted. The objective is maximum SAFE reduction, and
    a number chosen in advance would push the engine into unsafe merges to
    reach it.
    """
    rows = _review_query(db, current_user, account_id).all()
    _party_of, report = _resolve_parties(db, current_user, rows)
    return report.as_dict()
