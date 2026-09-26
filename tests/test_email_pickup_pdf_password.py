import os
import uuid
import pytest
import fitz
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.email.attachment_handler import attachment_handler
from app.email.models import EmailAttachment
from app.models.account import Account
from app.models.transaction import Transaction
from app.database.session import get_db
from tests.conftest import test_engine, Base


@pytest.fixture
def auth_headers(client):
    email = f"pdf_pass_user_{uuid.uuid4().hex[:6]}@example.com"
    password = "Password123!"
    reg_res = client.post("/auth/register", json={"email": email, "password": password})
    user_id_str = reg_res.json()["id"]

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    token = login_res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, user_id_str


def create_encrypted_pdf(file_path: str, content_lines: list[str], password: str):
    """Helper to create an encrypted PDF using PyMuPDF (fitz)."""
    doc = fitz.open()
    page = doc.new_page()
    text = "\n".join(content_lines)
    page.insert_text((50, 50), text)
    
    # Save with encryption
    doc.save(
        file_path,
        user_pw=password,
        owner_pw="master123",
        encryption=fitz.PDF_ENCRYPT_AES_256
    )
    doc.close()


def test_encrypted_pdf_detection_without_password(tmp_path):
    pdf_path = str(tmp_path / "Encrypted_HDFC_Statement.pdf")
    lines = [
        "HDFC Bank Account Statement",
        "Date | Particulars | Debit | Credit | Balance",
        "01/08/2026 | SALARY CREDIT | 0.00 | 75000.00 | 175000.00",
        "02/08/2026 | GROCERY | 2500.00 | 0.00 | 172500.00"
    ]
    create_encrypted_pdf(pdf_path, lines, password="SecretPassword123")

    # Inspection without password MUST return PDF_PASSWORD_REQUIRED
    is_stmt, classif, reason, acc = attachment_handler.inspect_attachment_content(pdf_path, "Encrypted_HDFC_Statement.pdf")
    assert is_stmt is False
    assert classif == "PDF_PASSWORD_REQUIRED"
    assert "password-protected" in reason.lower()


def test_encrypted_pdf_incorrect_password(tmp_path):
    pdf_path = str(tmp_path / "Encrypted_HDFC_Statement.pdf")
    lines = [
        "HDFC Bank Account Statement",
        "Date | Particulars | Debit | Credit | Balance",
        "01/08/2026 | SALARY CREDIT | 0.00 | 75000.00 | 175000.00"
    ]
    create_encrypted_pdf(pdf_path, lines, password="SecretPassword123")

    # Inspection with WRONG password MUST return PASSWORD_INVALID
    is_stmt, classif, reason, acc = attachment_handler.inspect_attachment_content(
        pdf_path, "Encrypted_HDFC_Statement.pdf", pdf_password="WrongPassword"
    )
    assert is_stmt is False
    assert classif == "PASSWORD_INVALID"
    assert "invalid" in reason.lower()


def test_encrypted_genuine_bank_statement_correct_password_flow(client, auth_headers, tmp_path):
    headers, user_id = auth_headers

    # 1. Register Bank Master account
    acc_res = client.post(
        "/v1/bank-master/accounts",
        headers=headers,
        json={
            "bank_code": "HDFC",
            "account_number": "50200099881534",
            "account_type": "CURRENT",
            "currency": "INR"
        }
    )
    assert acc_res.status_code == 201
    acc_id = acc_res.json()["id"]

    # 2. Create encrypted genuine bank statement PDF on disk
    pdf_path = str(tmp_path / "HDFC_Pass_Jul2026.pdf")
    lines = [
        "HDFC Bank Account Statement for A/C 50200099881534",
        "Date,Particulars,Debit,Credit,Balance",
        "01/07/2026,CLIENT PAYMENT,0.00,45000.00,145000.00",
        "05/07/2026,OFFICE RENT,15000.00,0.00,130000.00"
    ]
    doc_password = "MyDocPassword123"
    create_encrypted_pdf(pdf_path, lines, password=doc_password)

    # 3. Create EmailAttachment record in DB with PDF_PASSWORD_REQUIRED
    from main import app
    db = next(app.dependency_overrides[get_db]())
    att = EmailAttachment(
        user_id=uuid.UUID(user_id),
        email_message_id=f"msg_pass_{uuid.uuid4().hex[:6]}",
        bank_name="HDFC Bank",
        filename="HDFC_Pass_Jul2026.pdf",
        file_size_bytes=os.path.getsize(pdf_path),
        file_hash="99887766554433221100aabbccddeeff99887766554433221100aabbccddeeff",
        local_path=pdf_path,
        import_status="PENDING",
        classification="PDF_PASSWORD_REQUIRED",
        classification_reason="PDF is password protected",
        account_id=None
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    att_id = str(att.id)

    # 4. Attempt import with WRONG password -> MUST fail HTTP 400 Bad Request
    res_bad = client.post(
        f"/email/import/{att_id}",
        headers=headers,
        json={"account_id": acc_id, "pdf_password": "WrongPassword"}
    )
    assert res_bad.status_code == 400
    assert "invalid" in res_bad.json()["detail"].lower()

    # Verify attachment is NOT marked IMPORTED and classification is updated
    db.expire_all()
    att_check1 = db.query(EmailAttachment).filter(EmailAttachment.id == uuid.UUID(att_id)).first()
    assert att_check1.import_status == "PENDING"
    assert att_check1.classification == "PASSWORD_INVALID"

    # 5. Attempt import with CORRECT password -> MUST succeed, create canonical transactions & set IMPORTED
    res_good = client.post(
        f"/email/import/{att_id}",
        headers=headers,
        json={"account_id": acc_id, "pdf_password": doc_password}
    )
    assert res_good.status_code == 200
    data_good = res_good.json()
    assert data_good["status"] == "success"
    assert data_good["transactions_created"] >= 1

    # Verify DB state: import_status == IMPORTED, classification == BANK_STATEMENT_CONFIRMED
    db.expire_all()
    att_check2 = db.query(EmailAttachment).filter(EmailAttachment.id == uuid.UUID(att_id)).first()
    assert att_check2.import_status == "IMPORTED"
    assert att_check2.classification == "BANK_STATEMENT_CONFIRMED"
    assert att_check2.file_id is not None
    db.close()


def test_encrypted_non_bank_pdf(client, auth_headers, tmp_path):
    headers, user_id = auth_headers

    acc_res = client.post(
        "/v1/bank-master/accounts",
        headers=headers,
        json={"bank_code": "SBI", "account_number": "998877665544", "account_type": "SAVINGS", "currency": "INR"}
    )
    acc_id = acc_res.json()["id"]

    # Encrypted PDF containing an invoice (non-bank document)
    pdf_path = str(tmp_path / "Encrypted_Invoice.pdf")
    lines = [
        "EatSure Tax Invoice GSTIN: 27AAAAA0000A1Z5",
        "Order CRN-998877 Total Amount: 850.00",
        "Food Items Delivery Date: 2026-08-11"
    ]
    doc_password = "InvoicePass123"
    create_encrypted_pdf(pdf_path, lines, password=doc_password)

    from main import app
    db = next(app.dependency_overrides[get_db]())
    att = EmailAttachment(
        user_id=uuid.UUID(user_id),
        email_message_id=f"msg_inv_{uuid.uuid4().hex[:6]}",
        bank_name="Unknown",
        filename="Encrypted_Invoice.pdf",
        file_size_bytes=os.path.getsize(pdf_path),
        file_hash="1122334455667788990011223344556677889900112233445566778899001122",
        local_path=pdf_path,
        import_status="PENDING",
        classification="PDF_PASSWORD_REQUIRED",
        account_id=None
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    att_id = str(att.id)

    # Import with correct password -> MUST unlock, run Stage C inspection, identify NOT_BANK_STATEMENT & reject
    res = client.post(
        f"/email/import/{att_id}",
        headers=headers,
        json={"account_id": acc_id, "pdf_password": doc_password}
    )
    assert res.status_code == 400
    assert "not_bank_statement" in res.json()["detail"].lower() or "invoice" in res.json()["detail"].lower() or "attachment" in res.json()["detail"].lower()

    # Verify classification in DB is updated to NOT_BANK_STATEMENT
    db.expire_all()
    att_check = db.query(EmailAttachment).filter(EmailAttachment.id == uuid.UUID(att_id)).first()
    assert att_check.classification == "NOT_BANK_STATEMENT"
    assert att_check.import_status == "PENDING"
    db.close()


def test_pdf_password_never_persisted_to_database(client, auth_headers, tmp_path):
    """Verify security invariant: pdf_password is NEVER stored in database tables."""
    headers, user_id = auth_headers
    acc_res = client.post("/v1/bank-master/accounts", headers=headers, json={"bank_code": "HDFC", "account_number": "112233445566", "account_type": "CURRENT", "currency": "INR"})
    acc_id = acc_res.json()["id"]

    pdf_path = str(tmp_path / "SecTest_Statement.pdf")
    lines = ["HDFC Bank Statement", "Date,Particulars,Debit,Credit,Balance", "01/08/2026,FEE,100.00,0.00,9900.00"]
    secret_pass = "SUPER_SECRET_PASS_9999"
    create_encrypted_pdf(pdf_path, lines, password=secret_pass)

    from main import app
    db = next(app.dependency_overrides[get_db]())
    att = EmailAttachment(
        user_id=uuid.UUID(user_id),
        email_message_id=f"msg_sec_{uuid.uuid4().hex[:6]}",
        bank_name="HDFC",
        filename="SecTest_Statement.pdf",
        file_size_bytes=os.path.getsize(pdf_path),
        # Exactly 64 hex chars — email_attachments.file_hash is VARCHAR(64), and
        # PostgreSQL rejects anything longer where SQLite silently accepted it.
        file_hash="aabbcc11223344556677889900aabbcc11223344556677889900aabbcc112233",
        local_path=pdf_path,
        import_status="PENDING",
        classification="PDF_PASSWORD_REQUIRED",
        account_id=None
    )
    db.add(att)
    db.commit()
    att_id = str(att.id)

    res = client.post(f"/email/import/{att_id}", headers=headers, json={"account_id": acc_id, "pdf_password": secret_pass})
    assert res.status_code == 200

    # Inspect all columns of EmailAttachment in DB
    att_db = db.query(EmailAttachment).filter(EmailAttachment.id == uuid.UUID(att_id)).first()
    for col in att_db.__table__.columns:
        val = getattr(att_db, col.name)
        assert secret_pass not in str(val), f"Password leaked in EmailAttachment column '{col.name}'!"
    db.close()
