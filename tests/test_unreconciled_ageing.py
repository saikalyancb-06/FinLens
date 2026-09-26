"""Ageing of the bank reconciliation bridge."""

import uuid
from datetime import date, timedelta

import pytest

from tests.conftest import TestingSessionLocal


@pytest.fixture
def auth_headers(client):
    email = f"age_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    tok = client.post("/auth/login", json={"email": email, "password": "Password123!"}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _user_id(client, headers):
    return client.get("/auth/me", headers=headers).json()["id"]


def _build_run(user_id, ages_and_sides):
    """One reconciliation run holding items of the given ages."""
    from app.models.account import Account
    from app.models.reconciliation import ReconciliationItem, ReconciliationRun

    db = TestingSessionLocal()
    try:
        acc = Account(id=uuid.uuid4(), user_id=user_id, bank_code="HDFC",
                      account_number_masked="****9001", account_type="CURRENT",
                      currency="INR")
        db.add(acc)
        db.flush()

        run = ReconciliationRun(
            id=uuid.uuid4(), user_id=user_id, account_id=acc.id,
            period_from=date(2026, 7, 1), period_to=date(2026, 7, 31),
        )
        db.add(run)
        db.flush()

        for age, side, amount, flagged in ages_and_sides:
            db.add(ReconciliationItem(
                id=uuid.uuid4(), run_id=run.id, user_id=user_id, side=side,
                brs_category="outstanding_cheque", amount_paise=amount,
                direction="subtract", age_days=age, exception_flag=flagged,
            ))
        db.commit()
        return run.id
    finally:
        db.close()


def test_items_land_in_the_right_bands(client, auth_headers):
    uid = _user_id(client, auth_headers)
    _build_run(uid, [
        (5,   "bank", 100_00, False),
        (30,  "bank", 200_00, False),     # boundary: still 0-30
        (31,  "book", 300_00, False),     # boundary: first of 31-60
        (95,  "bank", 400_00, True),
        (400, "book", 500_00, True),
    ])

    data = client.get("/analytics/unreconciled-ageing", headers=auth_headers).json()
    by_label = {b["label"]: b for b in data["buckets"]}

    assert by_label["0-30 days"]["total_count"] == 2
    assert by_label["31-60 days"]["total_count"] == 1
    assert by_label["61-90 days"]["total_count"] == 0
    assert by_label["90+ days"]["total_count"] == 2      # 95 and 400 both tail in

    assert data["total_count"] == 5
    assert data["total_value"] == pytest.approx(1500.0)
    assert data["oldest_days"] == 400
    assert data["as_of"] == "2026-07-31"


def test_bank_and_book_are_reported_separately(client, auth_headers):
    uid = _user_id(client, auth_headers)
    _build_run(uid, [(5, "bank", 100_00, False), (5, "book", 250_00, False)])

    band = {b["label"]: b for b in client.get(
        "/analytics/unreconciled-ageing", headers=auth_headers).json()["buckets"]}["0-30 days"]

    # Netting these would hide which side of the bridge the work is on.
    assert band["bank_count"] == 1 and band["bank_value"] == pytest.approx(100.0)
    assert band["book_count"] == 1 and band["book_value"] == pytest.approx(250.0)
    assert band["total_value"] == pytest.approx(350.0)


def test_opposing_signs_do_not_cancel_a_bucket_to_empty(client, auth_headers):
    """A bridge item's sign is add-or-subtract, not outstanding-or-not."""
    uid = _user_id(client, auth_headers)
    _build_run(uid, [(5, "bank", 100_00, False), (5, "bank", -100_00, False)])

    band = {b["label"]: b for b in client.get(
        "/analytics/unreconciled-ageing", headers=auth_headers).json()["buckets"]}["0-30 days"]
    assert band["total_count"] == 2
    assert band["total_value"] == pytest.approx(200.0)


def test_exception_flags_are_counted(client, auth_headers):
    uid = _user_id(client, auth_headers)
    _build_run(uid, [(95, "bank", 100_00, True), (95, "bank", 100_00, False)])

    data = client.get("/analytics/unreconciled-ageing", headers=auth_headers).json()
    assert data["exceptions"] == 1
    assert {b["label"]: b["exceptions"] for b in data["buckets"]}["90+ days"] == 1


def test_a_user_with_no_reconciliation_gets_empty_bands_not_an_error(client, auth_headers):
    res = client.get("/analytics/unreconciled-ageing", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["total_count"] == 0
    assert data["accounts_covered"] == 0
    assert data["as_of"] is None
    # The bands are still present so the panel renders a schedule, not a blank.
    assert [b["label"] for b in data["buckets"]] == [
        "0-30 days", "31-60 days", "61-90 days", "90+ days"]


def test_another_users_bridge_is_not_visible(client, auth_headers):
    other = f"other_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": other, "password": "Password123!"})
    other_tok = client.post("/auth/login", json={"email": other, "password": "Password123!"}).json()["access_token"]
    other_id = client.get("/auth/me", headers={"Authorization": f"Bearer {other_tok}"}).json()["id"]

    _build_run(other_id, [(5, "bank", 999_00, False)])

    data = client.get("/analytics/unreconciled-ageing", headers=auth_headers).json()
    assert data["total_count"] == 0
