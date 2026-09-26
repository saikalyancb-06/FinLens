"""The ledger write must not depend on the counterparty memory.

WHAT HAPPENED. `counterparty_memory` gained a `kind` column in the models, but
the database had not caught up. `store_transactions` loads that table once per
batch to auto-apply previously-categorised counterparties — a convenience. The
query raised, the exception propagated, and the ENTIRE ledger write went with
it. The user's statement parsed cleanly, 60 rows validated, and nothing was
stored: an empty Transactions tab and an empty review queue, with the upload
reporting success.

A lookup that IMPROVES categorisation must never be able to destroy the write.
Degrading to "no memory" costs one round of re-categorising counterparties.
Losing the write costs the statement.

THE SUBTLETY, which the first attempt at this fix got wrong. A failed statement
poisons the enclosing postgres transaction — every later statement errors until
a rollback. But a plain `db.rollback()` also discards work already flushed in
that transaction, including the Statement row, and the transaction INSERT then
dies with

    ForeignKeyViolation: violates constraint transactions_statement_id_fkey

losing the write just as thoroughly, from a different direction. The lookup has
to be isolated in a SAVEPOINT so only it is rolled back.
"""

import datetime
import uuid

import pytest
from sqlalchemy import text

from app.models.account import Account
from app.models.prediction import Prediction
from app.models.transaction import Transaction
from app.services.transaction_storage import TransactionStorageService
from tests.conftest import TestingSessionLocal, register_bank_account
from tests.conftest import test_engine


@pytest.fixture
def auth(client):
    email = f"ing_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    tok = client.post("/auth/login",
                      json={"email": email, "password": "Password123!"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}
    user_id = uuid.UUID(client.get("/auth/me", headers=headers).json()["id"])
    yield headers, user_id
    db = TestingSessionLocal()
    try:
        db.query(Prediction).filter(Prediction.transaction_id.in_(
            db.query(Transaction.id).filter(Transaction.user_id == user_id)
        )).delete(synchronize_session=False)
        db.query(Transaction).filter(
            Transaction.user_id == user_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def account(client, auth):
    headers, user_id = auth
    register_bank_account(client, headers,
                          account_number=f"5060{uuid.uuid4().int % 10**8:08d}")
    db = TestingSessionLocal()
    try:
        return db.query(Account).filter(Account.user_id == user_id).first().id
    finally:
        db.close()


@pytest.fixture
def broken_counterparty_memory():
    """Reproduce the user's schema: the table exists, a column does not.

    Restored afterwards whatever the test does, because every other test in the
    suite reads this table.
    """
    with test_engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE counterparty_memory DROP COLUMN IF EXISTS kind"))
    try:
        yield
    finally:
        with test_engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE counterparty_memory ADD COLUMN IF NOT EXISTS kind "
                "VARCHAR(16) NOT NULL DEFAULT 'counterparty'"))


def _rows(n=12):
    base = datetime.date(2026, 5, 1)
    return [
        {
            "date": (base + datetime.timedelta(days=i)).isoformat(),
            "description": f"EBANK:WIB/15019{i:05d}/KUMAR FISH",
            "raw_text": f"EBANK:WIB/15019{i:05d}/KUMAR FISH",
            "debit": 1500.00 + i,
            "credit": None,
            "balance": 500000.00 - i * 1500,
            "row_index": i,
        }
        for i in range(n)
    ]


def _ingest(user_id, account_id, rows):
    db = TestingSessionLocal()
    try:
        stored = TransactionStorageService().store_transactions(
            db=db, processed_txns=rows, user_id=user_id,
            account_id=account_id, source_channel="MANUAL_UPLOAD",
        )
        db.commit()
        return len(stored)
    finally:
        db.close()


def test_transactions_are_stored_when_the_memory_table_is_broken(
    auth, account, broken_counterparty_memory
):
    """The regression, exactly as the user hit it."""
    _headers, user_id = auth
    stored = _ingest(user_id, account, _rows(12))
    assert stored == 12, "the ledger write must not depend on the memory lookup"

    db = TestingSessionLocal()
    try:
        assert db.query(Transaction).filter(
            Transaction.user_id == user_id).count() == 12
    finally:
        db.close()


def test_the_statement_row_survives_the_failed_lookup(
    auth, account, broken_counterparty_memory
):
    """Guards the second failure mode.

    The first fix called db.rollback(), which discarded the Statement flushed
    earlier in the same transaction; every transaction INSERT then failed on
    transactions_statement_id_fkey. Rows existing at all proves the outer
    transaction survived intact.
    """
    _headers, user_id = auth
    _ingest(user_id, account, _rows(6))

    db = TestingSessionLocal()
    try:
        rows = db.query(Transaction).filter(Transaction.user_id == user_id).all()
        assert len(rows) == 6
        # Written and committed, so no FK violation occurred.
        assert all(r.id is not None for r in rows)
    finally:
        db.close()


def test_classification_still_runs_when_the_memory_is_unavailable(
    auth, account, broken_counterparty_memory
):
    """Degraded means "no memory", not "no categorisation".

    The rules do not need the table; only the counterparty auto-apply does.
    """
    _headers, user_id = auth
    rows = _rows(4)
    rows.append({
        "date": "2026-05-20", "description": "SERVICE CHARGE FOR MAY 2026",
        "raw_text": "SERVICE CHARGE FOR MAY 2026", "debit": 250.0,
        "credit": None, "balance": 480000.0, "row_index": 99,
    })
    _ingest(user_id, account, rows)

    db = TestingSessionLocal()
    try:
        charge = db.query(Transaction).filter(
            Transaction.user_id == user_id,
            Transaction.narration_clean.like("%SERVICE CHARGE%"),
        ).first()
        assert charge is not None
        assert charge.category_id is not None, (
            "rule-based categorisation does not use the memory table and must "
            "keep working when it is unavailable"
        )
    finally:
        db.close()


def test_memory_is_applied_normally_when_the_table_is_healthy(auth, account):
    """The degraded path must not become the only path."""
    from app.categorization.counterparty_memory import remember

    _headers, user_id = auth
    db = TestingSessionLocal()
    try:
        remember(db, user_id, "EBANK:WIB/1/KUMAR FISH", category="Cost of Goods")
        db.commit()
    finally:
        db.close()

    _ingest(user_id, account, _rows(5))

    db = TestingSessionLocal()
    try:
        for tx in db.query(Transaction).filter(Transaction.user_id == user_id).all():
            pred = db.query(Prediction).filter(
                Prediction.transaction_id == tx.id).first()
            assert pred.classification_method == "counterparty_memory"
    finally:
        db.close()
