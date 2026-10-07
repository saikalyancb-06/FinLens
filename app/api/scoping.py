"""Shared filter predicates for the API layer.

The rules here are shared rather than repeated at each call site because the
first one already WAS repeated at twelve of them — and every one of the twelve
had the same bug.
"""

from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.transaction import Transaction
from app.models.user import User


def entity_scope(entity_id: Any):
    """Rows belonging to one entity, resolved so it cannot go stale.

    `Transaction.entity_id` is DENORMALISED: `store_transactions` copies it off
    the account at ingestion time. That copy is only correct if the account was
    already linked to its entity when the statement was parsed, and nothing
    re-copies it afterwards. In practice the order is usually the other way
    round — statements are uploaded first, and the account is filed under an
    entity later, from Bank Master.

    Every row ingested before that link therefore keeps `entity_id = NULL`
    forever, and a filter of `Transaction.entity_id == :id` matches none of
    them. What the user saw was not an error: picking their entity from the
    filter bar took the dashboard from 18 transactions to 0, the analytics
    panels to empty, and the reports to nil — the whole application going blank
    on a filter that named the only entity they had.

    So entity membership is asked as the two-part question it actually is: the
    row says so itself, OR the account the row landed in belongs to that
    entity. The second half is the durable one — an account's entity link is
    the single place that relationship is edited — and it makes the answer
    correct for rows written before the link existed, with no backfill and no
    migration. The first half is kept because a transaction may legitimately
    carry an entity of its own.

    Returns a SQLAlchemy predicate; use it inside `.filter(...)`.
    """
    # The account's link wins whenever the account has one. Moving an account
    # to another entity in Bank Master does not rewrite the copies on its old
    # rows, so "row says E1 OR account says E2" counted those rows under BOTH
    # entities and the per-entity figures summed to more than the total. The
    # row's own copy is consulted only when its account has no entity (or the
    # row has no account) — the case the copy exists for.
    return or_(
        Transaction.account_id.in_(
            select(Account.id).where(Account.entity_id == entity_id)
        ),
        and_(
            Transaction.entity_id == entity_id,
            or_(
                Transaction.account_id.is_(None),
                Transaction.account_id.in_(
                    select(Account.id).where(Account.entity_id.is_(None))
                ),
            ),
        ),
    )


def accounts_in_scope(
    db: Session,
    user: User,
    account_id: Optional[Any] = None,
    entity_id: Optional[Any] = None,
) -> Optional[List[Any]]:
    """The account ids a filter-bar selection covers.

    Returns None when nothing is selected, meaning "do not narrow" — callers
    must treat that as different from `[]`, which means "a real selection that
    happens to cover no accounts" and must match nothing.

    This exists for the tables that hang off an ACCOUNT rather than off a
    transaction — anomaly findings and policy violations. They carry
    `account_id` and no entity, so an entity selection has to be resolved to the
    accounts filed under it before it can be applied. Ownership is enforced in
    the same query, so a caller cannot widen the scope by naming somebody
    else's account or entity.
    """
    if not account_id and not entity_id:
        return None
    q = db.query(Account.id).filter(Account.user_id == user.id)
    if account_id:
        q = q.filter(Account.id == account_id)
    if entity_id:
        q = q.filter(Account.entity_id == entity_id)
    return [row[0] for row in q.all()]


def scope_findings(query, model, scope_ids, date_from=None, date_to=None):
    """Narrow an AnomalyFinding / PolicyViolation query to the filter bar.

    One definition for every place findings are counted or listed — the
    dashboard tiles, the panel headers and the lists under them — so the
    header can never say 12 above a list of 9.

    * Account scope: findings on the selected accounts only. A finding that
      names no account cannot be attributed to the selection, so once a scope
      is set it is left out (an empty scope matches nothing).
    * Dates: findings that happened inside the period. A finding with no date
      is not tied to any period and is kept.
    """
    if scope_ids is not None:
        query = query.filter(model.account_id.in_(scope_ids)) if scope_ids else query.filter(False)
    if date_from is not None:
        query = query.filter(or_(model.occurred_on.is_(None), model.occurred_on >= date_from))
    if date_to is not None:
        query = query.filter(or_(model.occurred_on.is_(None), model.occurred_on <= date_to))
    return query


def finding_key(row) -> tuple:
    """What makes two findings the same finding (scans can write one twice)."""
    rule = getattr(row, "rule_id", None)
    kind = getattr(row, "anomaly_type", None)
    return (str(rule or kind), str(row.transaction_id) if row.transaction_id else None,
            str(row.occurred_on), row.amount_paise,
            getattr(row, "title", None) if kind is not None else getattr(row, "detail", None))


def count_distinct_findings(rows) -> dict:
    """{severity: count} over distinct findings, plus 'total'."""
    seen, out = set(), {}
    for r in rows:
        k = finding_key(r)
        if k in seen:
            continue
        seen.add(k)
        out[r.severity] = out.get(r.severity, 0) + 1
    out["total"] = sum(v for k, v in out.items() if k != "total")
    return out
