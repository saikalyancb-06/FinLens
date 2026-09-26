import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from main import app
from app.database.session import engine, Base, get_db
from app.email.models import ImportHistory, EmailAttachment
from app.models.account import Account
from app.models.user import User
from tests.conftest import test_engine

client = TestClient(app)


def register_and_login_user(client: TestClient, prefix: str):
    Base.metadata.create_all(bind=test_engine)
    email = f"{prefix}_{uuid.uuid4().hex[:6]}@example.com"
    password = "StrongPassword123!"
    reg_res = client.post("/auth/register", json={"email": email, "password": password})
    user_id_str = reg_res.json()["id"]

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, token, user_id_str


def test_import_history_schema_has_account_id():
    """Verify that import_history table in active SQLite DB actually contains account_id column."""
    inspector = inspect(engine)
    columns = [c["name"] for c in inspector.get_columns("import_history")]
    assert "account_id" in columns, "account_id column missing from import_history table!"


def test_import_history_record_saves_account_id(client):
    """Verify that importing an email statement creates an ImportHistory record with account_id populated."""
    headers, token, user_id_str = register_and_login_user(client, "imp_hist")

    # 1. Create a bank account
    acc_res = client.post(
        "/v1/bank-master/accounts",
        headers=headers,
        json={
            "bank_code": "AXIS",
            "account_number": "918020003654",
            "account_type": "CURRENT",
            "currency": "INR"
        }
    )
    assert acc_res.status_code == 201
    account_id = acc_res.json()["id"]

    # 2. Connect email & complete OAuth callback
    conn_res = client.post("/email/connect", headers=headers)
    data_conn = conn_res.json()
    import urllib.parse
    state_param = urllib.parse.parse_qs(urllib.parse.urlparse(data_conn["authorization_url"]).query)["state"][0]
    client.get(f"/email/oauth/callback?code=demo_code_test&state={state_param}", headers=headers)

    client.post("/email/scan", headers=headers)

    # 3. Get statements
    stmts_res = client.get("/email/statements", headers=headers)
    assert stmts_res.status_code == 200
    stmts = stmts_res.json()
    assert len(stmts) > 0
    target_att_id = stmts[0]["id"]

    # 4. Import statement with target account
    imp_res = client.post(
        f"/email/import/{target_att_id}",
        headers=headers,
        json={"account_id": account_id}
    )
    assert imp_res.status_code == 200

    # 5. Inspect ImportHistory record directly in database
    db = next(get_db())
    history_rec = db.query(ImportHistory).filter(ImportHistory.user_id == uuid.UUID(user_id_str)).first()
    assert history_rec is not None
    assert str(history_rec.account_id) == str(account_id)
    assert history_rec.status == "SUCCESS"
