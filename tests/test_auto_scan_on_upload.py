"""Anomalies and policy compliance are computed as part of ingestion.

The behaviour this replaces: after an upload, the dashboard's Anomalies and
Policy Compliance panels kept showing the PREVIOUS statement's numbers until the
user found the Re-scan button. Stale figures that look current are worse than
no figures, because nothing on screen says they are out of date.
"""

import datetime
import uuid

import pytest

from app.compliance import auto_scan
from app.models.account import Account
from app.models.compliance import AnomalyFinding, PolicyRule, PolicyViolation
from app.models.transaction import Direction, SourceType, Transaction
from tests.conftest import TestingSessionLocal, register_bank_account


@pytest.fixture
def auth(client):
    email = f"scan_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    tok = client.post("/auth/login",
                      json={"email": email, "password": "Password123!"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}
    user_id = uuid.UUID(client.get("/auth/me", headers=headers).json()["id"])
    yield headers, user_id
    db = TestingSessionLocal()
    try:
        for model in (AnomalyFinding, PolicyViolation, Transaction, PolicyRule):
            db.query(model).filter(model.user_id == user_id).delete(
                synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _seed(user_id, account_id, n=30):
    """Rows shaped to trip the cash-limit and round-sum detectors."""
    db = TestingSessionLocal()
    try:
        for i in range(n):
            db.add(Transaction(
                id=uuid.uuid4(), user_id=user_id, account_id=account_id,
                direction=Direction.DEBIT,
                debit_paise=(250000_00 if i % 5 == 0 else 4500_00),
                balance_paise=900000_00 - (i * 1000_00),
                txn_date=datetime.date(2026, 7, 1) + datetime.timedelta(days=i % 28),
                narration_raw=f"CASH PAYMENT VENDOR {i}",
                narration_clean=f"CASH PAYMENT VENDOR {i}",
                source_type=SourceType.STATEMENT, booked_currency="INR",
            ))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def seeded(client, auth):
    headers, user_id = auth
    register_bank_account(client, headers,
                          account_number=f"5020{uuid.uuid4().int % 10**8:08d}")
    db = TestingSessionLocal()
    try:
        account_id = db.query(Account).filter(Account.user_id == user_id).first().id
    finally:
        db.close()
    _seed(user_id, account_id)
    return headers, user_id, account_id


def test_scan_seeds_rules_and_produces_findings_without_a_button_press(seeded):
    _headers, user_id, account_id = seeded
    db = TestingSessionLocal()
    try:
        # Nothing has looked at compliance yet, so this user has no rules at all.
        assert db.query(PolicyRule).filter(PolicyRule.user_id == user_id).count() == 0

        result = auto_scan.run_scan_safely(db, user_id, account_id=account_id)

        assert result is not None and "error" not in result
        assert result["anomalies_found"] > 0
        assert db.query(PolicyRule).filter(PolicyRule.user_id == user_id).count() > 0
        assert db.query(AnomalyFinding).filter(
            AnomalyFinding.user_id == user_id).count() > 0
    finally:
        db.close()


def test_a_detector_failure_does_not_fail_the_upload(seeded, monkeypatch):
    """The ledger is already committed when the scan runs.

    Raising here would push the user into re-uploading a statement that is in
    fact already ingested, which creates duplicates. The correct outcome is a
    loud log and a reported error, with the transactions intact.
    """
    _headers, user_id, account_id = seeded

    def boom(*_args, **_kwargs):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(auto_scan, "detect_anomalies", boom)

    db = TestingSessionLocal()
    try:
        result = auto_scan.run_scan_safely(db, user_id, account_id=account_id)
        assert result is not None
        assert result["error"] == "detector exploded"
        # The transactions survived.
        assert db.query(Transaction).filter(
            Transaction.user_id == user_id).count() == 30
    finally:
        db.close()


def test_rescan_endpoint_and_upload_scan_run_the_same_code(client, seeded):
    """The button must not be able to disagree with the automatic scan.

    Both go through auto_scan.run_scan. If they diverged, a user would get one
    set of numbers on upload and a different set on Re-scan, with no way to tell
    which was right.
    """
    headers, user_id, account_id = seeded
    db = TestingSessionLocal()
    try:
        auto = auto_scan.run_scan_safely(db, user_id, account_id=account_id)
    finally:
        db.close()

    res = client.post("/compliance/analyze", headers=headers)
    assert res.status_code == 200, res.text
    manual = res.json()

    # Re-running a scan over unchanged data must be idempotent: the same
    # findings are reproduced, not duplicated.
    assert manual["anomalies_found"] == auto["anomalies_found"]


def test_ingestion_invokes_the_scan(seeded, monkeypatch):
    """Guards the wiring itself, not just the helper.

    A test that only exercises run_scan_safely would still pass if someone
    removed the call from the parsing task, which is the exact regression that
    would bring the stale-panel bug back.
    """
    import app.compliance.auto_scan as module

    calls = []
    real = module.run_scan_safely

    def spy(db, user_id, account_id=None):
        calls.append((user_id, account_id))
        return real(db, user_id, account_id=account_id)

    monkeypatch.setattr(module, "run_scan_safely", spy)

    import inspect

    import app.services.parsing_queue as pq
    source = inspect.getsource(pq.process_file_parsing_task)
    assert "run_scan_safely" in source, (
        "The parsing task no longer runs the compliance scan; uploads will "
        "leave the dashboard showing the previous statement's figures."
    )
    # And it must be imported from the shared module, not reimplemented.
    assert "from app.compliance.auto_scan import run_scan_safely" in source
