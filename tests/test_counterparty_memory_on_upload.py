"""The loop that makes counterparty memory worth having.

Categorise a party once -> upload a NEW statement naming that party -> those
rows arrive already categorised, without ever entering the review queue.

This exercises `TransactionStorageService.store_transactions`, which is the
canonical ledger write the upload pipeline calls (parsing_queue.py:184). Testing
the memory helper alone would not catch the failure mode that matters here: the
memory returning a category name that the Category table has no row for, which
would silently store the transaction with no category at all.
"""

import datetime
import uuid

import pytest

from app.categorization.counterparty_memory import remember
from app.services.recategorize import METHOD_COUNTERPARTY
from app.models.account import Account
from app.models.counterparty_memory import CounterpartyMemory
from app.models.prediction import Prediction
from app.models.transaction import Transaction
from app.services.transaction_storage import TransactionStorageService
from tests.conftest import TestingSessionLocal, register_bank_account


@pytest.fixture
def auth(client):
    email = f"cpu_{uuid.uuid4().hex[:6]}@example.com"
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
        db.query(CounterpartyMemory).filter(
            CounterpartyMemory.user_id == user_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def account(client, auth):
    headers, user_id = auth
    register_bank_account(client, headers,
                          account_number=f"5020{uuid.uuid4().int % 10**8:08d}")
    db = TestingSessionLocal()
    try:
        return db.query(Account).filter(Account.user_id == user_id).first().id
    finally:
        db.close()


def _statement_rows(narrations, start_day=1):
    """The shape the parser hands to store_transactions."""
    return [
        {
            "date": (datetime.date(2026, 9, start_day)
                     + datetime.timedelta(days=i)).isoformat(),
            "description": n,
            "raw_text": n,
            "debit": 1500.00 + i,
            "credit": None,
            "balance": 900000.00 - (i * 1500),
            "row_index": i,
        }
        for i, n in enumerate(narrations)
    ]


def _ingest(user_id, account_id, narrations, start_day=1):
    db = TestingSessionLocal()
    try:
        stored = TransactionStorageService().store_transactions(
            db=db,
            processed_txns=_statement_rows(narrations, start_day),
            user_id=user_id,
            account_id=account_id,
            source_channel="MANUAL_UPLOAD",
        )
        db.commit()
        return len(stored)
    finally:
        db.close()


def _rows_for(user_id, fragment):
    db = TestingSessionLocal()
    try:
        out = []
        for tx in db.query(Transaction).filter(Transaction.user_id == user_id).all():
            narration = tx.narration_clean or tx.narration_raw or ""
            if fragment in narration.upper():
                pred = db.query(Prediction).filter(
                    Prediction.transaction_id == tx.id).first()
                out.append({
                    "narration": narration,
                    "category_id": tx.category_id,
                    "purpose": tx.legacy_category,
                    "requires_review": pred.requires_review if pred else None,
                    "method": pred.classification_method if pred else None,
                    "explanation": pred.explanation if pred else None,
                })
        return out
    finally:
        db.close()


def test_an_unknown_counterparty_goes_to_review(auth, account):
    """Baseline. Without a decision on file, an UNREADABLE name is not guessed at.

    The name here changed when trade-name rules arrived. `KUMAR FISH` used to
    be the example of an unknown counterparty and is no longer unknown: the
    name says the party sells fish, and reading it is the point of that
    feature. `PRAKASH AGENCIES` is the case this test is actually about —
    corporate boilerplate around a person's name, saying nothing about what the
    money was for. Those must still reach a human.
    """
    _headers, user_id = auth
    _ingest(user_id, account, [
        "EBANK:WIB/1501906475/PRAKASH AGENCIES",
        "EBANK:WIB/1501906476/PRAKASH AGENCIES",
    ])
    rows = _rows_for(user_id, "PRAKASH AGENCIES")
    assert len(rows) == 2
    assert all(r["category_id"] is None for r in rows)
    assert all(r["requires_review"] for r in rows)


def test_a_name_that_states_a_trade_does_not_go_to_review(auth, account):
    """The other side of the line above, ingested end to end.

    73 counterparties waiting on a person for one month's statement is the
    problem this removes, and most of them named a trade.
    """
    _headers, user_id = auth
    _ingest(user_id, account, ["EBANK:WIB/1501906480/KUMAR FISH"])
    rows = _rows_for(user_id, "KUMAR FISH")
    assert len(rows) == 1
    assert rows[0]["category_id"] is not None
    assert rows[0]["requires_review"] is False
    assert "FISH" in (rows[0]["explanation"] or "")


def test_a_remembered_counterparty_is_categorised_on_upload(auth, account):
    """The whole point: decide once, and the next upload needs no decision."""
    _headers, user_id = auth

    db = TestingSessionLocal()
    try:
        remember(db, user_id, "EBANK:WIB/1501906475/KUMAR FISH",
                 category="Cost of Goods", event_type=None)
        db.commit()
    finally:
        db.close()

    # A NEW statement, with reference numbers this user has never seen.
    _ingest(user_id, account, [
        "EBANK:WIB/9900000001/KUMAR FISH",
        "NEFT-HDFCH99887766-KUMAR FISH-HDFC BANK LTD.",
        "EBANK:WIB/9900000003/KUMAR FISH",
    ], start_day=5)

    rows = _rows_for(user_id, "KUMAR FISH")
    assert len(rows) == 3
    for r in rows:
        # A category name alone is not enough — the FK has to resolve, or the
        # row is stored uncategorised and the memory achieved nothing.
        assert r["category_id"] is not None, f"no category_id on {r['narration']}"
        assert r["requires_review"] is False
        assert r["method"] == "counterparty_memory"
        assert "you have already categorised" in (r["explanation"] or "")

    # Reached across a different payment rail too: the memory is keyed on the
    # party, not on how the money moved.
    assert any("NEFT" in r["narration"] for r in rows)


def test_the_memory_never_overrides_a_rule_that_fired(auth, account):
    """A bank charge is a fact about the transaction, not about who was paid.

    If the memory could overwrite a fired rule, one careless decision about a
    counterparty would start relabelling unrelated charges.
    """
    _headers, user_id = auth
    db = TestingSessionLocal()
    try:
        remember(db, user_id, "EBANK:WIB/1/KUMAR FISH", category="Cost of Goods")
        db.commit()
    finally:
        db.close()

    _ingest(user_id, account, ["SERVICE CHARGE FOR AUGUST 2026"], start_day=9)
    rows = _rows_for(user_id, "SERVICE CHARGE")
    assert len(rows) == 1
    assert rows[0]["method"] != "counterparty_memory"


def test_only_the_deciding_users_uploads_are_affected(client, auth, account):
    """One account holder's supplier list must not classify another's ledger."""
    _headers, user_a = auth
    db = TestingSessionLocal()
    try:
        remember(db, user_a, "EBANK:WIB/1/SHIVKUMAR VEG", category="Cost of Goods")
        db.commit()
    finally:
        db.close()

    email = f"cpu2_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    tok = client.post("/auth/login",
                      json={"email": email, "password": "Password123!"}).json()["access_token"]
    hdrs_b = {"Authorization": f"Bearer {tok}"}
    user_b = uuid.UUID(client.get("/auth/me", headers=hdrs_b).json()["id"])
    register_bank_account(client, hdrs_b,
                          account_number=f"5030{uuid.uuid4().int % 10**8:08d}")
    db = TestingSessionLocal()
    try:
        acct_b = db.query(Account).filter(Account.user_id == user_b).first().id
    finally:
        db.close()

    try:
        _ingest(user_b, acct_b, ["EBANK:WIB/7777777/SHIVKUMAR VEG"], start_day=12)
        rows = _rows_for(user_b, "SHIVKUMAR VEG")
        assert len(rows) == 1
        # The assertion moved from "user B's row has no category" to "user B's
        # row was not categorised BY USER A'S DECISION", and the second is what
        # this test was always about. Since trade names arrived, `SHIVKUMAR
        # VEG` is categorised for anyone — the name says it sells vegetables —
        # so an absent category no longer distinguishes isolation from
        # ignorance. Provenance does.
        assert rows[0]["method"] != METHOD_COUNTERPARTY
        assert "already categorised" not in (rows[0]["explanation"] or "")
    finally:
        db = TestingSessionLocal()
        try:
            db.query(Prediction).filter(Prediction.transaction_id.in_(
                db.query(Transaction.id).filter(Transaction.user_id == user_b)
            )).delete(synchronize_session=False)
            db.query(Transaction).filter(
                Transaction.user_id == user_b).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()


@pytest.mark.parametrize("category", [
    "Cost of Goods", "Sales Income", "Rent & Premises", "Professional Fees",
    "Owner Funding", "Loans & Borrowing", "Interest & Finance Cost",
    "Taxes & Statutory", "Internal Movement", "Utilities & Bills",
])
def test_every_business_category_resolves_to_a_real_category_row(auth, account, category):
    """Guards the seam between the memory and the Category table.

    store_transactions looks the remembered name up in the seeded category map.
    A name the map does not contain yields category_id=None — the transaction
    would be stored uncategorised while the UI reported it as handled. That is
    the worst possible outcome here, so every category the user can pick is
    checked against the real lookup.
    """
    _headers, user_id = auth
    narration = f"EBANK:WIB/1/SUPPLIER {category.replace(' ', '')[:12]}"
    db = TestingSessionLocal()
    try:
        remember(db, user_id, narration, category=category)
        db.commit()
    finally:
        db.close()

    _ingest(user_id, account, [narration], start_day=15)
    rows = _rows_for(user_id, "SUPPLIER")
    assert len(rows) == 1
    assert rows[0]["method"] == "counterparty_memory"
    assert rows[0]["category_id"] is not None, (
        f"'{category}' has no row in the Category table, so a transaction "
        f"remembered under it is stored with no category at all"
    )
