"""Re-categorising rows that are already in the ledger.

A transaction keeps the category it got at upload time. Before this existed, an
improvement to the rules only helped the NEXT upload: a statement imported
before a rule was written stayed uncategorised, and the only way to benefit was
to clear the account and re-import — destroying every review decision made since.
"""

import datetime
import uuid

import pytest

from app.categorization.counterparty_memory import remember
from app.models.account import Account
from app.models.category import Category
from app.models.counterparty_memory import CounterpartyMemory
from app.models.prediction import Prediction
from app.models.transaction import Direction, SourceType, Transaction
from tests.conftest import TestingSessionLocal, register_bank_account


@pytest.fixture
def auth(client):
    email = f"rec_{uuid.uuid4().hex[:6]}@example.com"
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
                          account_number=f"5040{uuid.uuid4().int % 10**8:08d}")
    db = TestingSessionLocal()
    try:
        return db.query(Account).filter(Account.user_id == user_id).first().id
    finally:
        db.close()


def _seed_uncategorized(user_id, account_id, narrations, method=None):
    """Rows as an OLDER classifier would have left them: no category at all."""
    db = TestingSessionLocal()
    try:
        ids = []
        for i, n in enumerate(narrations):
            tx = Transaction(
                id=uuid.uuid4(), user_id=user_id, account_id=account_id,
                direction=Direction.DEBIT, debit_paise=3000_00 + i,
                balance_paise=800000_00,
                txn_date=datetime.date(2026, 6, 1) + datetime.timedelta(days=i),
                narration_raw=n, narration_clean=n.upper(),
                source_type=SourceType.STATEMENT, booked_currency="INR",
                category_id=None,
            )
            db.add(tx)
            db.flush()
            db.add(Prediction(
                id=uuid.uuid4(), transaction_id=tx.id,
                predicted_category="Uncategorized", confidence=0.0,
                classification_method=method, requires_review=True,
            ))
            ids.append(tx.id)
        db.commit()
        return ids
    finally:
        db.close()


def _state(user_id):
    db = TestingSessionLocal()
    try:
        out = {}
        for tx in db.query(Transaction).filter(Transaction.user_id == user_id).all():
            pred = db.query(Prediction).filter(
                Prediction.transaction_id == tx.id).first()
            out[(tx.narration_clean or "")[:60]] = {
                "has_category": tx.category_id is not None,
                "purpose": tx.legacy_category,
                "method": pred.classification_method if pred else None,
                "requires_review": pred.requires_review if pred else None,
            }
        return out
    finally:
        db.close()


def test_rules_written_after_the_upload_are_applied_to_existing_rows(client, auth, account):
    """The gap this closes.

    These narrations are matched by rules that exist NOW. Rows imported before
    those rules were written carry no category, and nothing would ever fix them.
    """
    headers, user_id = auth
    _seed_uncategorized(user_id, account, [
        "EAZYDINER PRIVATE LIMITED SETTLEMENT",
        "SERVICE CHARGE FOR JUNE 2026",
        "BY CASH 4432",
    ])
    before = _state(user_id)
    assert all(not v["has_category"] for v in before.values())

    res = client.post("/v1/review-queue/recategorize", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["examined"] == 3
    # All three match a rule, and they split on whether the narration states a
    # FACT or implies one:
    #
    #   SERVICE CHARGE FOR JUNE 2026   the bank naming its own charge — certain
    #   BY CASH 4432                   a cash deposit — certain
    #   EAZYDINER ... SETTLEMENT       an aggregator settled money in, so this
    #                                  is PROBABLY revenue — a judgement
    #
    # Only the last one is queued. The first two used to be, and 350+ rows of
    # exactly that shape were what made the review queue unusable.
    assert body["categorized"] == 3
    assert body["provisional"] == 1

    after = _state(user_id)
    assert all(v["has_category"] for v in after.values())
    # A provisional row is categorised AND still in the review queue. Both halves
    # matter: dropping it from review would hide an unconfirmed guess.
    assert sum(1 for v in after.values() if v["requires_review"]) == 1


def test_saved_counterparties_are_applied_to_existing_rows(client, auth, account):
    """The behaviour the user actually asked for.

    Deciding about a counterparty must reach the transactions ALREADY in the
    database, not only the ones uploaded afterwards.
    """
    headers, user_id = auth
    _seed_uncategorized(user_id, account, [
        "EBANK:WIB/1501906475/KUMAR FISH",
        "EBANK:WIB/1501906476/KUMAR FISH",
        "NEFT-HDFCH123-KUMAR FISH-HDFC BANK LTD.",
    ])

    db = TestingSessionLocal()
    try:
        remember(db, user_id, "EBANK:WIB/1/KUMAR FISH", category="Cost of Goods")
        db.commit()
    finally:
        db.close()

    res = client.post("/v1/review-queue/recategorize", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["categorized"] == 3
    assert body["by_method"].get("counterparty_memory") == 3

    after = _state(user_id)
    for v in after.values():
        assert v["has_category"] is True
        assert v["purpose"] == "Cost of Goods"
        assert v["requires_review"] is False


def test_a_manual_decision_is_never_overwritten(client, auth, account):
    """Losing a morning of review work to a re-scan is worse than a stale label."""
    headers, user_id = auth
    _seed_uncategorized(user_id, account, ["EBANK:WIB/1/KUMAR FISH"])

    # The user reviews it and calls it Travel — an answer no rule would produce.
    items = client.get("/v1/review-queue?limit=50", headers=headers).json()
    client.patch(f"/v1/review-queue/{items[0]['transaction_id']}",
                 headers=headers, json={"category": "Travel"})

    # Now a rule and a memory entry both disagree with the user.
    db = TestingSessionLocal()
    try:
        db.query(CounterpartyMemory).filter(
            CounterpartyMemory.user_id == user_id).delete(synchronize_session=False)
        db.commit()
        remember(db, user_id, "EBANK:WIB/1/KUMAR FISH", category="Cost of Goods")
        db.commit()
    finally:
        db.close()

    res = client.post("/v1/review-queue/recategorize?force=true", headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["skipped_manual"] == 1

    after = _state(user_id)
    assert list(after.values())[0]["purpose"] == "Travel"


def test_rows_that_still_cannot_be_decided_are_reported_honestly(client, auth, account):
    """A re-run that fixes nothing must say so, not report silent success."""
    headers, user_id = auth
    _seed_uncategorized(user_id, account, [
        "NEFT-BARBZ99-SOME UNKNOWN SUPPLIER-CANARA BANK",
        "NEFT-BARBZ98-ANOTHER UNKNOWN PARTY-AXIS BANK",
    ])
    res = client.post("/v1/review-queue/recategorize", headers=headers)
    body = res.json()
    assert body["categorized"] == 0
    assert body["still_unresolved"] == 2
    # And it names them, so the user can see WHAT is still open.
    assert len(body["unresolved_samples"]) == 2


def test_recategorize_is_idempotent(client, auth, account):
    """Running it twice must not double-count or churn."""
    headers, user_id = auth
    _seed_uncategorized(user_id, account, ["SERVICE CHARGE FOR JUNE 2026"])

    first = client.post("/v1/review-queue/recategorize", headers=headers).json()
    assert first["categorized"] == 1

    second = client.post("/v1/review-queue/recategorize", headers=headers).json()
    # Nothing is left in scope, so the second run examines nothing.
    assert second["categorized"] == 0
    assert second["examined"] == 0


def test_one_users_rerun_does_not_touch_another_users_ledger(client, auth, account):
    headers, user_a = auth
    _seed_uncategorized(user_a, account, ["SERVICE CHARGE FOR JUNE 2026"])

    email = f"rec2_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    tok = client.post("/auth/login",
                      json={"email": email, "password": "Password123!"}).json()["access_token"]
    hdrs_b = {"Authorization": f"Bearer {tok}"}
    user_b = uuid.UUID(client.get("/auth/me", headers=hdrs_b).json()["id"])
    register_bank_account(client, hdrs_b,
                          account_number=f"5050{uuid.uuid4().int % 10**8:08d}")
    db = TestingSessionLocal()
    try:
        acct_b = db.query(Account).filter(Account.user_id == user_b).first().id
    finally:
        db.close()
    _seed_uncategorized(user_b, acct_b, ["SERVICE CHARGE FOR JUNE 2026"])

    try:
        res = client.post("/v1/review-queue/recategorize", headers=headers).json()
        assert res["examined"] == 1  # only user A's row
        assert _state(user_b)["SERVICE CHARGE FOR JUNE 2026"]["has_category"] is False
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
