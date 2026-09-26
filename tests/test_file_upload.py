import io
import os
import tempfile
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from main import app as fastapi_app
from app.database.session import get_db
from app.models.user import User
from app.models.uploaded_file import UploadedFile

def test_file_upload_queue_flow(client):
    # 1. Register & Login User
    register_res = client.post("/auth/register", json={
        "email": "uploader@example.com",
        "password": "Password123!"
    })
    assert register_res.status_code == 201

    login_res = client.post("/auth/login", json={
        "email": "uploader@example.com",
        "password": "Password123!"
    })
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Uploads require at least one registered Bank Master account.
    from conftest import register_bank_account
    register_bank_account(client, headers)

    # 2. Test Invalid File Format (.txt rejection)
    invalid_file = ("test.txt", io.BytesIO(b"Hello world"), "text/plain")
    res_invalid = client.post("/files/upload", files={"file": invalid_file}, headers=headers)
    assert res_invalid.status_code == 400
    assert "Unsupported file format" in res_invalid.json()["detail"]

    # 3. Test Valid Upload (.pdf)
    pdf_file = ("statement.pdf", io.BytesIO(b"%PDF-1.4 sample content"), "application/pdf")
    res_pdf = client.post("/files/upload", files={"file": pdf_file}, headers=headers)
    assert res_pdf.status_code == 202
    pdf_data = res_pdf.json()
    assert pdf_data["filename"] == "statement.pdf"
    assert pdf_data["status"] in ["QUEUED", "PROCESSING", "COMPLETED", "FAILED"]
    assert "file_id" in pdf_data

    file_id = pdf_data["file_id"]

    # 4. Test Valid Upload (.csv)
    csv_file = ("transactions.csv", io.BytesIO(b"date,amount,description\n2026-01-01,100,Test"), "text/csv")
    res_csv = client.post("/files/upload", files={"file": csv_file}, headers=headers)
    assert res_csv.status_code == 202
    assert res_csv.json()["status"] in ["QUEUED", "PROCESSING", "COMPLETED", "FAILED"]

    # 5. Check Status endpoint
    status_res = client.get(f"/files/{file_id}", headers=headers)
    assert status_res.status_code == 200
    assert status_res.json()["file_id"] == file_id
    assert status_res.json()["status"] in ["QUEUED", "PROCESSING", "COMPLETED", "FAILED"]

    # 6. List User Files endpoint
    list_res = client.get("/files/", headers=headers)
    assert list_res.status_code == 200
    assert len(list_res.json()) >= 2
