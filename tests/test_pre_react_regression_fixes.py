import uuid
import datetime
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from main import app
from app.database.session import get_db
from app.models.user import User
from app.models.oauth_state import OAuthState
from app.email.models import ConnectedAccount
from app.aa.models import AaConsent

client = TestClient(app)


def register_and_login(user_email: str, password: str = "StrongPass123!") -> dict:
    """Helper to register and log in a user."""
    client.post("/auth/register", json={
        "email": user_email,
        "password": password,
        "full_name": "Regression Tester",
        "role": "treasurer"
    })
    r = client.post("/auth/login", json={"email": user_email, "password": password})
    assert r.status_code == 200, f"Login failed for {user_email}: {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestPreReactRegressionFixes:
    """Regression tests for Gmail OAuth Foreign Key Integrity and Account Aggregator Consent Initiation."""

    def test_gmail_oauth_callback_foreign_key_integrity(self):
        """Verify Gmail OAuth callback creates ConnectedAccount without SQLite Foreign Key error."""
        db: Session = next(get_db())

        # 1. Create real test user
        email = f"gmail_fk_test_{uuid.uuid4().hex[:6]}@example.com"
        user = User(email=email, hashed_password="hashed_pw_123")
        db.add(user)
        db.commit()
        db.refresh(user)

        assert user.id is not None
        user_id = user.id

        # 2. Create OAuthState for user
        state_value = f"state_fk_test_{uuid.uuid4().hex}"
        oauth_state = OAuthState(
            user_id=user_id,
            state_value=state_value,
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=10),
            consumed=False
        )
        db.add(oauth_state)
        db.commit()

        # 3. Simulate OAuth Callback
        callback_url = f"/email/oauth/callback?state={state_value}&code=demo_code_123"
        r_cb = client.get(callback_url)
        assert r_cb.status_code == 200
        assert "Successfully connected" in r_cb.text or "status\":\"success" in r_cb.text

        # 4. Verify ConnectedAccount.user_id exists & FK constraint passed cleanly
        conn_acc = db.query(ConnectedAccount).filter(ConnectedAccount.user_id == user_id).first()
        assert conn_acc is not None
        assert conn_acc.user_id == user_id
        assert conn_acc.is_active is True
        assert conn_acc.provider == "gmail"

    def test_gmail_oauth_cross_user_state_isolation(self):
        """Verify User B cannot consume or link User A's OAuth state."""
        db: Session = next(get_db())

        user_a = User(email=f"usera_{uuid.uuid4().hex[:6]}@example.com", hashed_password="pw")
        user_b = User(email=f"userb_{uuid.uuid4().hex[:6]}@example.com", hashed_password="pw")
        db.add_all([user_a, user_b])
        db.commit()

        state_a = f"state_usera_{uuid.uuid4().hex}"
        oauth_state_a = OAuthState(
            user_id=user_a.id,
            state_value=state_a,
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=10),
            consumed=False
        )
        db.add(oauth_state_a)
        db.commit()

        # Execute callback with state_a
        r_cb = client.get(f"/email/oauth/callback?state={state_a}&code=demo_code_123")
        assert r_cb.status_code == 200

        # Verify ConnectedAccount is created for User A, NOT User B
        acc_a = db.query(ConnectedAccount).filter(ConnectedAccount.user_id == user_a.id).first()
        acc_b = db.query(ConnectedAccount).filter(ConnectedAccount.user_id == user_b.id).first()

        assert acc_a is not None
        assert acc_b is None

    def test_account_aggregator_consent_initiation_success(self):
        """Verify POST /aa/consent/initiate creates consent handle and returns approval_url without 500 error."""
        headers = register_and_login(f"aa_user_{uuid.uuid4().hex[:6]}@example.com")

        payload = {
            "date_from": "2026-01-01",
            "date_to": "2026-02-01",
            "purpose_code": "101",
            "fi_type": "DEPOSIT"
        }

        r_init = client.post("/aa/consent/initiate", json=payload, headers=headers)
        assert r_init.status_code == 200, f"AA Consent initiate failed: {r_init.text}"

        res_data = r_init.json()
        assert "consent_handle" in res_data
        assert "approval_url" in res_data
        assert res_data["status"] == "PENDING"
        assert res_data["approval_url"].startswith("http")

    def test_clear_all_transactions_endpoint(self):
        """Verify DELETE /transactions/clear clears user transactions and returns HTTP 200 OK."""
        headers = register_and_login(f"clear_tx_user_{uuid.uuid4().hex[:6]}@example.com")

        # Create a transaction
        r_create = client.post("/transactions/", json={
            "narration_raw": "Test Transaction to Clear",
            "credit_paise": 50000,
            "txn_date": "2026-02-01"
        }, headers=headers)
        assert r_create.status_code == 201

        # Verify transaction listed
        r_list = client.get("/transactions", headers=headers)
        assert len(r_list.json()) >= 1

        # Delete / clear all transactions
        r_clear = client.delete("/transactions/clear", headers=headers)
        assert r_clear.status_code == 200
        assert r_clear.json()["status"] == "success"

        # Verify transaction list is now empty for user
        r_list_after = client.get("/transactions", headers=headers)
        assert len(r_list_after.json()) == 0
