import os
import uuid
import pytest
import fitz
from fastapi.testclient import TestClient
from main import app
from app.database.session import Base, get_db
from app.email.models import EmailAttachment, ImportHistory
from app.models.statement import Statement
from app.models.transaction import Transaction
from tests.conftest import test_engine

client = TestClient(app)


def create_encrypted_pdf_on_disk(user_id_str: str, password: str = "BankTest@2026") -> str:
    """Creates a valid, password-encrypted bank statement PDF file on disk."""
    upload_dir = os.path.join("uploads", "email")
    os.makedirs(upload_dir, exist_ok=True)
    filename = f"{user_id_str}_enc_{uuid.uuid4().hex[:8]}_HDFC_Bank_statement.pdf"
    file_path = os.path.join(upload_dir, filename)

    doc = fitz.open()
    page = doc.new_page()
    content = (
        "HDFC BANK STATEMENT OF ACCOUNT\n"
        "Account Number: 50200088884589\n"
        "Date | Description | Debit | Credit | Balance\n"
        "01/07/2026 | Opening Balance | 0.00 | 0.00 | 50000.00\n"
        "05/07/2026 | Salary Credit | 0.00 | 75000.00 | 125000.00\n"
        "10/07/2026 | Office Rent Paid | 25000.00 | 0.00 | 100000.00\n"
    )
    page.insert_text((50, 50), content)

    encrypt_method = fitz.PDF_ENCRYPT_AES_256 if hasattr(fitz, "PDF_ENCRYPT_AES_256") else fitz.PDF_ENCRYPT_STANDARD
    pdf_bytes = doc.tobytes(encryption=encrypt_method, owner_pw="ownerpass", user_pw=password)
    doc.close()

    with open(file_path, "wb") as f:
        f.write(pdf_bytes)

    return file_path, filename


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


def setup_user_account_and_encrypted_attachment(client, headers, user_id_str, password: str = "BankTest@2026"):
    # 1. Create a bank account
    acc_res = client.post(
        "/v1/bank-master/accounts",
        headers=headers,
        json={
            "bank_code": "HDFC",
            "account_number": "50200088884589",
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

    # 3. Create encrypted PDF on disk & insert EmailAttachment in DB
    file_path, filename = create_encrypted_pdf_on_disk(user_id_str, password=password)

    db = next(get_db())
    conn_acc = db.query(EmailAttachment).first()

    att = EmailAttachment(
        user_id=uuid.UUID(user_id_str),
        connected_account_id=conn_acc.connected_account_id if conn_acc else None,
        email_message_id=f"msg_{uuid.uuid4().hex[:8]}",
        bank_name="HDFC Bank",
        subject="HDFC Bank E-Statement for Account ending 4589",
        sender="statements@hdfcbank.net",
        filename=filename,
        file_size_bytes=os.path.getsize(file_path),
        mime_type="application/pdf",
        file_hash=uuid.uuid4().hex,
        local_path=file_path,
        is_duplicate=False,
        import_status="PENDING",
        classification="BANK_STATEMENT_CONFIRMED",
        classification_reason="Attachment verified as bank statement.",
        account_id=uuid.UUID(account_id)
    )
    db.add(att)
    db.commit()
    db.refresh(att)

    return account_id, str(att.id)


def test_case_1_encrypted_pdf_no_password_returns_password_required():
    """Case 1: Encrypted PDF imported without password must return HTTP 400 PDF_PASSWORD_REQUIRED state."""
    headers, token, user_id_str = register_and_login_user(client, "pass_c1")
    account_id, att_id = setup_user_account_and_encrypted_attachment(client, headers, user_id_str, password="BankTest@2026")

    # Call import endpoint WITHOUT pdf_password
    imp_res = client.post(
        f"/email/import/{att_id}",
        headers=headers,
        json={"bank_account_id": account_id}
    )
    assert imp_res.status_code == 400
    assert "password" in imp_res.json()["detail"].lower()

    # Verify classification in database was updated to PDF_PASSWORD_REQUIRED
    db = next(get_db())
    att = db.query(EmailAttachment).filter(EmailAttachment.id == uuid.UUID(att_id)).first()
    assert att is not None
    assert att.classification == "PDF_PASSWORD_REQUIRED"


def test_case_2_encrypted_pdf_with_password_succeeds_and_associates_account():
    """Case 2: Encrypted PDF imported with correct password passes password to parser, succeeds, and links account."""
    headers, token, user_id_str = register_and_login_user(client, "pass_c2")
    account_id, att_id = setup_user_account_and_encrypted_attachment(client, headers, user_id_str, password="BankTest@2026")

    # Call import endpoint WITH correct pdf_password
    imp_res = client.post(
        f"/email/import/{att_id}",
        headers=headers,
        json={
            "bank_account_id": account_id,
            "pdf_password": "BankTest@2026"
        }
    )
    assert imp_res.status_code == 200
    res_data = imp_res.json()
    assert res_data["status"] == "success"
    assert str(res_data["bank_account_id"]) == str(account_id)

    # Verify ImportHistory record
    db = next(get_db())
    hist = db.query(ImportHistory).filter(ImportHistory.attachment_id == uuid.UUID(att_id)).first()
    assert hist is not None
    assert str(hist.account_id) == str(account_id)
    assert hist.status == "SUCCESS"
