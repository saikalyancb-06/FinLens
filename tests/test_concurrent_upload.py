import pytest
import io
import uuid
import concurrent.futures
from fastapi.testclient import TestClient

from main import app
from app.database.session import get_db, SessionLocal
from app.models.uploaded_file import UploadedFile

def get_auth_headers(client: TestClient, email: str = "concurrency_user@example.com") -> dict:
    password = "SecurePassword123!"
    client.post("/auth/register", json={"email": email, "password": password})
    res = client.post("/auth/login", json={"email": email, "password": password})
    token = res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}

def test_concurrent_duplicate_upload(client: TestClient):
    """Simulate rapid duplicate file uploads for the same user and verify database uniqueness enforcement."""
    headers = get_auth_headers(client, "concurrent_upload_user@example.com")

    # Uploads require at least one registered Bank Master account.
    from conftest import register_bank_account
    register_bank_account(client, headers)
    file_content = b"Date,Description,Debit,Credit,Balance\n2026-08-10,CONCURRENT TEST,500,0,5000\n"

    # Rapid sequential duplicate uploads
    res1 = client.post(
        "/files/upload",
        files={"file": ("concurrent_statement.csv", io.BytesIO(file_content), "text/csv")},
        headers=headers
    )
    res2 = client.post(
        "/files/upload",
        files={"file": ("concurrent_statement.csv", io.BytesIO(file_content), "text/csv")},
        headers=headers
    )

    assert res1.status_code == 202
    assert res2.status_code == 202
    assert res1.json()["file_id"] == res2.json()["file_id"]

