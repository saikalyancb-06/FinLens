"""Cache invalidation that cannot be forgotten.

WHY THIS IS NOT A SET OF invalidate_user() CALLS
The obvious way to keep the cache honest is to call `cache.invalidate_user(...)`
next to every write. This application has over sixty `db.commit()` sites across
twenty files, and the number grows every time someone adds an endpoint. Every
one of them is a chance to forget, and forgetting does not produce a slow page —
it produces a cash position on screen that no longer matches the database, which
is exactly the failure a treasury tool cannot have.

So invalidation is attached to the session itself. Any write to a table whose
contents feed a cached figure invalidates that user's entries when the
transaction commits, whether the code that wrote it knew this cache existed or
not. A new endpoint gets it for free.

TWO PATHS, BECAUSE THERE ARE TWO KINDS OF WRITE
  * ORM writes (session.add, attribute mutation, session.delete) pass through
    the unit of work, so the objects — and therefore their user_id — can be
    inspected before the flush. These invalidate precisely one user.
  * Bulk writes (`query.update(...)`, `query.delete(synchronize_session=False)`)
    are compiled straight to SQL and never instantiate a row. There is nothing
    to inspect and no reliable way to recover whose data was touched, so these
    clear the whole cache. That is deliberate: they are rare and destructive —
    clearing a user's transactions, deleting an account — and a cold cache for a
    moment is a far better outcome than a stale one.

ONLY AFTER COMMIT
Nothing is invalidated until the transaction actually commits. Invalidating on
flush would throw away good cache entries for a write that then rolled back, and
this codebase rolls back deliberately in several places.
"""

from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.services.cache import cache

logger = logging.getLogger(__name__)


#: Tables whose contents feed a cached figure.
#:
#: Matched by table name rather than by importing the model classes, for two
#: reasons: the models import Base from app.database.session, so importing them
#: here would create a cycle, and a name comparison costs nothing on a hot path
#: that runs on every flush.
#:
#: Deliberately broad. A table listed here that did not need to be costs an
#: unnecessary cache clear; a table missing from it serves stale financial data.
#: Those are not comparable errors.
CACHE_AFFECTING_TABLES = frozenset({
    # The ledger itself, and how it is classified
    "transactions",
    "processed_transactions",
    "predictions",
    "categories",
    "counterparty_memory",
    # Where transactions come from
    "uploaded_files",
    "statements",
    "import_batches",
    "email_attachments",
    # Scope and balances — an account's minimum balance is part of the
    # liquidity figures, and deleting an entity changes every total
    "accounts",
    "entities",
    "entity_link",
    # Reconciliation drives the unreconciled and ageing figures
    "reconciliation_runs",
    "reconciliation_items",
    "reconciliation_matches",
    "reconciliation_match_lines",
    "book_entries",
    # Compliance drives the anomaly and policy tiles
    "anomaly_findings",
    "policy_violations",
    "policy_rules",
    # Duplicates change which transactions are active
    "duplicate_matches",
})

#: Key under which the pending invalidation set rides on Session.info.
_PENDING = "_cache_invalidate_users"
_PENDING_ALL = "_cache_invalidate_all"


def _table_of(obj) -> str:
    table = getattr(obj, "__tablename__", None)
    if table:
        return table
    mapped = getattr(obj, "__table__", None)
    return getattr(mapped, "name", "") if mapped is not None else ""


@event.listens_for(Session, "before_flush")
def _collect_dirty_users(session, flush_context, instances):
    """Note which users are about to have cache-affecting rows written.

    Runs before the flush because that is the last moment the objects are still
    inspectable — after a delete flushes, the instance is gone.
    """
    try:
        pending = session.info.get(_PENDING)
        for obj in list(session.new) + list(session.dirty) + list(session.deleted):
            if _table_of(obj) not in CACHE_AFFECTING_TABLES:
                continue
            user_id = getattr(obj, "user_id", None)
            if user_id is None:
                # A row on a cache-affecting table that does not name its owner
                # (predictions and match lines reach their user through a join).
                # There is no safe narrowing available, so widen instead.
                session.info[_PENDING_ALL] = True
                continue
            if pending is None:
                pending = set()
                session.info[_PENDING] = pending
            pending.add(str(user_id))
    except Exception:
        # Invalidation must never be able to fail a write. Worst case here is a
        # stale entry, which the TTL clears shortly anyway.
        logger.exception("[cache] could not collect dirty users; widening")
        session.info[_PENDING_ALL] = True


@event.listens_for(Session, "after_bulk_update")
@event.listens_for(Session, "after_bulk_delete")
def _note_bulk_write(context):
    """Flag a bulk UPDATE/DELETE, which the unit of work never sees.

    These compile straight to SQL, so no instance is created and there is no
    user_id to read. The only honest response is to widen to everything.
    """
    try:
        session = getattr(context, "session", None)
        mapper = getattr(context, "mapper", None)
        table = getattr(getattr(mapper, "local_table", None), "name", "")
        if session is not None and (not table or table in CACHE_AFFECTING_TABLES):
            session.info[_PENDING_ALL] = True
    except Exception:
        logger.exception("[cache] could not inspect a bulk write; widening")
        try:
            context.session.info[_PENDING_ALL] = True
        except Exception:
            pass


@event.listens_for(Session, "after_commit")
def _invalidate_on_commit(session):
    """Apply what the flush collected — only once the write is durable."""
    users = session.info.pop(_PENDING, None)
    widen = session.info.pop(_PENDING_ALL, False)
    if not users and not widen:
        return
    try:
        if widen:
            cache.invalidate_all()
        else:
            for user_id in users:
                cache.invalidate_user(user_id)
    except Exception:
        # Same reasoning as above, and by this point the commit has already
        # happened — raising here would report a successful write as a failure.
        logger.exception("[cache] invalidation failed after commit")


@event.listens_for(Session, "after_rollback")
def _discard_on_rollback(session):
    """A write that did not happen must not evict anything."""
    session.info.pop(_PENDING, None)
    session.info.pop(_PENDING_ALL, None)


def install() -> None:
    """Importing this module is what registers the listeners.

    This function exists so the import in main.py reads as an intentional act
    rather than an unused import somebody later tidies away.
    """
    logger.debug("[cache] invalidation listeners registered")
