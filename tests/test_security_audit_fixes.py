import os
import io
import jwt
import pytest
from app.config import settings

def test_sec_01_jwt_secret_validation():
    # 1. Configured valid secret token works
    valid_token = jwt.encode({"sub": "test_user_id", "type": "access"}, settings.JWT_SECRET_KEY, algorithm="HS256")
    payload = jwt.decode(valid_token, settings.JWT_SECRET_KEY, algorithms=["HS256"])
    assert payload["sub"] == "test_user_id"

    # 2. Known default secret token is rejected when verified against active secret (if non-default)
    if settings.JWT_SECRET_KEY != settings.INSECURE_DEFAULT_SECRET:
        default_token = jwt.encode({"sub": "attacker_user", "type": "access"}, settings.INSECURE_DEFAULT_SECRET, algorithm="HS256")
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(default_token, settings.JWT_SECRET_KEY, algorithms=["HS256"])

def test_sec_02_oauth_csrf_prevention(client):
    # Register victim and attacker
    client.post("/auth/register", json={"email": "victim@example.com", "password": "Password123!"})
    res_vic = client.post("/auth/login", json={"email": "victim@example.com", "password": "Password123!"})
    vic_token = res_vic.json()["access_token"]

    client.post("/auth/register", json={"email": "attacker@example.com", "password": "Password123!"})
    res_att = client.post("/auth/login", json={"email": "attacker@example.com", "password": "Password123!"})
    att_token = res_att.json()["access_token"]

    # Attacker generates an OAuth URL / state
    att_connect = client.post("/email/connect", headers={"Authorization": f"Bearer {att_token}"}).json()
    att_auth_url = att_connect["authorization_url"]
    
    import urllib.parse
    parsed = urllib.parse.urlparse(att_auth_url)
    qs = urllib.parse.parse_qs(parsed.query)
    attacker_state = qs["state"][0]

    # Attempting to use attacker's state without server-side resolution for victim or arbitrary state fails
    fake_callback_res = client.get(f"/email/oauth/callback?code=fake_code&state=fake_unregistered_state")
    assert "Invalid, expired, or already-used OAuth state" in fake_callback_res.text

def test_sec_03_file_upload_signature_validation(client):
    # Login user
    client.post("/auth/register", json={"email": "sec_tester@example.com", "password": "Password123!"})
    login_res = client.post("/auth/login", json={"email": "sec_tester@example.com", "password": "Password123!"})
    headers = {"Authorization": f"Bearer {login_res.json()['access_token']}"}

    # Register an account so the upload reaches file-signature validation rather
    # than short-circuiting on the NO_BANK_ACCOUNT precondition.
    from conftest import register_bank_account
    register_bank_account(client, headers)

    # 1. Spoofed PDF (executable disguised as .pdf) -> MUST BE REJECTED
    fake_pdf = ("malicious.pdf", io.BytesIO(b"MZ\x90\x00\x03\x00\x00\x00 Executable payload"), "application/pdf")
    res_fake_pdf = client.post("/files/upload", files={"file": fake_pdf}, headers=headers)
    assert res_fake_pdf.status_code == 400
    assert "INVALID_FILE_STRUCTURE" in res_fake_pdf.json()["detail"]

    # 2. Spoofed XLSX (text disguised as .xlsx) -> MUST BE REJECTED
    fake_xlsx = ("malicious.xlsx", io.BytesIO(b"Not a zip or xlsx container"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    res_fake_xlsx = client.post("/files/upload", files={"file": fake_xlsx}, headers=headers)
    assert res_fake_xlsx.status_code == 400
    assert "INVALID_FILE_STRUCTURE" in res_fake_xlsx.json()["detail"]

    # 3. Spoofed CSV (binary null bytes disguised as .csv) -> MUST BE REJECTED
    fake_csv = ("malicious.csv", io.BytesIO(b"date,amt\x00\x00binary_junk"), "text/csv")
    res_fake_csv = client.post("/files/upload", files={"file": fake_csv}, headers=headers)
    assert res_fake_csv.status_code == 400
    assert "INVALID_FILE_STRUCTURE" in res_fake_csv.json()["detail"]

def test_sec_04_cors_unapproved_origin(client):
    res = client.get("/transactions", headers={"Origin": "https://unauthorized-evil-domain.com"})
    assert res.headers.get("access-control-allow-origin") != "https://unauthorized-evil-domain.com"
