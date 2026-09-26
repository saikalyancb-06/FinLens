"""Drill-down over the category tree.

The endpoint returns ONE LEVEL AT A TIME, and that is the design rather than a
convenience. The alternative — ship the whole tree with counts and let the
browser walk it — has to decide up front how deep the tree goes, and this tree
does not have an answer to that: `Transfers > Own Account Transfer` is two
levels and finished, `Food & Dining > Restaurants > Fast Food > McDonald's` is
four, and which one a user is looking at is not known until they click.

So each response answers exactly the question that was asked — "what is under
here?" — and each child carries `has_children`, computed from the transactions
that are actually there. Two consequences the UI gets for free:

* A level that exists in the taxonomy but holds nothing for this user is never
  offered. No empty drill-downs.
* A level nobody can go deeper into is marked as such, so the UI stops offering
  a click that would lead to a blank list.

Levels are derived from `Transaction.category_path`, not from the categories
table, and that is deliberate too. The path is user-scoped, so the levels that
came from a narration — an employer under Salary, a supplier under Inventory —
appear for the user whose statement named them and for nobody else. The
categories table has no user_id and cannot make that distinction.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.categorization import flow as F
from app.categorization import hierarchy as H
from app.categorization.decisions import REVIEW_THRESHOLD, needs_decision
from app.database.session import get_db
from app.models.account import Account
from app.models.transaction import Transaction
from app.models.user import User
from app.utils.security import get_current_user
from app.api.scoping import entity_scope

router = APIRouter(prefix="/v1/categories", tags=["Categories"])

SEPARATOR = " > "


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------

class CategoryNode(BaseModel):
    name: str
    path: List[str]
    display_path: str
    slug: str
    level: int
    has_children: bool = Field(
        description="Whether drilling into this node leads anywhere. False means "
                    "the transactions here carry no deeper level, so the UI must "
                    "not offer another click."
    )
    transaction_count: int
    total_debit: float
    total_credit: float
    net: float
    #: Share of the parent level's volume, so a bar can be drawn without a
    #: second request.
    share: float


class DrillDownResponse(BaseModel):
    path: List[str]
    display_path: Optional[str]
    level: int
    #: Every ancestor, so the UI can render a breadcrumb without keeping state.
    breadcrumb: List[Dict[str, object]]
    children: List[CategoryNode]
    transaction_count: int
    total_debit: float
    total_credit: float
    #: True when this node holds transactions but none of them go deeper. The UI
    #: should show the transaction list rather than another level.
    is_terminal: bool


class TreeNode(BaseModel):
    name: str
    slug: str
    level: int
    display_path: str
    accepts_custom_child: bool
    children: List["TreeNode"] = Field(default_factory=list)


TreeNode.model_rebuild()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _scoped_query(db: Session, user: User, account_id: Optional[UUID],
                  date_from: Optional[date], date_to: Optional[date],
                  flow_type: Optional[str], entity_id: Optional[UUID] = None):
    q = db.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.superseded_by_id.is_(None),
    )
    if account_id:
        owns = db.query(Account).filter(
            Account.id == account_id, Account.user_id == user.id
        ).first()
        if not owns:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Account not found")
        q = q.filter(Transaction.account_id == account_id)
    # The Entity selector sits in the same filter bar as the account and the
    # dates on this screen, and every OTHER screen behind that bar honours it —
    # /transactions and every dashboard panel filter on entity_id. This endpoint
    # did not, and took no such parameter, so choosing an entity on Categories
    # silently changed nothing: the control was there, it moved, and the figures
    # underneath were still the whole ledger. Scoped here so the bar means the
    # same thing on every page it is shown on.
    if entity_id:
        q = q.filter(entity_scope(entity_id))
    if date_from:
        q = q.filter(Transaction.txn_date >= date_from)
    if date_to:
        q = q.filter(Transaction.txn_date <= date_to)
    if flow_type:
        q = q.filter(Transaction.flow_type == flow_type)
    return q


def _parse_path(path: Optional[str]) -> List[str]:
    """Accept `A > B > C` or `A/B/C`; both are natural to type in a URL."""
    if not path:
        return []
    raw = path.replace("/", SEPARATOR) if SEPARATOR not in path else path
    return [p.strip() for p in raw.split(">") if p.strip()]


# The node that was removed from the taxonomy. Kept as a STRING and only to
# recognise stale rows — nothing creates it any more.
RETIRED_ROOT = "Other / Uncategorized"


def _residual_for(txn: Transaction):
    """Where a row goes on the facts it carries: its rail and its direction."""
    return H.residual_path(
        txn.transaction_method,
        "credit" if (txn.credit_paise or 0) else "debit",
        txn.flow_type,
    )


def _row_path(txn: Transaction) -> List[str]:
    """The path stored on a row, falling back through what is available.

    A row ingested before the hierarchy existed has no `category_path`. Rather
    than dropping it out of the drill-down entirely — which would make the
    totals disagree with every other screen — it is placed at its flat category.

    The last fallback used to be `Other / Uncategorized`. That node is gone: it
    was not a category, it was the absence of one, and it swallowed a quarter of
    a statement into something nobody could drill into or reconcile. A row that
    has never been through the classifier still carries a payment rail and a
    direction, and those are facts about it — so it is placed by those instead.
    Running `Re-categorize existing` replaces this floor with a real answer
    wherever the narration supports one.
    """
    stored = txn.category_path or txn.category or ""
    if stored.startswith(RETIRED_ROOT):
        # A row still stamped with the node that was deleted. Migration 011
        # rewrites these, but the screen must not show a category the code no
        # longer has just because the migration has not been run yet — that is
        # how a user ends up looking at a branch nothing can open.
        return list(_residual_for(txn))
    if txn.category_path:
        return [p.strip() for p in txn.category_path.split(">") if p.strip()]
    if txn.category:
        return [txn.category]
    if txn.legacy_category:
        return [txn.legacy_category]
    return list(_residual_for(txn))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/tree", response_model=List[TreeNode])
def category_tree(_current_user: User = Depends(get_current_user)):
    """The taxonomy itself, for pickers and for showing what is available.

    This is the FIXED tree only — it says nothing about what any user has. Use
    `/drilldown` for the tree a user's own transactions actually populate; the
    two are different questions and conflating them is how a picker ends up
    offering categories nobody can select.
    """
    def build(node: H.Node) -> TreeNode:
        return TreeNode(
            name=node.name,
            slug=node.slug,
            level=node.level,
            display_path=node.display_path,
            accepts_custom_child=node.allows_dynamic_children,
            children=[build(H.NODES[c]) for c in node.children],
        )

    return [build(H.NODES[H.path_slug((root,))]) for root in H.ROOTS]


@router.get("/drilldown", response_model=DrillDownResponse)
def drilldown(
    path: Optional[str] = Query(
        None, description="Where to drill from. Omit for the top level. "
                          "`Food & Dining > Restaurants` or `Food & Dining/Restaurants`."),
    account_id: Optional[UUID] = None,
    entity_id: Optional[UUID] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    flow_type: Optional[str] = Query(
        None, description="INFLOW | OUTFLOW | TRANSFER | REVERSAL"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One level of the tree, as this user's own transactions populate it."""
    if flow_type and flow_type.upper() not in F.FLOW_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"flow_type must be one of {', '.join(F.FLOW_TYPES)}",
        )

    prefix = _parse_path(path)
    depth = len(prefix)

    rows = _scoped_query(db, current_user, account_id, date_from, date_to,
                         flow_type.upper() if flow_type else None,
                         entity_id=entity_id).all()

    buckets: Dict[str, Dict[str, float]] = {}
    here_count = here_debit = here_credit = 0
    deeper_exists = False

    for txn in rows:
        row_path = _row_path(txn)
        if row_path[:depth] != prefix:
            continue

        debit = (txn.debit_paise or 0) / 100.0
        credit = (txn.credit_paise or 0) / 100.0
        here_count += 1
        here_debit += debit
        here_credit += credit

        if len(row_path) <= depth:
            # The row stops here. It is counted in this node's totals and
            # contributes no child — which is exactly how a path that ends at
            # two levels stays a complete answer instead of manufacturing a
            # third.
            continue

        deeper_exists = True
        child = row_path[depth]
        b = buckets.setdefault(child, {
            "count": 0, "debit": 0.0, "credit": 0.0, "has_children": False,
        })
        b["count"] += 1
        b["debit"] += debit
        b["credit"] += credit
        if len(row_path) > depth + 1:
            b["has_children"] = True

    volume = sum(b["debit"] + b["credit"] for b in buckets.values()) or 1.0

    children = [
        CategoryNode(
            name=name,
            path=prefix + [name],
            display_path=SEPARATOR.join(prefix + [name]),
            slug=H.path_slug(prefix + [name]),
            level=depth + 1,
            has_children=bool(b["has_children"]),
            transaction_count=int(b["count"]),
            total_debit=round(b["debit"], 2),
            total_credit=round(b["credit"], 2),
            net=round(b["credit"] - b["debit"], 2),
            share=round(((b["debit"] + b["credit"]) / volume) * 100, 2),
        )
        for name, b in buckets.items()
    ]
    # Biggest first: a drill-down is a way of finding where the money went, and
    # alphabetical order buries that under whatever happens to start with A.
    children.sort(key=lambda c: c.total_debit + c.total_credit, reverse=True)

    breadcrumb = [
        {"name": part,
         "path": SEPARATOR.join(prefix[:i + 1]),
         "level": i + 1}
        for i, part in enumerate(prefix)
    ]

    return DrillDownResponse(
        path=prefix,
        display_path=SEPARATOR.join(prefix) if prefix else None,
        level=depth,
        breadcrumb=breadcrumb,
        children=children,
        transaction_count=here_count,
        total_debit=round(here_debit, 2),
        total_credit=round(here_credit, 2),
        is_terminal=here_count > 0 and not deeper_exists,
    )


@router.get("/transactions")
def transactions_at(
    path: str = Query(..., description="The node to list transactions for."),
    include_descendants: bool = Query(
        True, description="Include rows filed deeper than this node. False lists "
                          "only the rows that stop exactly here."),
    account_id: Optional[UUID] = None,
    entity_id: Optional[UUID] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    flow_type: Optional[str] = None,
    limit: int = Query(200, le=1000),
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The transactions behind a node — the bottom of every drill-down."""
    prefix = _parse_path(path)
    if not prefix:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="path is required")
    depth = len(prefix)

    rows = _scoped_query(db, current_user, account_id, date_from, date_to,
                         flow_type.upper() if flow_type else None,
                         entity_id=entity_id).all()

    matched = []
    for txn in rows:
        row_path = _row_path(txn)
        if row_path[:depth] != prefix:
            continue
        if not include_descendants and len(row_path) != depth:
            continue
        matched.append((txn, row_path))

    matched.sort(key=lambda pair: (pair[0].txn_date, pair[0].created_at), reverse=True)
    total = len(matched)
    page = matched[offset:offset + limit]

    return {
        "path": prefix,
        "display_path": SEPARATOR.join(prefix),
        "total": total,
        "limit": limit,
        "offset": offset,
        "transactions": [
            {
                "id": str(txn.id),
                "txn_date": txn.txn_date.isoformat() if txn.txn_date else None,
                "value_date": txn.value_date.isoformat() if txn.value_date else None,
                "narration": txn.narration_clean or txn.narration_raw,
                "debit": (txn.debit_paise or 0) / 100.0,
                "credit": (txn.credit_paise or 0) / 100.0,
                "balance": (txn.balance_paise / 100.0)
                           if txn.balance_paise is not None else None,
                "flow_type": txn.flow_type,
                "transaction_method": txn.transaction_method,
                "category": txn.category,
                "category_path": SEPARATOR.join(row_path),
                # Spelled out as well as joined, because a client that wants to
                # render the levels separately should not have to re-split a
                # string whose separator is a display choice.
                "subcategory": row_path[1] if len(row_path) > 1 else None,
                "specific_category": row_path[2] if len(row_path) > 2 else None,
                "detail": row_path[3] if len(row_path) > 3 else None,
                "merchant": txn.merchant,
                "counterparty": txn.counterparty,
                "reference_no": txn.reference_no,
                "confidence": (float(txn.category_confidence)
                               if txn.category_confidence is not None else None),
                "legacy_category": txn.legacy_category,
            }
            for txn, row_path in page
        ],
    }


@router.get("/summary")
def summary(
    account_id: Optional[UUID] = None,
    entity_id: Optional[UUID] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """How well the classification is doing, in numbers the user can check.

    Coverage and confidence are reported separately from the totals because
    they answer a different question: not "where did the money go" but "how
    much of this should you believe". A drill-down that looks complete while a
    third of its rows are low-confidence guesses is worse than one that says so.
    """
    rows = _scoped_query(db, current_user, account_id, date_from, date_to, None,
                         entity_id=entity_id).all()
    total = len(rows)

    # How many DECISIONS are outstanding, which is the only number here a
    # person can act on. "N transactions uncategorised" is a symptom; the work
    # is one answer per counterparty, and 220 rows behind 12 parties is a
    # different afternoon from 220 rows behind 180.
    from app.categorization.counterparty import group_for

    pending_groups = set()
    for txn in rows:
        if not needs_decision(txn):
            continue
        grp = group_for(txn.narration_clean or txn.narration_raw or "")
        if grp:
            pending_groups.add(grp.group_key)

    by_depth: Dict[int, int] = {}
    by_flow: Dict[str, int] = {}
    by_method: Dict[str, int] = {}
    needs_review = uncategorised = unclear = 0
    confidence_sum = 0.0
    confidence_n = 0

    for txn in rows:
        depth = len(_row_path(txn))
        by_depth[depth] = by_depth.get(depth, 0) + 1
        by_flow[txn.flow_type or "unknown"] = by_flow.get(txn.flow_type or "unknown", 0) + 1
        by_method[txn.transaction_method or "unknown"] = (
            by_method.get(txn.transaction_method or "unknown", 0) + 1)
        if not (txn.category or ""):
            uncategorised += 1
        if needs_decision(txn):
            unclear += 1
        if txn.category_confidence is not None:
            c = float(txn.category_confidence)
            confidence_sum += c
            confidence_n += 1
            if c < REVIEW_THRESHOLD:
                needs_review += 1

    return {
        "transactions": total,
        "categorised": total - unclear,
        "uncategorised": uncategorised,
        # The number that replaced "uncategorised" as the one worth showing.
        # See `app.categorization.decisions.needs_decision` — the SAME predicate
        # the Review Queue filters on, so the two screens cannot disagree.
        "unclear": unclear,
        "needs_review": needs_review,
        # Decisions, not rows. See above.
        "decisions_outstanding": len(pending_groups),
        "average_confidence": round(confidence_sum / confidence_n, 3) if confidence_n else None,
        # How deep the answers actually go. A distribution sitting entirely at
        # depth 1 means the hierarchy is not earning its keep on this data.
        "depth_distribution": {str(k): v for k, v in sorted(by_depth.items())},
        "by_flow_type": by_flow,
        "by_transaction_method": by_method,
    }
