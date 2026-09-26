import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.email.utils import (
    encrypt_token,
    decrypt_token,
    compute_file_sha256,
    detect_bank_from_email,
    is_statement_email
)
from app.email.attachment_handler import attachment_handler
from app.email.models import ConnectedAccount, EmailAttachment, ImportHistory
from tests.conftest import TestingSessionLocal, Base, test_engine

def test_email_utils_encryption_and_hashing():
    original_token = "demo_refresh_token_xyz_12345"
    encrypted = encrypt_token(original_token)
    assert encrypted != original_token
    decrypted = decrypt_token(encrypted)
    assert decrypted == original_token

    content = b"Sample Bank Statement Content 2026"
    file_hash = compute_file_sha256(content)
    assert len(file_hash) == 64
    assert file_hash == compute_file_sha256(content)

def test_email_utils_bank_and_statement_detection():
    # Bank detection
    assert detect_bank_from_email("e-statement@sbi.co.in", "Monthly Statement") == "SBI"
    assert detect_bank_from_email("alerts@hdfcbank.net", "HDFC Bank Statement") == "HDFC Bank"
    assert detect_bank_from_email("estatement@icicibank.com", "ICICI Bank Statement") == "ICICI Bank"
    assert detect_bank_from_email("info@unknownsender.com", "Generic Notice") == "Bank Statement"

    # Statement subject matching
    assert is_statement_email("e-statement@sbi.co.in", "Monthly Account Statement") is True
    assert is_statement_email("alerts@hdfcbank.net", "E-Statement Jul 2026") is True

def test_attachment_extension_filtering():
    assert attachment_handler.is_valid_attachment_extension("statement.pdf") is True
    assert attachment_handler.is_valid_attachment_extension("data.csv") is True
    assert attachment_handler.is_valid_attachment_extension("report.xlsx") is True
    assert attachment_handler.is_valid_attachment_extension("sheet.xls") is True
    
    assert attachment_handler.is_valid_attachment_extension("image.png") is False
    assert attachment_handler.is_valid_attachment_extension("archive.zip") is False
    assert attachment_handler.is_valid_attachment_extension("executable.exe") is False

@pytest.fixture
def auth_headers(client):
    Base.metadata.create_all(bind=test_engine)
    email = f"email_tester_{uuid.uuid4().hex[:6]}@example.com"
    password = "Password123!"
    reg_res = client.post("/auth/register", json={"email": email, "password": password})
    user_id_str = reg_res.json()["id"]

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    token = login_res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, user_id_str

def test_email_pickup_complete_api_flow(client, auth_headers):
    headers, user_id = auth_headers

    # 1. GET /email/status (Initially Not Connected)
    res_status = client.get("/email/status", headers=headers)
    assert res_status.status_code == 200
    assert res_status.json()["connected"] is False

    # 2. POST /email/connect (Get Google OAuth authorization URL)
    res_conn = client.post("/email/connect", headers=headers)
    assert res_conn.status_code == 200
    data_conn = res_conn.json()
    assert "authorization_url" in data_conn
    assert "gmail.readonly" in data_conn["scopes"][0]

    # 3. GET /email/oauth/callback (Exchange OAuth code for tokens)
    import urllib.parse
    state_param = urllib.parse.parse_qs(urllib.parse.urlparse(data_conn["authorization_url"]).query)["state"][0]
    res_cb = client.get(f"/email/oauth/callback?code=demo_code_test_123&state={state_param}", headers=headers)
    assert res_cb.status_code == 200
    assert res_cb.json()["status"] == "success"
    assert "email_address" in res_cb.json()

    # Verify status is now Connected
    res_status2 = client.get("/email/status", headers=headers)
    assert res_status2.status_code == 200
    assert res_status2.json()["connected"] is True

    # Register Bank Master account
    acc_res = client.post(
        "/v1/bank-master/accounts",
        headers=headers,
        json={
            "bank_code": "HDFC",
            "account_number": "50200012341534",
            "account_type": "CURRENT",
            "currency": "INR"
        }
    )
    assert acc_res.status_code == 201
    acc_id = acc_res.json()["id"]

    # 4. POST /email/scan (Scan mailbox for bank statements)
    res_scan = client.post("/email/scan", headers=headers)
    assert res_scan.status_code == 200
    data_scan = res_scan.json()
    assert data_scan["status"] == "success"
    assert data_scan["total_found"] >= 1
    assert "statements" in data_scan

    # 5. GET /email/statements (List detected attachments)
    res_list = client.get("/email/statements", headers=headers)
    assert res_list.status_code == 200
    statements = res_list.json()
    assert len(statements) >= 1

    stmt_item = next((s for s in statements if s.get("attachment_type") != "ALERT"), statements[0])

    # 6. POST /email/import/{id} (Import single statement into parsing pipeline with target account)
    res_imp = client.post(
        f"/email/import/{stmt_item['id']}",
        headers=headers,
        json={"account_id": acc_id}
    )
    assert res_imp.status_code == 200
    data_imp = res_imp.json()
    assert data_imp["status"] == "success"
    assert "file_id" in data_imp

    # 7. POST /email/import-all (Import remaining pending statements)
    res_imp_all = client.post("/email/import-all", headers=headers)
    assert res_imp_all.status_code == 200
    assert res_imp_all.json()["status"] in ["success", "info"]

    # 8. DELETE /email/disconnect (Disconnect email account)
    res_disc = client.delete("/email/disconnect", headers=headers)
    assert res_disc.status_code == 200
    assert res_disc.json()["status"] == "success"

    # Verify status returns disconnected
    res_status3 = client.get("/email/status", headers=headers)
    assert res_status3.status_code == 200
    assert res_status3.json()["connected"] is False


def test_classifier_strictness_eatsure_and_bookmyshow(tmp_path):
    # 1. EatSure invoice content test
    eatsure_file = tmp_path / "EatSure.pdf"
    eatsure_file.write_text("EatSure Order CRN-221321644 Invoice Tax Invoice GSTIN: 27AAAAA0000A1Z5 Total: 564 Date: 2026-08-11 Food Items Delivery")
    is_stmt, classif, reason, acc = attachment_handler.inspect_attachment_content(str(eatsure_file), "EatSure.pdf")
    assert is_stmt is False
    assert classif == "NOT_BANK_STATEMENT"

    # 2. BookMyShow GST ticket content test
    bms_file = tmp_path / "WRH6M3Q_GST_Invoice.pdf"
    bms_file.write_text("BookMyShow Cinema Ticket Tax Invoice Booking Ref WRH6M3Q Seat A12 Convenience Fee Total Amount: 450 GSTIN 27AABCB1234F1ZB Date: 2026-08-11")
    is_stmt_bms, classif_bms, reason_bms, acc_bms = attachment_handler.inspect_attachment_content(str(bms_file), "WRH6M3Q_GST_Invoice.pdf")
    assert is_stmt_bms is False
    assert classif_bms == "NOT_BANK_STATEMENT"

    # 3. Genuine Bank Statement content test
    stmt_file = tmp_path / "HDFC_Statement_Jul2026.csv"
    stmt_file.write_text("Date,Particulars,Debit,Credit,Balance\n01/07/2026,SALARY CREDIT,0.00,50000.00,150000.00\n02/07/2026,ATM WITHDRAWAL,2000.00,0.00,148000.00\n")
    is_stmt_real, classif_real, reason_real, acc_real = attachment_handler.inspect_attachment_content(str(stmt_file), "HDFC_Statement_Jul2026.csv")
    assert is_stmt_real is True
    assert classif_real == "BANK_STATEMENT_CONFIRMED"


def test_import_requires_target_account(client, auth_headers):
    headers, user_id = auth_headers
    
    # First connect email OAuth
    res_conn = client.post("/email/connect", headers=headers)
    import urllib.parse
    state_param = urllib.parse.parse_qs(urllib.parse.urlparse(res_conn.json()["authorization_url"]).query)["state"][0]
    client.get(f"/email/oauth/callback?code=demo_code_test&state={state_param}", headers=headers)
    
    # Scan inbox
    client.post("/email/scan", headers=headers)
    
    res_stmts = client.get("/email/statements", headers=headers)
    statements = res_stmts.json()
    stmt_item = next((s for s in statements if s.get("attachment_type") != "ALERT"), statements[0])
    
    # Attempt import without passing account_id payload -> MUST return 400 Bad Request
    res = client.post(f"/email/import/{stmt_item['id']}", headers=headers)
    assert res.status_code == 400
    assert "account selection is required" in res.json()["detail"].lower()

