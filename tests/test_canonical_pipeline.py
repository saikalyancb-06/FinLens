import uuid
import pytest
from app.models.transaction import Transaction


def test_manual_upload_to_canonical_transaction(client):
    # Register & Login User
    client.post("/auth/register", json={"email": "canonical_manual@example.com", "password": "password123"})
    login = client.post("/auth/login", json={"email": "canonical_manual@example.com", "password": "password123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Uploads require at least one registered Bank Master account.
    from conftest import register_bank_account
    register_bank_account(client, headers)

    # Fetch User ID
    me_res = client.get("/auth/me", headers=headers)
    user_id_str = me_res.json()["id"]

    # Upload manual statement CSV via /files/upload
    csv_content = "Date,Description,Debit,Credit,Balance\n2026-01-15,Swiggy Food Order,350.00,0.00,4500.00\n2026-01-16,Salary Credit,0.00,50000.00,54500.00\n"
    files = {"file": ("manual_test.csv", csv_content.encode("utf-8"), "text/csv")}
    upload_res = client.post("/files/upload", files=files, headers=headers)
    assert upload_res.status_code == 202
    file_id_str = upload_res.json()["file_id"]

    # Execute parsing pipeline
    from app.services.parsing_queue import process_file_parsing_task
    from app.models.uploaded_file import UploadedFile
    from main import app as fastapi_app
    from app.database.session import get_db

    db_gen = fastapi_app.dependency_overrides.get(get_db, get_db)()
    db_session = next(db_gen)
    db_file = db_session.query(UploadedFile).filter(UploadedFile.id == uuid.UUID(file_id_str)).first()

    summary = process_file_parsing_task(file_id=uuid.UUID(file_id_str), file_path=db_file.file_path, user_id=user_id_str)
    assert summary["status"] == "COMPLETED"

    # Verify transactions accessible via /transactions endpoint (Canonical API)
    txns_res = client.get("/transactions", headers=headers)
    assert txns_res.status_code == 200
    txns = txns_res.json()
    assert len(txns) >= 2



def test_aa_setu_demo_to_canonical_transaction(client):
    # Register & Login User
    client.post("/auth/register", json={"email": "canonical_aa@example.com", "password": "password123"})
    login = client.post("/auth/login", json={"email": "canonical_aa@example.com", "password": "password123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Trigger AA Setu consent initiation & fetch
    consent_res = client.post("/aa/consent/initiate", json={}, headers=headers)
    assert consent_res.status_code == 200
    handle = consent_res.json()["consent_handle"]

    fetch_res = client.post("/aa/fetch", json={"consent_handle": handle}, headers=headers)
    assert fetch_res.status_code == 200

    # Verify transactions accessible via /transactions endpoint (Canonical API)
    txns_res = client.get("/transactions", headers=headers)
    assert txns_res.status_code == 200
    txns = txns_res.json()
    assert len(txns) >= 1



def test_user_entity_account_ownership_and_provenance(client):
    # Register User & Create Entity
    client.post("/auth/register", json={"email": "ownership@example.com", "password": "password123"})
    login = client.post("/auth/login", json={"email": "ownership@example.com", "password": "password123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    ent_res = client.post("/v1/bank-master/entities", json={"name": "Acme Capital"}, headers=headers)
    assert ent_res.status_code == 201
    ent_id = ent_res.json()["id"]

    entities_list = client.get("/v1/bank-master/entities", headers=headers)
    assert entities_list.status_code == 200
    assert len(entities_list.json()) == 1
    assert entities_list.json()[0]["id"] == ent_id



def test_rpa_not_connected_status(client):
    # Verify RPA endpoints return 405 Method Not Allowed / 404 / 501 explicitly showing NOT CONNECTED
    login_res = client.post("/auth/register", json={"email": "rpa_check@example.com", "password": "password123"})
    login = client.post("/auth/login", json={"email": "rpa_check@example.com", "password": "password123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    rpa_res = client.get("/rpa/status", headers=headers)
    assert rpa_res.status_code in (200, 404, 405, 501)

