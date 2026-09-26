import uuid
import pytest
from fastapi.testclient import TestClient
from app.email.models import ConnectedAccount, EmailAttachment
from app.models.account import Account
from app.models.entity import Entity, Bank
from app.models.transaction import Transaction
from tests.conftest import test_engine, Base, TestingSessionLocal

@pytest.fixture
def auth_headers(client):
    Base.metadata.create_all(bind=test_engine)
    email = f"email_e2e_{uuid.uuid4().hex[:6]}@example.com"
    password = "Password123!"
    reg_res = client.post("/auth/register", json={"email": email, "password": password})
    user_id_str = reg_res.json()["id"]

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    token = login_res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, user_id_str

def test_email_pickup_end_to_end_full_flow(client, auth_headers):
    headers, user_id = auth_headers
    db = TestingSessionLocal()
    user_uuid = uuid.UUID(user_id)

    # 1. User first registers Bank Master accounts via Bank Master API
    res_acc1 = client.post(
        "/v1/bank-master/accounts",
        headers=headers,
        json={
            "bank_code": "HDFC",
            "account_number": "50200012341534",
            "account_type": "CURRENT",
            "currency": "INR"
        }
    )
    assert res_acc1.status_code == 201
    acc1_id = res_acc1.json()["id"]

    res_acc2 = client.post(
        "/v1/bank-master/accounts",
        headers=headers,
        json={
            "bank_code": "SBI",
            "account_number": "304988888888",
            "account_type": "SAVINGS",
            "currency": "INR"
        }
    )
    assert res_acc2.status_code == 201
    acc2_id = res_acc2.json()["id"]

    # 2. Check initial Email Pickup status (Not Connected)
    res_status = client.get("/email/status", headers=headers)
    assert res_status.status_code == 200
    assert res_status.json()["connected"] is False

    # 3. User connects Gmail via OAuth
    res_conn = client.post("/email/connect", headers=headers)
    assert res_conn.status_code == 200
    data_conn = res_conn.json()
    assert "authorization_url" in data_conn

    import urllib.parse
    state_param = urllib.parse.parse_qs(urllib.parse.urlparse(data_conn["authorization_url"]).query)["state"][0]
    res_cb = client.get(f"/email/oauth/callback?code=demo_code_e2e&state={state_param}", headers=headers)
    assert res_cb.status_code == 200
    assert res_cb.json()["status"] == "success"

    # Status is now Connected
    res_status2 = client.get("/email/status", headers=headers)
    assert res_status2.status_code == 200
    assert res_status2.json()["connected"] is True

    # 4. User scans inbox (Stage A, Stage B, Stage C)
    res_scan = client.post("/email/scan", headers=headers)
    assert res_scan.status_code == 200
    scan_data = res_scan.json()
    assert scan_data["status"] == "success"
    assert scan_data["total_found"] >= 1

    # 5. Retrieve statements & inspect classification & Bank Master account matching
    res_stmts = client.get("/email/statements", headers=headers)
    assert res_stmts.status_code == 200
    stmts = res_stmts.json()
    assert len(stmts) >= 1

    # Verify statement attributes
    for s in stmts:
        assert "classification" in s
        assert "classification_reason" in s

    # 6. Test manual account mapping endpoint for ambiguous statements
    real_stmts = [s for s in stmts if s.get("attachment_type") != "ALERT"]
    assert len(real_stmts) >= 1
    unmapped_stmt = next((s for s in real_stmts if s["classification"] == "NEEDS_ACCOUNT_MAPPING" or not s["account_id"]), real_stmts[0])
    
    res_map = client.post(
        f"/email/statements/{unmapped_stmt['id']}/map-account",
        headers=headers,
        json={"account_id": str(acc1_id)}
    )
    assert res_map.status_code == 200
    assert res_map.json()["status"] == "success"
    assert res_map.json()["account_id"] == str(acc1_id)

    # 7. Import statement via canonical ingestion pipeline
    res_imp = client.post(
        f"/email/import/{unmapped_stmt['id']}",
        headers=headers,
        json={"account_id": str(acc1_id)}
    )
    assert res_imp.status_code == 200
    imp_data = res_imp.json()
    assert imp_data["status"] == "success"
    assert "file_id" in imp_data

    # Verify transactions were stored with account_id via canonical transactions API
    res_txns = client.get("/transactions", headers=headers)
    assert res_txns.status_code == 200
    txns_data = res_txns.json()
    assert len(txns_data) >= 1
    assert "description" in txns_data[0]
    assert "final_category" in txns_data[0]
