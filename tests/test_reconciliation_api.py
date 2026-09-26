import pytest
import uuid
from datetime import date
from fastapi.testclient import TestClient
from app.models.reconciliation import (
    ReconciliationRun, ReconciliationMatch, ReconciliationItem, MatchStatusEnum, BRSSideEnum, DirectionEnum
)
from app.models.account import Account
from app.database.session import get_db


def get_auth_headers(client: TestClient, email: str, password: str = "Secret123!"):
    # Register user
    reg_res = client.post("/auth/register", json={
        "email": email,
        "password": password,
        "full_name": email.split("@")[0].title(),
        "entity_type": "BUSINESS"
    })
    # Login
    res = client.post("/auth/login", json={"email": email, "password": password})
    token = res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Ensure user has a bank account registered via API
    client.post("/v1/bank-master/accounts", headers=headers, json={
        "bank_code": "HDFC",
        "account_number": "1234567890",
        "account_type": "CURRENT"
    })


    return headers



def create_test_account(db, user_id):
    account = Account(
        id=uuid.uuid4(),
        user_id=user_id,
        bank_code="HDFC",
        account_number_masked="****8888",
        account_type="CURRENT"
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def test_1_create_reconciliation_run(client: TestClient):
    headers = get_auth_headers(client, "recon_user1@example.com")
    res = client.post("/v1/reconciliation/runs", headers=headers, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    assert res.status_code == 200
    data = res.json()
    assert "run_id" in data
    assert "verdict" in data


def test_2_get_reconciliation_runs(client: TestClient):
    headers = get_auth_headers(client, "recon_user2@example.com")
    # Create run
    run_res = client.post("/v1/reconciliation/runs", headers=headers, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    assert run_res.status_code == 200

    # List runs
    res = client.get("/v1/reconciliation/runs", headers=headers)
    assert res.status_code == 200
    runs = res.json()
    assert len(runs) >= 1
    assert "id" in runs[0]
    assert "book_closing_paise" in runs[0]
    assert "bank_closing_paise" in runs[0]


def test_3_user_isolation_runs_list(client: TestClient):
    headers_a = get_auth_headers(client, "recon_userA@example.com")
    headers_b = get_auth_headers(client, "recon_userB@example.com")

    # Create run for User A
    run_res = client.post("/v1/reconciliation/runs", headers=headers_a, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    run_id_a = run_res.json()["run_id"]

    # User B lists runs — should not see User A's run
    res_b = client.get("/v1/reconciliation/runs", headers=headers_b)
    assert res_b.status_code == 200
    b_run_ids = [r["id"] for r in res_b.json()]
    assert run_id_a not in b_run_ids


def test_4_get_reconciliation_run_detail(client: TestClient):
    headers = get_auth_headers(client, "recon_user4@example.com")
    run_res = client.post("/v1/reconciliation/runs", headers=headers, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    run_id = run_res.json()["run_id"]

    res = client.get(f"/v1/reconciliation/runs/{run_id}", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == run_id
    assert "items" in data
    assert "verdict" in data


def test_5_user_cannot_access_other_user_run(client: TestClient):
    headers_a = get_auth_headers(client, "recon_user5_a@example.com")
    headers_b = get_auth_headers(client, "recon_user5_b@example.com")

    run_res = client.post("/v1/reconciliation/runs", headers=headers_a, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    run_id_a = run_res.json()["run_id"]

    # User B attempts to access User A's run
    res_b = client.get(f"/v1/reconciliation/runs/{run_id_a}", headers=headers_b)
    assert res_b.status_code == 404
    assert res_b.json()["detail"] == "Reconciliation run not found"


def test_6_get_run_matches_and_ownership(client: TestClient):
    headers_a = get_auth_headers(client, "recon_user6_a@example.com")
    headers_b = get_auth_headers(client, "recon_user6_b@example.com")

    run_res = client.post("/v1/reconciliation/runs", headers=headers_a, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    run_id_a = run_res.json()["run_id"]

    res_a = client.get(f"/v1/reconciliation/runs/{run_id_a}/matches", headers=headers_a)
    assert res_a.status_code == 200

    # User B trying to fetch matches of User A's run
    res_b = client.get(f"/v1/reconciliation/runs/{run_id_a}/matches", headers=headers_b)
    assert res_b.status_code == 404


def test_7_status_filtering_on_matches(client: TestClient):
    headers = get_auth_headers(client, "recon_user7@example.com")
    run_res = client.post("/v1/reconciliation/runs", headers=headers, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    run_id = run_res.json()["run_id"]

    res = client.get(f"/v1/reconciliation/runs/{run_id}/matches?status=pending_review", headers=headers)
    assert res.status_code == 200
    matches = res.json()
    for m in matches:
        assert m["status"] == "pending_review"


def test_8_confirm_match(client: TestClient):
    headers = get_auth_headers(client, "recon_user8@example.com")
    run_res = client.post("/v1/reconciliation/runs", headers=headers, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    run_id = run_res.json()["run_id"]

    # Inject a pending review match directly for test verification
    from app.database.session import get_db
    db = next(client.app.dependency_overrides[get_db]())
    user_res = client.get("/auth/me", headers=headers).json()
    user_id = uuid.UUID(user_res["id"])

    match = ReconciliationMatch(
        run_id=uuid.UUID(run_id),
        user_id=user_id,
        tier="tier_1",
        confidence=0.8,
        status=MatchStatusEnum.PENDING_REVIEW.value,
        reason="Test candidate match"
    )
    db.add(match)
    db.commit()
    match_id = match.id

    # Confirm match via API
    res = client.post(f"/v1/reconciliation/matches/{match_id}/confirm", headers=headers)
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # Verify state in DB
    db = next(client.app.dependency_overrides[get_db]())
    updated = db.query(ReconciliationMatch).get(match_id)
    assert updated.status == MatchStatusEnum.CONFIRMED.value


def test_9_reject_match(client: TestClient):
    headers = get_auth_headers(client, "recon_user9@example.com")
    run_res = client.post("/v1/reconciliation/runs", headers=headers, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    run_id = run_res.json()["run_id"]

    db = next(client.app.dependency_overrides[get_db]())
    user_res = client.get("/auth/me", headers=headers).json()
    user_id = uuid.UUID(user_res["id"])

    match = ReconciliationMatch(
        run_id=uuid.UUID(run_id),
        user_id=user_id,
        tier="tier_1",
        confidence=0.8,
        status=MatchStatusEnum.PENDING_REVIEW.value,
        reason="Test candidate match"
    )
    db.add(match)
    db.commit()
    match_id = match.id

    # Reject match via API
    res = client.post(f"/v1/reconciliation/matches/{match_id}/reject", headers=headers)
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # Verify DB state
    db = next(client.app.dependency_overrides[get_db]())
    updated = db.query(ReconciliationMatch).get(match_id)
    assert updated.status == MatchStatusEnum.REJECTED.value


def test_10_user_cannot_confirm_other_user_match(client: TestClient):
    headers_a = get_auth_headers(client, "recon_user10_a@example.com")
    headers_b = get_auth_headers(client, "recon_user10_b@example.com")

    run_res = client.post("/v1/reconciliation/runs", headers=headers_a, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    run_id_a = run_res.json()["run_id"]

    db = next(client.app.dependency_overrides[get_db]())
    user_a_id = uuid.UUID(client.get("/auth/me", headers=headers_a).json()["id"])
    match = ReconciliationMatch(
        run_id=uuid.UUID(run_id_a),
        user_id=user_a_id,
        tier="tier_1",
        confidence=0.8,
        status=MatchStatusEnum.PENDING_REVIEW.value,
        reason="User A Match"
    )
    db.add(match)
    db.commit()
    match_id = match.id

    # User B attempts to confirm User A's match
    res_b = client.post(f"/v1/reconciliation/matches/{match_id}/confirm", headers=headers_b)
    assert res_b.status_code == 404
    assert res_b.json()["detail"] == "Match not found"


def test_11_classify_item(client: TestClient):
    headers = get_auth_headers(client, "recon_user11@example.com")
    run_res = client.post("/v1/reconciliation/runs", headers=headers, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    run_id = run_res.json()["run_id"]

    db = next(client.app.dependency_overrides[get_db]())
    user_id = uuid.UUID(client.get("/auth/me", headers=headers).json()["id"])
    item = ReconciliationItem(
        run_id=uuid.UUID(run_id),
        user_id=user_id,
        side=BRSSideEnum.BANK.value,
        brs_category="unexplained_bank_charge",
        amount_paise=5000,
        direction=DirectionEnum.SUBTRACT.value,
        age_days=5
    )
    db.add(item)
    db.commit()
    item_id = item.id

    res = client.post(f"/v1/reconciliation/items/{item_id}/classify", headers=headers, json={
        "brs_category": "bank_charge"
    })
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    db = next(client.app.dependency_overrides[get_db]())
    updated = db.query(ReconciliationItem).get(item_id)
    assert updated.brs_category == "bank_charge"
    assert updated.overridden_by_user == True


def test_12_review_queue_end_to_end_flow(client: TestClient):
    headers = get_auth_headers(client, "recon_user12@example.com")
    run_res = client.post("/v1/reconciliation/runs", headers=headers, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    run_id = run_res.json()["run_id"]

    db = next(client.app.dependency_overrides[get_db]())
    user_id = uuid.UUID(client.get("/auth/me", headers=headers).json()["id"])
    match = ReconciliationMatch(
        run_id=uuid.UUID(run_id),
        user_id=user_id,
        tier="tier_1",
        confidence=0.8,
        status=MatchStatusEnum.PENDING_REVIEW.value,
        reason="Amount mismatch on reference"
    )
    db.add(match)
    db.commit()
    match_id = match.id


    # 1. Fetch runs
    runs_res = client.get("/v1/reconciliation/runs", headers=headers)
    assert runs_res.status_code == 200
    runs = runs_res.json()
    assert len(runs) >= 1
    latest_run_id = runs[0]["id"]

    # 2. Fetch matches for latest run with status=pending_review
    matches_res = client.get(f"/v1/reconciliation/runs/{latest_run_id}/matches?status=pending_review", headers=headers)
    assert matches_res.status_code == 200
    pending_matches = matches_res.json()
    assert len(pending_matches) >= 1
    assert pending_matches[0]["match_id"] == str(match_id)

    # 3. Confirm match
    confirm_res = client.post(f"/v1/reconciliation/matches/{match_id}/confirm", headers=headers)
    assert confirm_res.status_code == 200

    # 4. Fetch pending matches again -> should be empty now
    matches_res_2 = client.get(f"/v1/reconciliation/runs/{latest_run_id}/matches?status=pending_review", headers=headers)
    assert matches_res_2.status_code == 200
    assert len(matches_res_2.json()) == 0


def test_13_existing_run_replacement_behavior_doc(client: TestClient):
    """Document Phase 10 immutable versioning behavior: rerunning reconciliation creates version 2 superseding version 1."""
    headers = get_auth_headers(client, "recon_user13@example.com")
    run_res_1 = client.post("/v1/reconciliation/runs", headers=headers, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    run_id_1 = run_res_1.json()["run_id"]

    # Rerun for exact same period
    run_res_2 = client.post("/v1/reconciliation/runs", headers=headers, json={
        "period_from": "2026-01-01",
        "period_to": "2026-01-31",
        "force": True
    })
    run_id_2 = run_res_2.json()["run_id"]

    assert run_id_1 != run_id_2

    # Verify run_1 is preserved (immutable historical version 1)
    res_1 = client.get(f"/v1/reconciliation/runs/{run_id_1}", headers=headers)
    assert res_1.status_code == 200

    # Verify run_2 exists (version 2)
    res_2 = client.get(f"/v1/reconciliation/runs/{run_id_2}", headers=headers)
    assert res_2.status_code == 200


def test_14_confirm_books_import_requires_account_id(client: TestClient):
    """Verify that POST /v1/reconciliation/imports/confirm rejects requests without account_id with HTTP 400."""
    headers = get_auth_headers(client, "recon_user14@example.com")
    
    # 1. Missing account_id -> HTTP 400
    res_bad = client.post("/v1/reconciliation/imports/confirm", headers=headers, json={
        "column_mapping": {"Date": "entry_date", "Amount": "money_out"},
        "date_format": "%Y-%m-%d",
        "rows": [{"Date": "2026-01-15", "Amount": "100.00"}]
    })
    assert res_bad.status_code == 400
    assert "account_id is required" in res_bad.json()["detail"]

    # 2. Valid account_id -> HTTP 200
    acc_res = client.get("/v1/bank-master/accounts", headers=headers)
    acc_id = acc_res.json()[0]["id"]

    res_good = client.post("/v1/reconciliation/imports/confirm", headers=headers, json={
        "account_id": acc_id,
        "column_mapping": {"Date": "entry_date", "Amount": "money_out"},
        "date_format": "%Y-%m-%d",
        "rows": [{"Date": "2026-01-15", "Amount": "100.00"}]
    })
    assert res_good.status_code == 200
    assert "batch_id" in res_good.json()
