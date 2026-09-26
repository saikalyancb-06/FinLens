import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from main import app as fastapi_app
from app.database.session import Base, get_db
from app.models.user import User
from app.models.refresh_token import RefreshToken
from app.models.account import Account
from app.models.uploaded_file import UploadedFile
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.prediction import Prediction
from app.models.report import Report
from app.models.audit_log import AuditLog

def test_auth_full_flow(client):
    # 1. Register User
    register_res = client.post("/auth/register", json={
        "email": "user@example.com",
        "password": "SecurePassword123!",
        "full_name": "Test User"
    })
    print("DEBUG TEST_AUTH:", register_res.status_code, register_res.text)
    assert register_res.status_code == 201
    user_data = register_res.json()
    assert user_data["email"] == "user@example.com"
    assert user_data["full_name"] == "Test User"

    # 2. Login
    login_res = client.post("/auth/login", json={
        "email": "user@example.com",
        "password": "SecurePassword123!"
    })
    assert login_res.status_code == 200
    tokens = login_res.json()
    assert "access_token" in tokens
    assert "refresh_token" in tokens

    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    # 3. Access Protected Route /auth/me
    me_res = client.get("/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me_res.status_code == 200
    assert me_res.json()["email"] == "user@example.com"

    # 4. Refresh Token
    refresh_res = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_res.status_code == 200
    new_tokens = refresh_res.json()
    assert "access_token" in new_tokens
    assert "refresh_token" in new_tokens

    # 5. Forgot Password
    forgot_res = client.post("/auth/forgot-password", json={"email": "user@example.com"})
    assert forgot_res.status_code == 200
    reset_token = forgot_res.json()["reset_token"]

    # 6. Reset Password
    reset_res = client.post("/auth/reset-password", json={
        "reset_token": reset_token,
        "new_password": "NewSecretPassword456!"
    })
    assert reset_res.status_code == 200

    # 7. Verify login with new password
    new_login_res = client.post("/auth/login", json={
        "email": "user@example.com",
        "password": "NewSecretPassword456!"
    })
    assert new_login_res.status_code == 200
