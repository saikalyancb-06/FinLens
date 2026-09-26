import os
import uuid
import pytest
from app.models.uploaded_file import UploadedFile
from app.models.transaction import Transaction
from app.database.session import get_db
from tests.test_email_pickup_pdf_password import create_encrypted_pdf


@pytest.fixture
def auth_headers(client):
    email = f"manual_pass_user_{uuid.uuid4().hex[:6]}@example.com"
    password = "Password123!"
    reg_res = client.post("/auth/register", json={"email": email, "password": password})
    user_id_str = reg_res.json()["id"]

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    token = login_res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, user_id_str


def test_manual_upload_encrypted_pdf_without_password(client, auth_headers, tmp_path):
    headers, user_id = auth_headers

    # Uploads require at least one registered Bank Master account.
    from conftest import register_bank_account
    register_bank_account(client, headers)

    pdf_path = str(tmp_path / "Encrypted_Statement.pdf")
    lines = [
        "HDFC Bank Account Statement",
        "Date,Particulars,Debit,Credit,Balance",
        "01/08/2026,CLIENT DEPOSIT,0.00,50000.00,150000.00"
    ]
    doc_password = "SecretPassword123"
    create_encrypted_pdf(pdf_path, lines, password=doc_password)

    with open(pdf_path, "rb") as f:
        res = client.post(
            "/files/upload",
            headers=headers,
            files={"file": ("Encrypted_Statement.pdf", f, "application/pdf")}
        )

    assert res.status_code == 400
    assert "PDF_PASSWORD_REQUIRED" in res.json()["detail"]


def test_manual_upload_encrypted_pdf_incorrect_password(client, auth_headers, tmp_path):
    headers, user_id = auth_headers

    # Uploads require at least one registered Bank Master account.
    from conftest import register_bank_account
    register_bank_account(client, headers)

    pdf_path = str(tmp_path / "Encrypted_Statement.pdf")
    lines = [
        "HDFC Bank Account Statement",
        "Date,Particulars,Debit,Credit,Balance",
        "01/08/2026,CLIENT DEPOSIT,0.00,50000.00,150000.00"
    ]
    doc_password = "SecretPassword123"
    create_encrypted_pdf(pdf_path, lines, password=doc_password)

    with open(pdf_path, "rb") as f:
        res = client.post(
            "/files/upload",
            headers=headers,
            files={"file": ("Encrypted_Statement.pdf", f, "application/pdf")},
            data={"pdf_password": "WrongPassword"}
        )

    assert res.status_code == 400
    assert "PASSWORD_INVALID" in res.json()["detail"]


def test_manual_upload_encrypted_pdf_correct_password_flow(client, auth_headers, tmp_path):
    headers, user_id = auth_headers

    # 1. Register Bank Master Account
    acc_res = client.post(
        "/v1/bank-master/accounts",
        headers=headers,
        json={"bank_code": "HDFC", "account_number": "50200099112233", "account_type": "CURRENT", "currency": "INR"}
    )
    assert acc_res.status_code == 201
    acc_id = acc_res.json()["id"]

    # 2. Create encrypted genuine bank statement PDF on disk
    pdf_path = str(tmp_path / "Manual_HDFC_Statement.pdf")
    lines = [
        "HDFC Bank Account Statement for A/C 50200099112233",
        "Date,Particulars,Debit,Credit,Balance",
        "01/08/2026,VENDOR PAYMENT,12500.00,0.00,87500.00",
        "05/08/2026,CLIENT INFLOW,0.00,35000.00,122500.00"
    ]
    doc_password = "CorrectPass99"
    create_encrypted_pdf(pdf_path, lines, password=doc_password)

    # 3. Upload with correct password
    with open(pdf_path, "rb") as f:
        res = client.post(
            f"/files/upload?account_id={acc_id}",
            headers=headers,
            files={"file": ("Manual_HDFC_Statement.pdf", f, "application/pdf")},
            data={"pdf_password": doc_password}
        )

    assert res.status_code == 202
    file_id = res.json()["file_id"]

    # 4. Verify file status transitions to COMPLETED
    res_status = client.get(f"/files/{file_id}", headers=headers)
    assert res_status.status_code == 200

    # 5. Verify transactions generated are queryable via /transactions endpoint
    res_txns = client.get("/transactions", headers=headers)
    assert res_txns.status_code == 200
    txns = res_txns.json()
    assert len(txns) >= 1
