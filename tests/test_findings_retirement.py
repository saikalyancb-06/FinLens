"""A rescan must retire what it no longer reproduces.

Both engines upsert on a fingerprint, which refreshes a finding that still
reproduces but did nothing about one that stopped. Findings tied to a single
transaction disappear with it via the foreign key — but the ones that matter
most name no transaction at all. A balance discontinuity, a structuring
pattern, an unreconciled total, a per-day aggregate breach: each is a property
of a *set* of rows, so nothing removed them, and they stayed open on the
dashboard beside findings from data that had since been corrected.

Retired means `resolved`, not deleted. This is a compliance tool: that something
was once flagged and later stopped reproducing is itself a fact worth keeping,
and a silent delete makes an auditor's question unanswerable.
"""

import datetime
import uuid

import pytest
from sqlalchemy import select

from app.compliance.anomaly_engine import detect_anomalies
from app.compliance.policy_engine import evaluate_policies
from app.compliance.rules_seed import seed_policy_rules
from app.models.account import Account
from app.models.compliance import AnomalyFinding, PolicyViolation
from app.models.transaction import Direction, SourceType, Transaction
from tests.conftest import TestingSessionLocal


@pytest.fixture
def user_id(client):
    email = f"ret_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    tok = client.post("/auth/login",
                      json={"email": email, "password": "Password123!"}).json()["access_token"]
    return uuid.UUID(client.get(
        "/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()["id"])


def _finding(db, user_id, fp, status="open", occurred=None, account_id=None):
    """A set-level finding: no transaction_id, so no FK cascade can reach it."""
    db.add(AnomalyFinding(
        id=uuid.uuid4(), user_id=user_id, account_id=account_id,
        anomaly_type="balance_discontinuity", severity="high", status=status,
        title=f"Set-level finding {fp}", detail="from a previous upload",
        occurred_on=occurred or datetime.date(2026, 5, 10), fingerprint=fp))


def _clean_transactions(db, user_id, account_id=None, n=6):
    """Ordinary rows that trip no detector."""
    for i in range(n):
        db.add(Transaction(
            id=uuid.uuid4(), user_id=user_id, account_id=account_id,
            direction=Direction.DEBIT, debit_paise=1234_00,
            balance_paise=500000_00 - i * 1234_00,
            txn_date=datetime.date(2026, 6, 1) + datetime.timedelta(days=i),
            narration_raw=f"UPI/PAY/{i}/VENDOR", narration_clean=f"UPI PAY VENDOR {i}",
            source_type=SourceType.STATEMENT, booked_currency="INR"))


def _status(user_id, fp):
    db = TestingSessionLocal()
    try:
        row = db.execute(select(AnomalyFinding).where(
            AnomalyFinding.user_id == user_id,
            AnomalyFinding.fingerprint == fp)).scalar_one_or_none()
        return (row.status, row.resolved_at) if row else (None, None)
    finally:
        db.close()


def test_a_finding_the_rescan_does_not_reproduce_is_resolved(user_id):
    db = TestingSessionLocal()
    try:
        _finding(db, user_id, "stale-open", "open")
        _finding(db, user_id, "stale-ack", "acknowledged")
        _clean_transactions(db, user_id)
        db.commit()
        detect_anomalies(db, user_id)
        db.commit()
    finally:
        db.close()

    for fp in ("stale-open", "stale-ack"):
        status, resolved_at = _status(user_id, fp)
        assert status == "resolved", fp
        assert resolved_at is not None, fp


def test_the_users_own_judgement_is_never_overwritten(user_id):
    """`false_positive` is a decision about the finding, not an observation."""
    db = TestingSessionLocal()
    try:
        _finding(db, user_id, "user-waived", "false_positive")
        _clean_transactions(db, user_id)
        db.commit()
        detect_anomalies(db, user_id)
        db.commit()
    finally:
        db.close()

    status, resolved_at = _status(user_id, "user-waived")
    assert status == "false_positive"
    assert resolved_at is None


def test_a_scan_that_finds_nothing_still_retires_everything(user_id):
    """The worst case: the old code returned early and left every finding open."""
    db = TestingSessionLocal()
    try:
        _finding(db, user_id, "orphan-1")
        _finding(db, user_id, "orphan-2")
        db.commit()
        detect_anomalies(db, user_id)      # no transactions at all
        db.commit()
    finally:
        db.close()

    assert _status(user_id, "orphan-1")[0] == "resolved"
    assert _status(user_id, "orphan-2")[0] == "resolved"


def test_a_scoped_scan_does_not_resolve_what_it_never_looked_at(user_id):
    """Scanning one account must not clear another account's findings.

    A scoped scan sees part of the picture; resolving what it could not look at
    would retire real findings on the strength of never having checked them.
    """
    db = TestingSessionLocal()
    try:
        scanned = Account(id=uuid.uuid4(), user_id=user_id, bank_code="HDFC",
                          account_number_masked="****1111", account_type="CURRENT",
                          currency="INR")
        untouched = Account(id=uuid.uuid4(), user_id=user_id, bank_code="ICICI",
                            account_number_masked="****2222", account_type="CURRENT",
                            currency="INR")
        db.add_all([scanned, untouched])
        db.commit()
        scanned_id, untouched_id = scanned.id, untouched.id

        _finding(db, user_id, "in-scope", account_id=scanned_id)
        _finding(db, user_id, "out-of-scope", account_id=untouched_id)
        _clean_transactions(db, user_id, account_id=scanned_id)
        db.commit()

        detect_anomalies(db, user_id, account_id=scanned_id)
        db.commit()
    finally:
        db.close()

    assert _status(user_id, "in-scope")[0] == "resolved"
    assert _status(user_id, "out-of-scope")[0] == "open"


def test_a_date_scoped_scan_leaves_other_periods_alone(user_id):
    db = TestingSessionLocal()
    try:
        _finding(db, user_id, "inside-window", occurred=datetime.date(2026, 6, 3))
        _finding(db, user_id, "before-window", occurred=datetime.date(2026, 1, 3))
        _clean_transactions(db, user_id)
        db.commit()
        detect_anomalies(db, user_id,
                         date_from=datetime.date(2026, 6, 1),
                         date_to=datetime.date(2026, 6, 30))
        db.commit()
    finally:
        db.close()

    assert _status(user_id, "inside-window")[0] == "resolved"
    assert _status(user_id, "before-window")[0] == "open"


def test_a_finding_that_still_reproduces_stays_open(user_id):
    """Retirement must not sweep away live findings along with stale ones."""
    db = TestingSessionLocal()
    try:
        # A running balance is a property of one ledger, so the rows have to say
        # which: the balance detector compares within a statement, or — for rows
        # that arrived without one, as the Account Aggregator feed and the
        # transactions API both do — within an account. Rows bound to neither
        # could come from different accounts and are not a sequence at all.
        account = Account(id=uuid.uuid4(), user_id=user_id, bank_code="HDFC",
                          account_number_masked="****3333", account_type="CURRENT",
                          currency="INR")
        db.add(account)
        db.commit()
        account_id = account.id

        # Large round cash payments with a broken running balance: reproduces.
        for i in range(9):
            db.add(Transaction(
                id=uuid.uuid4(), user_id=user_id, account_id=account_id,
                direction=Direction.DEBIT,
                debit_paise=250000_00,
                balance_paise=900000_00 - i * 1000_00 + (777_00 if i % 3 == 0 else 0),
                txn_date=datetime.date(2026, 5, 1) + datetime.timedelta(days=i),
                narration_raw=f"CASH PAYMENT VENDOR {i}",
                narration_clean=f"CASH PAYMENT VENDOR {i}",
                source_type=SourceType.STATEMENT, booked_currency="INR"))
        db.commit()
        detect_anomalies(db, user_id)
        db.commit()

        first = db.execute(select(AnomalyFinding).where(
            AnomalyFinding.user_id == user_id,
            AnomalyFinding.status == "open")).scalars().all()
        assert first, "fixture no longer produces findings"

        detect_anomalies(db, user_id)      # same data, run again
        db.commit()

        still_open = db.execute(select(AnomalyFinding).where(
            AnomalyFinding.user_id == user_id,
            AnomalyFinding.status == "open")).scalars().all()
    finally:
        db.close()

    assert len(still_open) == len(first)


def test_policy_violations_are_retired_the_same_way(user_id):
    db = TestingSessionLocal()
    try:
        seed_policy_rules(db, user_id)
        db.commit()
        for i in range(9):
            db.add(Transaction(
                id=uuid.uuid4(), user_id=user_id, direction=Direction.DEBIT,
                debit_paise=250000_00, balance_paise=900000_00 - i * 1000_00,
                txn_date=datetime.date(2026, 5, 1) + datetime.timedelta(days=i),
                narration_raw=f"CASH PAYMENT VENDOR {i}",
                narration_clean=f"CASH PAYMENT VENDOR {i}",
                source_type=SourceType.STATEMENT, booked_currency="INR"))
        db.commit()
        evaluate_policies(db, user_id)
        db.commit()

        original = {v.fingerprint for v in db.execute(select(PolicyViolation).where(
            PolicyViolation.user_id == user_id,
            PolicyViolation.status == "open")).scalars()}
        assert original, "fixture no longer produces violations"

        # Replace the offending rows with innocuous ones and re-evaluate.
        db.query(Transaction).filter(Transaction.user_id == user_id).delete(
            synchronize_session=False)
        _clean_transactions(db, user_id)
        db.commit()
        evaluate_policies(db, user_id)
        db.commit()

        # Asserted on the original fingerprints, not on "no open violations":
        # the replacement rows span a weekend, so the weekend-payment rule
        # legitimately opens a new one. That is a live finding, not a stale one.
        still_open = {v.fingerprint for v in db.execute(select(PolicyViolation).where(
            PolicyViolation.user_id == user_id,
            PolicyViolation.status == "open")).scalars()}
    finally:
        db.close()

    assert not (original & still_open)
