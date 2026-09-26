import uuid
import pytest
from fastapi.testclient import TestClient
from main import app
from app.database.session import Base, get_db
from app.email.models import EmailAttachment, ImportHistory
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.models.uploaded_file import UploadedFile
from tests.conftest import test_engine

client = TestClient(app)


def register_and_login_user(client: TestClient, prefix: str):
    Base.metadata.create_all(bind=test_engine)
    email = f"{prefix}_{uuid.uuid4().hex[:6]}@example.com"
    pass_str = "StrongPassword123!"
    reg_res = client.post("/auth/register", json={"email": email, "password": pass_str})
    user_id_str = reg_res.json()["id"]

    login_res = client.post("/auth/login", json={"email": email, "password": pass_str})
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, token, user_id_str


def test_clear_all_transactions_deletes_records_without_fk_error():
    """Verify that DELETE /transactions/clear clears transactions and associated records without Foreign Key errors."""
    headers, token, user_id_str = register_and_login_user(client, "clear_tx")

    # 1. Create a bank account
    acc_res = client.post(
        "/v1/bank-master/accounts",
        headers=headers,
        json={
            "bank_code": "HDFC",
            "account_number": "50200088881234",
            "account_type": "CURRENT",
            "currency": "INR"
        }
    )
    assert acc_res.status_code == 201
    account_id = acc_res.json()["id"]

    # 2. Connect email & scan inbox
    conn_res = client.post("/email/connect", headers=headers)
    data_conn = conn_res.json()
    import urllib.parse
    state_param = urllib.parse.parse_qs(urllib.parse.urlparse(data_conn["authorization_url"]).query)["state"][0]
    client.get(f"/email/oauth/callback?code=demo_code_test&state={state_param}", headers=headers)
    client.post("/email/scan", headers=headers)

    # 3. Import CSV statement to generate UploadedFile, Statement, Transaction, ImportHistory, EmailAttachment
    stmts = client.get("/email/statements", headers=headers).json()
    csv_stmt = next((s for s in stmts if s["filename"].endswith(".csv")), None)
    assert csv_stmt is not None

    imp_res = client.post(
        f"/email/import/{csv_stmt['id']}",
        headers=headers,
        json={"bank_account_id": account_id}
    )
    assert imp_res.status_code == 200

    # Verify records exist before clear
    db = next(get_db())
    tx_count_before = db.query(Transaction).filter(Transaction.user_id == uuid.UUID(user_id_str)).count()
    assert tx_count_before > 0

    # 4. Perform Clear All
    clear_res = client.delete("/transactions/clear", headers=headers)
    assert clear_res.status_code == 200
    assert clear_res.json()["status"] == "success"

    # 5. Verify records cleared
    db = next(get_db())
    assert db.query(Transaction).filter(Transaction.user_id == uuid.UUID(user_id_str)).count() == 0
    assert db.query(Statement).filter(Statement.user_id == uuid.UUID(user_id_str)).count() == 0
    assert db.query(UploadedFile).filter(UploadedFile.user_id == uuid.UUID(user_id_str)).count() == 0
    assert db.query(ImportHistory).filter(ImportHistory.user_id == uuid.UUID(user_id_str)).count() == 0
    assert db.query(EmailAttachment).filter(EmailAttachment.user_id == uuid.UUID(user_id_str)).count() == 0
