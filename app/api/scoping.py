"""Shared filter predicates for the API layer.

The rules here are shared rather than repeated at each call site because the
first one already WAS repeated at twelve of them — and every one of the twelve
had the same bug.
"""

from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy import or_, select
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
    return or_(
        Transaction.entity_id == entity_id,
        Transaction.account_id.in_(
            select(Account.id).where(Account.entity_id == entity_id)
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
