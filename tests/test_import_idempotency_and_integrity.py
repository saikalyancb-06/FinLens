import pytest
import io
import uuid
import datetime
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from main import app
from app.database.session import get_db, SessionLocal
from app.models.user import User
from app.models.uploaded_file import UploadedFile
from app.models.transaction import Transaction, Direction

def get_auth_headers(client: TestClient, email: str = "idempotency_user@example.com") -> dict:
    password = "SecurePassword123!"
    client.post("/auth/register", json={"email": email, "password": password})
    res = client.post("/auth/login", json={"email": email, "password": password})
    token = res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Every test in this module uploads a statement, which requires the user to
    # have at least one registered Bank Master account.
    from conftest import register_bank_account
    register_bank_account(client, headers)
    return headers


def test_1_upload_identical_file_twice(client: TestClient):
    """Uploading the exact same file twice by the same user returns the existing file record (Idempotent)."""
    headers = get_auth_headers(client, "user_dup1@example.com")
    
    file_content = b"Date,Description,Debit,Credit,Balance\n2026-08-01,STORE PAYMENT,100,0,900\n"
    
    # First upload
    res1 = client.post(
        "/files/upload",
        files={"file": ("statement_aug.csv", io.BytesIO(file_content), "text/csv")},
        headers=headers
    )
    assert res1.status_code == 202
    data1 = res1.json()
    file_id_1 = data1["file_id"]

    # Second upload (exact same file)
    res2 = client.post(
        "/files/upload",
        files={"file": ("statement_aug.csv", io.BytesIO(file_content), "text/csv")},
        headers=headers
    )
    assert res2.status_code == 202
    data2 = res2.json()
    file_id_2 = data2["file_id"]

    # Idempotency check: returned file_id must match the original
    assert file_id_1 == file_id_2


def test_2_same_contents_different_filename(client: TestClient):
    """Same file bytes uploaded under a different filename is still recognized as duplicate."""
    headers = get_auth_headers(client, "user_dup2@example.com")
    
    file_content = b"Date,Description,Debit,Credit,Balance\n2026-08-02,VENDOR PAYOUT,0,5000,14000\n"

    res1 = client.post(
        "/files/upload",
        files={"file": ("original_file.csv", io.BytesIO(file_content), "text/csv")},
        headers=headers
    )
    assert res1.status_code == 202
    file_id_1 = res1.json()["file_id"]

    # Same bytes, renamed file
    res2 = client.post(
        "/files/upload",
        files={"file": ("renamed_file.csv", io.BytesIO(file_content), "text/csv")},
        headers=headers
    )
    assert res2.status_code == 202
    file_id_2 = res2.json()["file_id"]

    assert file_id_1 == file_id_2


def test_3_different_contents_same_filename(client: TestClient):
    """Different file bytes uploaded with the same filename creates a new import."""
    headers = get_auth_headers(client, "user_dup3@example.com")
    
    content1 = b"Date,Description,Debit,Credit,Balance\n2026-08-01,TXN A,10,0,90\n"
    content2 = b"Date,Description,Debit,Credit,Balance\n2026-08-02,TXN B,20,0,70\n"

    res1 = client.post(
        "/files/upload",
        files={"file": ("monthly.csv", io.BytesIO(content1), "text/csv")},
        headers=headers
    )
    file_id_1 = res1.json()["file_id"]

    res2 = client.post(
        "/files/upload",
        files={"file": ("monthly.csv", io.BytesIO(content2), "text/csv")},
        headers=headers
    )
    file_id_2 = res2.json()["file_id"]

    assert file_id_1 != file_id_2


def test_4_two_users_upload_identical_file(client: TestClient):
    """Two different users uploading the exact same file bytes is allowed (User-Scoped Uniqueness)."""
    headers_a = get_auth_headers(client, "user_a_dup4@example.com")
    headers_b = get_auth_headers(client, "user_b_dup4@example.com")

    shared_content = b"Date,Description,Debit,Credit,Balance\n2026-08-01,COMMON TXN,50,0,950\n"

    res_a = client.post(
        "/files/upload",
        files={"file": ("shared.csv", io.BytesIO(shared_content), "text/csv")},
        headers=headers_a
    )
    assert res_a.status_code == 202
    file_id_a = res_a.json()["file_id"]

    res_b = client.post(
        "/files/upload",
        files={"file": ("shared.csv", io.BytesIO(shared_content), "text/csv")},
        headers=headers_b
    )
    assert res_b.status_code == 202
    file_id_b = res_b.json()["file_id"]

    assert file_id_a != file_id_b


def test_5_foreign_key_enforcement(client: TestClient):
    """Verify Foreign Key integrity enforcement on database insertion.

    PostgreSQL enforces foreign keys unconditionally, so an orphaned insert must
    raise IntegrityError. Under SQLite this depended on a per-connection PRAGMA
    that was easy to lose, which is how the legacy file accumulated rows whose
    parent user no longer existed.
    """
    db_gen = client.app.dependency_overrides.get(get_db, get_db)()
    db: Session = next(db_gen)

    # Attempt inserting a transaction referencing a non-existent user_id
    invalid_user_id = uuid.uuid4()
    orphan_tx = Transaction(
        id=uuid.uuid4(),
        user_id=invalid_user_id,
        direction=Direction.DEBIT,
        debit_paise=1000,
        credit_paise=0,
        txn_date=datetime.date.today(),
        narration_raw="ORPHAN FK TEST"
    )

    db.add(orphan_tx)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
