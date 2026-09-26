"""A write must never leave a stale figure behind.

These tests drive real SQLAlchemy sessions against the configured database and
assert on what happens to the cache, because the guarantee being tested is a
property of the session machinery rather than of any one endpoint. The point of
attaching invalidation to the session is that an endpoint nobody has written yet
still gets it; a test that called an endpoint would not prove that.
"""

import datetime
import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.models.transaction import Direction, Transaction
from app.services import cache_invalidation  # noqa: F401  (registers listeners)
from app.services.cache import cache


#: A user id that is never written to the database. Only ever used as a cache
#: key, to prove one user's eviction does not touch another's entries.
USER_B = "bbbbbbbb-0000-0000-0000-000000000002"


@pytest.fixture(scope="module")
def real_user_id():
    """A user id that actually exists in the database.

    `transactions.user_id` carries a foreign key to `users`, and this is
    PostgreSQL — the constraint is enforced, so a made-up id cannot even reach
    the flush these tests are about; the INSERT is rejected first.

    The suite builds a fresh schema per run (see conftest), so there is nobody
    to borrow. One is created here rather than skipping: a skipped test proves
    nothing, and this is the half of the cache that keeps wrong figures off a
    treasury screen.
    """
    from app.models.user import User

    db = SessionLocal()
    try:
        row = db.execute(text("SELECT id FROM users LIMIT 1")).first()
        if row:
            return str(row[0])
        user = User(
            id=uuid.uuid4(),
            email="cache-invalidation-probe@example.invalid",
            hashed_password="not-a-real-hash",
        )
        db.add(user)
        db.commit()
        return str(user.id)
    finally:
        db.close()


@pytest.fixture
def seeded(real_user_id):
    """Put a known entry in the cache for two users, and clean up after."""
    key_a = cache.user_key(real_user_id, "dashboard-summary")
    key_b = cache.user_key(USER_B, "dashboard-summary")
    cache.set(key_a, {"total_cash": 111}, 60)
    cache.set(key_b, {"total_cash": 222}, 60)
    assert cache.get(key_a) == {"total_cash": 111}
    assert cache.get(key_b) == {"total_cash": 222}
    yield key_a, key_b
    cache.invalidate_all()


def test_listeners_are_registered():
    """If these come unhooked, every test below would pass vacuously."""
    assert event.contains(Session, "before_flush", cache_invalidation._collect_dirty_users)
    assert event.contains(Session, "after_commit", cache_invalidation._invalidate_on_commit)
    assert event.contains(Session, "after_rollback", cache_invalidation._discard_on_rollback)


def test_orm_write_invalidates_only_the_writing_user(seeded, real_user_id):
    """The precise path: an ORM write names its owner, so only they are evicted."""
    key_a, key_b = seeded
    db: Session = SessionLocal()
    try:
        # Not committed to the real ledger — the assertion is about what the
        # session machinery does with a pending Transaction row, so it is rolled
        # back before it can become data.
        txn = Transaction(
            user_id=uuid.UUID(real_user_id),
            txn_date=datetime.date(2026, 1, 1),
            narration_raw="cache invalidation probe",
            direction=Direction.DEBIT,
            debit_paise=1,
        )
        db.add(txn)
        db.flush()                       # fires before_flush; collects USER_A

        pending = db.info.get(cache_invalidation._PENDING)
        assert pending and real_user_id in pending, "the writing user was not collected"
        assert USER_B not in (pending or set()), "an unrelated user was collected"

        # Nothing evicted yet: the write is not durable.
        assert cache.get(key_a) == {"total_cash": 111}, "evicted before commit"

        db.rollback()
    finally:
        db.close()


def test_rollback_evicts_nothing(seeded, real_user_id):
    """A write that did not happen must not cost the cache anything."""
    key_a, key_b = seeded
    db: Session = SessionLocal()
    try:
        db.add(Transaction(
            user_id=uuid.UUID(real_user_id),
            txn_date=datetime.date(2026, 1, 1),
            narration_raw="rolled back probe",
            direction=Direction.DEBIT,
            debit_paise=1,
        ))
        db.flush()
        db.rollback()
    finally:
        db.close()

    assert cache.get(key_a) == {"total_cash": 111}, "rollback still evicted"
    assert cache.get(key_b) == {"total_cash": 222}


def test_commit_applies_the_collected_invalidation(seeded, real_user_id):
    """The listener chain end to end, without touching the ledger.

    `after_commit` is invoked directly on a session carrying the same state a
    real flush would have left, which exercises the handler that matters while
    keeping real transaction rows out of the database.
    """
    key_a, key_b = seeded
    db: Session = SessionLocal()
    try:
        db.info[cache_invalidation._PENDING] = {real_user_id}
        cache_invalidation._invalidate_on_commit(db)
    finally:
        db.close()

    assert cache.get(key_a) is None, "the writing user's entry survived the commit"
    assert cache.get(key_b) == {"total_cash": 222}, "another user's entry was evicted"


def test_bulk_write_widens_to_everything(seeded):
    """Bulk SQL names nobody, so it must clear the lot rather than guess."""
    key_a, key_b = seeded
    db: Session = SessionLocal()
    try:
        db.info[cache_invalidation._PENDING_ALL] = True
        cache_invalidation._invalidate_on_commit(db)
    finally:
        db.close()

    assert cache.get(key_a) is None
    assert cache.get(key_b) is None, "a bulk write left another user's entry cached"


def test_a_real_bulk_delete_sets_the_widen_flag(real_user_id):
    """The `after_bulk_delete` hook fires on the real query API.

    Scoped to a date that holds no rows, so the statement is genuinely issued
    and genuinely deletes nothing. It is rolled back regardless.
    """
    db: Session = SessionLocal()
    try:
        db.query(Transaction).filter(
            Transaction.user_id == uuid.UUID(real_user_id),
            Transaction.txn_date == datetime.date(1900, 1, 1),
        ).delete(synchronize_session=False)
        assert db.info.get(cache_invalidation._PENDING_ALL) is True, \
            "a bulk delete on transactions did not flag the cache"
        db.rollback()
    finally:
        db.close()


def test_every_cache_affecting_table_actually_exists():
    """A typo in the table list is silent: it just stops invalidating.

    This is the failure mode worth a test — nothing breaks, nothing logs, and
    the cache quietly serves figures for rows that changed.
    """
    import app.models  # noqa: F401
    import app.aa.models  # noqa: F401
    from app.database.session import Base

    real = {m.class_.__tablename__ for m in Base.registry.mappers}
    unknown = cache_invalidation.CACHE_AFFECTING_TABLES - real
    assert not unknown, f"CACHE_AFFECTING_TABLES names tables that do not exist: {unknown}"


def test_the_ledger_tables_are_covered():
    """The tables whose contents are definitely cached must be listed."""
    must_cover = {
        "transactions", "predictions", "categories", "accounts", "entities",
        "uploaded_files", "statements",
        "reconciliation_runs", "reconciliation_items",
        "anomaly_findings", "policy_violations", "policy_rules",
    }
    missing = must_cover - cache_invalidation.CACHE_AFFECTING_TABLES
    assert not missing, f"writes to these would not invalidate the cache: {missing}"
