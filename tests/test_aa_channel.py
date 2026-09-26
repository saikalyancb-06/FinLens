import uuid
import pytest
from fastapi.testclient import TestClient
from app.database.session import Base, engine, get_db
from app.models.user import User
from app.utils.security import hash_password
from main import app

@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield

def get_auth_token(client: TestClient) -> str:
    # Register/login test user via API
    email = f"aa_tester_{uuid.uuid4().hex[:6]}@example.com"
    password = "Password123!"
    
    reg_res = client.post("/auth/register", json={"email": email, "password": password})
    assert reg_res.status_code in (200, 201), reg_res.text

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 200, login_res.text
    return login_res.json()["access_token"]


def test_setu_aa_complete_pipeline():
    client = TestClient(app)
    token = get_auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Initiate Setu AA Consent Request
    initiate_resp = client.post(
        "/aa/consent/initiate",
        headers=headers,
        json={
            "date_from": "2026-05-01",
            "date_to": "2026-08-01",
            "purpose_code": "101",
            "fi_type": "DEPOSIT"
        }
    )
    assert initiate_resp.status_code == 200, initiate_resp.text
    init_data = initiate_resp.json()
    assert "consent_handle" in init_data
    assert "approval_url" in init_data
    handle = init_data["consent_handle"]
    assert "SETU-CS-" in handle or "CS-" in handle

    # 2. Check Consent Status
    status_resp = client.get(f"/aa/consent/status/{handle}", headers=headers)
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] in ("PENDING", "ACTIVE")

    # 3. Trigger FI Data Fetch, Decryption, Rule+ML Categorization & DB Persistence
    fetch_resp = client.post(
        "/aa/fetch",
        headers=headers,
        json={"consent_handle": handle}
    )
    assert fetch_resp.status_code == 200, fetch_resp.text
    fetch_data = fetch_resp.json()
    assert fetch_data["status"] == "COMPLETED"
    assert fetch_data["fetched_count"] > 0
    assert len(fetch_data["records"]) > 0

    first_record = fetch_data["records"][0]
    assert first_record["source"] == "account_aggregator"
    assert "category" in first_record
    assert first_record["category"] != ""

    # 4. Verify Transactions land in main GET /transactions endpoint
    txns_resp = client.get("/transactions", headers=headers)
    assert txns_resp.status_code == 200
    all_txns = txns_resp.json()

    aa_txns = [t for t in all_txns if t.get("file_id") is None and "Account Aggregator" in (t.get("original_raw_text") or "")]
    assert len(aa_txns) > 0, "Account Aggregator transactions should appear in GET /transactions"
