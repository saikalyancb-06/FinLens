"""Clearing transactions must clear what was derived from them.

The reported symptom was findings from a previous upload still showing after a
clear. Both findings tables declare ON DELETE CASCADE from `transaction_id`, so
findings naming a single transaction did go — but the interesting ones do not
name a transaction at all. A balance discontinuity, a structuring pattern, an
unreconciled total and an account-level policy breach are each properties of a
*set* of rows, so there is no single id for the database to follow, and they
survived indefinitely.

Measured on a real clear before the fix: 121 transactions deleted, 13 anomalies
and 3 violations left behind, plus 9 policy rules still holding the cached
counts the dashboard's compliance percentage is read from.
"""

import datetime
import uuid

import pytest
from sqlalchemy import func, select

from app.compliance.anomaly_engine import detect_anomalies
from app.compliance.policy_engine import evaluate_policies
from app.compliance.rules_seed import seed_policy_rules
from app.models.account import Account
from app.models.compliance import AnomalyFinding, PolicyRule, PolicyViolation
from app.models.transaction import Direction, SourceType, Transaction
from tests.conftest import TestingSessionLocal


@pytest.fixture
def auth(client):
    email = f"clr_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    tok = client.post("/auth/login",
                      json={"email": email, "password": "Password123!"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}
    user_id = uuid.UUID(client.get("/auth/me", headers=headers).json()["id"])
    return headers, user_id


def _seed(user_id, account_id=None, n=30):
    """Transactions shaped to trip several detectors, including set-level ones."""
    db = TestingSessionLocal()
    try:
        if account_id is None:
            # The rows need a ledger to belong to. A running balance is only a
            # sequence within one statement or one account, so an unbound row
            # is not something the balance detector can reason about.
            account = Account(id=uuid.uuid4(), user_id=user_id, bank_code="HDFC",
                              account_number_masked="****9999",
                              account_type="CURRENT", currency="INR")
            db.add(account)
            db.commit()
            account_id = account.id

        for i in range(n):
            db.add(Transaction(
                id=uuid.uuid4(), user_id=user_id, account_id=account_id,
                direction=Direction.DEBIT,
                # Every fifth row is a large round cash payment: trips the cash
                # limit rules, the round-sum detector and the structuring bands.
                debit_paise=(250000_00 if i % 5 == 0 else 4500_00),
                # A deliberately inconsistent running balance, so the
                # balance-discontinuity detector fires.
                balance_paise=900000_00 - (i * 1000_00) + (777_00 if i % 7 == 0 else 0),
                # Two clusters of activity either side of a five-month hole.
                # The gap is what produces a finding that names NO transaction:
                # a missing-activity window is a property of the pair of rows
                # bracketing it, not of any single row, so it carries a NULL
                # transaction_id and no foreign key can cascade to it. That
                # shape is the whole point of this test.
                txn_date=(datetime.date(2026, 1, 5) + datetime.timedelta(days=i)
                          if i < n // 2
                          else datetime.date(2026, 7, 1) + datetime.timedelta(days=i - n // 2)),
                narration_raw=f"CASH PAYMENT VENDOR {i}",
                narration_clean=f"CASH PAYMENT VENDOR {i}",
                source_type=SourceType.STATEMENT, booked_currency="INR",
            ))
        db.commit()
        # A fresh user has no rules until they are seeded, and an unseeded user
        # has nothing to evaluate — so without this the cached-stats assertions
        # would pass vacuously.
        seed_policy_rules(db, user_id)
        db.commit()
        detect_anomalies(db, user_id)
        evaluate_policies(db, user_id)
        db.commit()
    finally:
        db.close()


def _counts(user_id):
    db = TestingSessionLocal()
    try:
        c = lambda m: db.execute(
            select(func.count()).select_from(m).where(m.user_id == user_id)).scalar()
        return {
            "txns": c(Transaction),
            "anomalies": c(AnomalyFinding),
            "violations": c(PolicyViolation),
            "rules_with_cached_stats": db.execute(
                select(func.count()).select_from(PolicyRule).where(
                    PolicyRule.user_id == user_id,
                    PolicyRule.last_applicable.isnot(None))).scalar(),
        }
    finally:
        db.close()


def test_clearing_everything_leaves_no_findings_behind(client, auth):
    headers, user_id = auth
    _seed(user_id)

    before = _counts(user_id)
    assert before["txns"] > 0
    assert before["anomalies"] > 0 or before["violations"] > 0

    assert client.delete("/transactions/clear", headers=headers).status_code == 200

    after = _counts(user_id)
    assert after == {"txns": 0, "anomalies": 0, "violations": 0,
                     "rules_with_cached_stats": 0}


def test_findings_that_name_no_transaction_are_also_removed(client, auth):
    """The specific gap: FK cascade cannot reach these, so the endpoint must."""
    headers, user_id = auth
    _seed(user_id)

    db = TestingSessionLocal()
    try:
        orphanable = db.execute(
            select(func.count()).select_from(AnomalyFinding).where(
                AnomalyFinding.user_id == user_id,
                AnomalyFinding.transaction_id.is_(None))).scalar()
    finally:
        db.close()
    assert orphanable > 0, "fixture no longer produces set-level findings"

    client.delete("/transactions/clear", headers=headers)
    assert _counts(user_id)["anomalies"] == 0


def test_the_dashboard_stops_reporting_a_pass_rate_it_can_no_longer_justify(client, auth):
    """A compliance percentage over deleted transactions is worse than none."""
    headers, user_id = auth
    _seed(user_id)
    assert _counts(user_id)["rules_with_cached_stats"] > 0

    client.delete("/transactions/clear", headers=headers)

    db = TestingSessionLocal()
    try:
        stale = db.execute(select(PolicyRule).where(
            PolicyRule.user_id == user_id,
            PolicyRule.last_evaluated_at.isnot(None))).scalars().all()
    finally:
        db.close()
    assert not stale

    assert client.get("/compliance/overview", headers=headers).status_code == 200


def test_an_account_scoped_clear_leaves_other_accounts_findings_alone(client, auth):
    """Deleting one account's data must not wipe the rest of the user's."""
    headers, user_id = auth

    db = TestingSessionLocal()
    try:
        kept = Account(id=uuid.uuid4(), user_id=user_id, bank_code="HDFC",
                       account_number_masked="****1111", account_type="CURRENT",
                       currency="INR")
        cleared = Account(id=uuid.uuid4(), user_id=user_id, bank_code="ICICI",
                          account_number_masked="****2222", account_type="CURRENT",
                          currency="INR")
        db.add_all([kept, cleared])
        db.commit()
        kept_id, cleared_id = kept.id, cleared.id

        # One finding per account, each naming its account and no transaction —
        # exactly the shape the FK cascade cannot see.
        for acc, fp in ((kept_id, "keep-me"), (cleared_id, "clear-me")):
            db.add(AnomalyFinding(
                id=uuid.uuid4(), user_id=user_id, account_id=acc,
                anomaly_type="balance_discontinuity", severity="high",
                title="Balance jump", detail="test", status="open", fingerprint=fp))
        db.commit()
    finally:
        db.close()

    client.delete(f"/transactions/clear?account_id={cleared_id}", headers=headers)

    db = TestingSessionLocal()
    try:
        remaining = {f.fingerprint for f in db.execute(
            select(AnomalyFinding).where(AnomalyFinding.user_id == user_id)).scalars()}
    finally:
        db.close()
    assert remaining == {"keep-me"}
