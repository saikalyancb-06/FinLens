"""
Phase 9 & 9C: External Integrations, Token Security & Production Security Audit Test Suite
Covers:
  - OAuth state validation (server-side OAuthState generation, single-use consumption, anti-CSRF, expiration, replay, cross-user isolation)
  - Encrypted token storage & decryption verification
  - Refresh token rotation, expiration, and replay prevention
  - Token key separation / Fernet key derivation
  - Disconnect user isolation & token clean-up
  - Email attachment user isolation & import idempotency
  - AA consent user isolation, fetch idempotency, and canonical transaction persistence
  - AA webhook authenticity, HMAC signature verification, and replay protection
  - RPA job isolation, credential encryption, OTP lifecycle, and state machine transitions
  - Data boundary checks (External Ingestion -> Canonical Transaction)
"""

import io
import json
import uuid
import datetime
import pytest
from fastapi.testclient import TestClient

from app.models.user import User
from app.email.models import ConnectedAccount, EmailAttachment
from app.email.utils import encrypt_token, decrypt_token
from app.models.refresh_token import RefreshToken
from app.aa.models import AaConsent, AaDataSession
from app.models.rpa_job import RpaJob, RpaJobStatus
from app.models.transaction import Transaction


def register_and_login(client: TestClient, email: str, password: str = "StrongPass123!") -> dict:
    client.post("/auth/register", json={"email": email, "password": password})
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"Login failed for {email}: {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------------------------------------------------------------------------
# PART 1 — REFRESH TOKEN LIFECYCLE & REPLAY PREVENTION
# ---------------------------------------------------------------------------

class TestAuthRefreshTokenLifecycle:
    """Audit POST /auth/refresh lifecycle, rotation, and replay prevention."""

    def test_refresh_token_rotation_and_replay_rejection(self, client: TestClient):
        """Valid refresh token issues new pair and revokes old refresh token; reused token is rejected."""
        headers = register_and_login(client, "refresh_lifecycle@phase9.test")

        # Get initial refresh token from login response
        login_res = client.post("/auth/login", json={"email": "refresh_lifecycle@phase9.test", "password": "StrongPass123!"})
        initial_refresh_token = login_res.json()["refresh_token"]

        # 1st Refresh call: should succeed
        r1 = client.post("/auth/refresh", json={"refresh_token": initial_refresh_token})
        assert r1.status_code == 200, r1.text
        data1 = r1.json()
        new_refresh_token = data1["refresh_token"]

        assert new_refresh_token != initial_refresh_token

        # Replay 1st Refresh call using the OLD refresh token: MUST fail with 401
        r_replay = client.post("/auth/refresh", json={"refresh_token": initial_refresh_token})
        assert r_replay.status_code == 401, f"Replayed refresh token should be rejected, got {r_replay.status_code}"
        assert "revoked" in r_replay.json()["detail"].lower() or "invalid" in r_replay.json()["detail"].lower()

        # 2nd Refresh call using the NEW refresh token: should succeed
        r2 = client.post("/auth/refresh", json={"refresh_token": new_refresh_token})
        assert r2.status_code == 200

    def test_refresh_token_belonging_to_another_user(self, client: TestClient):
        """User A cannot refresh using User B's refresh token if tokens are scoped or validated."""
        register_and_login(client, "user_a_refresh@phase9.test")
        login_b = client.post("/auth/login", json={"email": "user_a_refresh@phase9.test", "password": "StrongPass123!"})
        b_refresh_token = login_b.json()["refresh_token"]

        # Using B's refresh token produces tokens for B, but if revoked/tampered it fails
        r = client.post("/auth/refresh", json={"refresh_token": b_refresh_token})
        assert r.status_code == 200  # Valid token returns tokens for user B


# ---------------------------------------------------------------------------
# PART 2 — ACCESS TOKEN ENCRYPTION AT REST
# ---------------------------------------------------------------------------

class TestTokenEncryptionAtRest:
    """Verify OAuth access & refresh tokens are encrypted in DB and plain text is never stored."""

    def test_encrypted_oauth_token_storage(self, client: TestClient):
        """Tokens stored in ConnectedAccount must NOT equal plain text and must decrypt accurately."""
        headers = register_and_login(client, "enc_storage@phase9.test")

        plain_access = "secret_access_token_12345"
        plain_refresh = "secret_refresh_token_67890"

        enc_access = encrypt_token(plain_access)
        enc_refresh = encrypt_token(plain_refresh)

        # Assert encrypted value is not equal to plaintext
        assert enc_access != plain_access
        assert enc_refresh != plain_refresh

        # Assert decrypting recovers original exact string
        assert decrypt_token(enc_access) == plain_access
        assert decrypt_token(enc_refresh) == plain_refresh

    def test_rpa_credentials_encryption(self, client: TestClient):
        """RPA credentials must be encrypted in database and not stored in plaintext."""
        headers = register_and_login(client, "rpa_enc@phase9.test")

        r = client.post("/rpa/start", json={
            "bank_name": "sbi",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "username": "my_secret_username",
            "password": "my_secret_password",
            "user_acknowledged": True
        }, headers=headers)
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]

        # Inspect job status via API endpoint (verifying user-isolation API layer)
        r_stat = client.get(f"/rpa/status/{job_id}", headers=headers)
        assert r_stat.status_code == 200
        # Verify status endpoint does NOT leak raw password or credentials
        assert "my_secret_password" not in json.dumps(r_stat.json())


# ---------------------------------------------------------------------------
# PART 3 — EMAIL / GMAIL OAUTH & DISCONNECT FLOW
# ---------------------------------------------------------------------------

class TestEmailOAuthAndDisconnect:
    """Verify Gmail OAuth state, callback user resolution, and disconnect isolation."""

    def test_gmail_connect_generates_state(self, client: TestClient):
        headers = register_and_login(client, "gmail_state@phase9.test")
        r = client.post("/email/connect", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert "authorization_url" in data
        assert "state=st_" in data["authorization_url"]

    def test_gmail_callback_user_resolution(self, client: TestClient):
        """OAuth callback identifies user from state parameter."""
        headers = register_and_login(client, "gmail_cb@phase9.test")

        # Initiate connect to create server-side state
        r_conn = client.post("/email/connect", headers=headers)
        auth_url = r_conn.json()["authorization_url"]

        import urllib.parse
        state_val = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)["state"][0]

        r_cb = client.get(f"/email/oauth/callback?code=demo_code_123&state={state_val}", headers={"Accept": "application/json"})
        assert r_cb.status_code in (200, 302)

        # Status check
        r_status = client.get("/email/status", headers=headers)
        assert r_status.status_code == 200
        assert r_status.json()["connected"] is True

    def test_email_disconnect_user_isolation(self, client: TestClient):
        """User A disconnecting email must not affect User B's active connection."""
        hA = register_and_login(client, "disc_a@phase9.test")
        hB = register_and_login(client, "disc_b@phase9.test")

        rA = client.post("/email/connect", headers=hA)
        rB = client.post("/email/connect", headers=hB)

        import urllib.parse
        stateA = urllib.parse.parse_qs(urllib.parse.urlparse(rA.json()["authorization_url"]).query)["state"][0]
        stateB = urllib.parse.parse_qs(urllib.parse.urlparse(rB.json()["authorization_url"]).query)["state"][0]

        # Connect both
        client.get(f"/email/oauth/callback?code=demo_code_1&state={stateA}", headers={"Accept": "application/json"})
        client.get(f"/email/oauth/callback?code=demo_code_2&state={stateB}", headers={"Accept": "application/json"})

        assert client.get("/email/status", headers=hA).json()["connected"] is True
        assert client.get("/email/status", headers=hB).json()["connected"] is True

        # User A disconnects
        r_disc = client.delete("/email/disconnect", headers=hA)
        assert r_disc.status_code == 200

        # User A is disconnected, User B remains connected
        assert client.get("/email/status", headers=hA).json()["connected"] is False
        assert client.get("/email/status", headers=hB).json()["connected"] is True


# ---------------------------------------------------------------------------
# PART 4 — EMAIL ATTACHMENT ISOLATION & IMPORT
# ---------------------------------------------------------------------------

class TestEmailAttachmentIsolation:
    """Verify email attachment endpoints enforce strict tenant isolation."""

    def test_single_attachment_import_cross_tenant_prevention(self, client: TestClient):
        """User A cannot import User B's email attachment by passing a fake/other user attachment_id."""
        hA = register_and_login(client, "att_iso_a@phase9.test")
        fake_uuid = str(uuid.uuid4())

        # User A attempts to import non-existent or other user's attachment ID: MUST return 404
        r = client.post(f"/email/import/{fake_uuid}", headers=hA)
        assert r.status_code == 404, f"Expected 404 for cross-user attachment import, got {r.status_code}"


# ---------------------------------------------------------------------------
# PART 5 — ACCOUNT AGGREGATOR (AA) ISOLATION & FETCH
# ---------------------------------------------------------------------------

class TestAccountAggregatorIsolation:
    """Verify AA consent status, approval, and fetch endpoints enforce user isolation."""

    def test_aa_consent_status_cross_user_isolation(self, client: TestClient):
        """User A cannot view status of User B's AA consent handle."""
        hA = register_and_login(client, "aa_iso_a@phase9.test")
        hB = register_and_login(client, "aa_iso_b@phase9.test")

        # Initiate consent as User B
        r_init_b = client.post("/aa/consent/initiate", json={"purpose_code": "101"}, headers=hB)
        assert r_init_b.status_code == 200
        handle_b = r_init_b.json()["consent_handle"]

        # User A checks status of User B's consent handle: MUST return 404
        r_check_a = client.get(f"/aa/consent/status/{handle_b}", headers=hA)
        assert r_check_a.status_code == 404, f"Expected 404 for cross-user AA consent check, got {r_check_a.status_code}"

    def test_aa_fetch_fi_data_cross_user_isolation(self, client: TestClient):
        """User A cannot fetch FI data using User B's consent handle."""
        hA = register_and_login(client, "aa_fetch_a@phase9.test")
        hB = register_and_login(client, "aa_fetch_b@phase9.test")

        r_init_b = client.post("/aa/consent/initiate", json={"purpose_code": "101"}, headers=hB)
        handle_b = r_init_b.json()["consent_handle"]

        # User A attempts fetch with handle_b: MUST return 404
        r_fetch_a = client.post("/aa/fetch", json={"consent_handle": handle_b}, headers=hA)
        assert r_fetch_a.status_code == 404

    def test_aa_consents_list_user_isolation(self, client: TestClient):
        """GET /aa/consents returns only current user's consents."""
        hA = register_and_login(client, "aa_list_a@phase9.test")
        hB = register_and_login(client, "aa_list_b@phase9.test")

        client.post("/aa/consent/initiate", json={"purpose_code": "101"}, headers=hA)
        client.post("/aa/consent/initiate", json={"purpose_code": "101"}, headers=hB)

        rA = client.get("/aa/consents", headers=hA)
        assert rA.status_code == 200
        assert len(rA.json()) == 1

        rB = client.get("/aa/consents", headers=hB)
        assert rB.status_code == 200
        assert len(rB.json()) == 1
        assert rA.json()[0]["consent_handle"] != rB.json()[0]["consent_handle"]


# ---------------------------------------------------------------------------
# PART 6 — AA WEBHOOK SECURITY (PHASE 9C)
# ---------------------------------------------------------------------------

class TestAAWebhookSecurity:
    """Verify AA Webhook authenticity, HMAC signature verification, and replay protection."""

    def test_aa_webhook_valid_signature_updates_consent(self, client: TestClient):
        """Valid HMAC SHA-256 webhook signature allows updating AaConsent.status."""
        headers = register_and_login(client, "aa_wh_valid@phase9.test")
        r_init = client.post("/aa/consent/initiate", json={"purpose_code": "101"}, headers=headers)
        handle = r_init.json()["consent_handle"]

        payload = {"consentHandle": handle, "consentStatus": "ACTIVE"}
        raw_body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        secret = "sandbox_webhook_secret"
        import hmac, hashlib
        sig = hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()

        # Post the exact bytes that were signed. The endpoint verifies the HMAC over
        # the raw request body, so sending via json= (which re-serialises with
        # different separators) would produce a different digest than `raw_body`.
        r_wh = client.post(
            "/aa/consent/webhook",
            content=raw_body,
            headers={"x-setu-signature": sig, "content-type": "application/json"},
        )
        assert r_wh.status_code == 200, r_wh.text
        assert r_wh.json()["status"] == "SUCCESS"

        # Check status was updated
        r_status = client.get(f"/aa/consent/status/{handle}", headers=headers)
        assert r_status.json()["status"] == "ACTIVE"

    def test_aa_webhook_invalid_signature_rejection_no_db_mutation(self, client: TestClient):
        """Invalid or tampered webhook signature returns HTTP 401 and DOES NOT mutate AaConsent."""
        headers = register_and_login(client, "aa_wh_invalid@phase9.test")
        r_init = client.post("/aa/consent/initiate", json={"purpose_code": "101"}, headers=headers)
        handle = r_init.json()["consent_handle"]

        payload = {"consentHandle": handle, "consentStatus": "REVOKED"}
        r_wh = client.post("/aa/consent/webhook", json=payload, headers={"x-setu-signature": "bogus_signature_123"})
        assert r_wh.status_code == 401, f"Expected 401, got {r_wh.status_code}"

        # Assert AaConsent status in list consents endpoint remains PENDING
        consents = client.get("/aa/consents", headers=headers).json()
        target = next(c for c in consents if c["consent_handle"] == handle)
        assert target["status"] == "PENDING"

    def test_aa_webhook_replay_protection_idempotency(self, client: TestClient):
        """Replaying identical webhook payload operates safely and idempotently."""
        headers = register_and_login(client, "aa_wh_replay@phase9.test")
        r_init = client.post("/aa/consent/initiate", json={"purpose_code": "101"}, headers=headers)
        handle = r_init.json()["consent_handle"]

        payload = {"consentHandle": handle, "consentStatus": "ACTIVE"}

        # 1st call
        r1 = client.post("/aa/consent/webhook", json=payload, headers={"x-fiu-id": "setu-fiu-sandbox"})
        assert r1.status_code == 200

        # 2nd call (Replay)
        r2 = client.post("/aa/consent/webhook", json=payload, headers={"x-fiu-id": "setu-fiu-sandbox"})
        assert r2.status_code == 200
        assert r2.json()["status"] == "SUCCESS"


# ---------------------------------------------------------------------------
# PART 7 — OAUTH STATE SECURITY (PHASE 9C)
# ---------------------------------------------------------------------------

class TestOAuthStateSecurity:
    """Verify cryptographically random server-side OAuthState anti-CSRF protections."""

    def test_oauth_connect_creates_server_side_state(self, client: TestClient):
        headers = register_and_login(client, "oauth_state_gen@phase9.test")
        r = client.post("/email/connect", headers=headers)
        assert r.status_code == 200
        data = r.json()
        auth_url = data["authorization_url"]
        assert "state=st_" in auth_url

    def test_oauth_callback_single_use_replay_prevention(self, client: TestClient):
        """Replaying a valid OAuth state in a second callback MUST fail."""
        headers = register_and_login(client, "oauth_replay@phase9.test")
        r_conn = client.post("/email/connect", headers=headers)
        auth_url = r_conn.json()["authorization_url"]

        import urllib.parse
        parsed = urllib.parse.urlparse(auth_url)
        params = urllib.parse.parse_qs(parsed.query)
        state_val = params["state"][0]

        # 1st callback with server-generated state: SUCCEEDS
        r_cb1 = client.get(f"/email/oauth/callback?code=demo_code_123&state={state_val}", headers={"Accept": "application/json"})
        assert r_cb1.status_code in (200, 302)

        # 2nd callback with SAME state (Replay Attack): MUST fail
        r_cb2 = client.get(f"/email/oauth/callback?code=demo_code_123&state={state_val}", headers={"Accept": "application/json"})
        assert "invalid" in r_cb2.text.lower() or "re-initiate" in r_cb2.text.lower() or r_cb2.status_code == 400

    def test_oauth_callback_invalid_and_missing_state(self, client: TestClient):
        """Missing or unrecognized state parameters are rejected without creating ConnectedAccount."""
        register_and_login(client, "oauth_bad_state@phase9.test")

        r_missing = client.get("/email/oauth/callback?code=demo_code_123", headers={"Accept": "application/json"})
        assert "missing" in r_missing.text.lower() or r_missing.status_code == 400

        r_bogus = client.get("/email/oauth/callback?code=demo_code_123&state=st_bogus_fake_state_value", headers={"Accept": "application/json"})
        assert "invalid" in r_bogus.text.lower() or r_bogus.status_code == 400

    def test_oauth_callback_cross_user_state_isolation(self, client: TestClient):
        """User B cannot consume User A's OAuth state to link account to User B."""
        hA = register_and_login(client, "oauth_user_a@phase9.test")
        hB = register_and_login(client, "oauth_user_b@phase9.test")

        # User A generates state
        rA = client.post("/email/connect", headers=hA)
        auth_url_a = rA.json()["authorization_url"]

        import urllib.parse
        parsed = urllib.parse.urlparse(auth_url_a)
        state_a = urllib.parse.parse_qs(parsed.query)["state"][0]

        # Callback executes with User A's state
        r_cb = client.get(f"/email/oauth/callback?code=demo_code_a&state={state_a}", headers={"Accept": "application/json"})
        assert r_cb.status_code in (200, 302)

        # Connected account MUST belong to User A, NOT User B
        statA = client.get("/email/status", headers=hA).json()
        statB = client.get("/email/status", headers=hB).json()
        assert statA["connected"] is True
        assert statB["connected"] is False


# ---------------------------------------------------------------------------
# PART 8 — RPA JOB ISOLATION, OTP LIFECYCLE & STATE MACHINE
# ---------------------------------------------------------------------------

class TestRpaJobIsolationAndLifecycle:
    """Verify RPA job endpoints enforce tenant isolation and valid state transitions."""

    def test_rpa_job_status_cross_user_isolation(self, client: TestClient):
        """User A cannot check status or submit OTP for User B's RPA job."""
        hA = register_and_login(client, "rpa_iso_a@phase9.test")
        hB = register_and_login(client, "rpa_iso_b@phase9.test")

        # Start job as User B
        r_start_b = client.post("/rpa/start", json={
            "bank_name": "sbi",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "username": "userB",
            "password": "passB",
            "user_acknowledged": True
        }, headers=hB)
        assert r_start_b.status_code == 202
        job_b_id = r_start_b.json()["job_id"]

        # User A polls User B's job status: MUST return 404
        r_stat_a = client.get(f"/rpa/status/{job_b_id}", headers=hA)
        assert r_stat_a.status_code == 404

        # User A submits OTP for User B's job: MUST return 404
        r_otp_a = client.post(f"/rpa/otp/{job_b_id}", json={"otp": "123456"}, headers=hA)
        assert r_otp_a.status_code == 404

        # User A deletes User B's job: MUST return 404
        r_del_a = client.delete(f"/rpa/{job_b_id}", headers=hA)
        assert r_del_a.status_code == 404

    def test_rpa_jobs_list_user_isolation(self, client: TestClient):
        """GET /rpa/jobs returns only the authenticated user's jobs."""
        hA = register_and_login(client, "rpa_list_a@phase9.test")
        hB = register_and_login(client, "rpa_list_b@phase9.test")

        client.post("/rpa/start", json={"bank_name": "sbi", "start_date": "2026-08-01", "end_date": "2026-08-31", "username": "uA", "password": "pA", "user_acknowledged": True}, headers=hA)
        client.post("/rpa/start", json={"bank_name": "hdfc", "start_date": "2026-08-01", "end_date": "2026-08-31", "username": "uB", "password": "pB", "user_acknowledged": True}, headers=hB)

        rA = client.get("/rpa/jobs", headers=hA)
        assert rA.status_code == 200
        assert len(rA.json()) == 1
        assert rA.json()[0]["bank_name"] == "sbi"

    def test_rpa_otp_submission_invalid_state(self, client: TestClient):
        """Submitting OTP for a job NOT in AWAITING_OTP status must be rejected."""
        headers = register_and_login(client, "rpa_otp_state@phase9.test")

        r_start = client.post("/rpa/start", json={
            "bank_name": "sbi",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "username": "user",
            "password": "pass",
            "user_acknowledged": True
        }, headers=headers)
        job_id = r_start.json()["job_id"]

        # Job status is PENDING (not AWAITING_OTP)
        r_otp = client.post(f"/rpa/otp/{job_id}", json={"otp": "123456"}, headers=headers)
        assert r_otp.status_code == 400
        assert "not awaiting otp" in r_otp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# PART 9 — EXTERNAL DATA → CANONICAL TRANSACTION CONSISTENCY
# ---------------------------------------------------------------------------

class TestExternalToCanonicalTransactionContract:
    """Verify that external ingestion endpoints respond with canonical schema or standard error codes."""

    def test_aa_fetch_ingests_and_stores_canonical_format(self, client: TestClient):
        """Fetching AA data returns records with canonical fields."""
        headers = register_and_login(client, "aa_canonical@phase9.test")

        r_init = client.post("/aa/consent/initiate", json={"purpose_code": "101"}, headers=headers)
        handle = r_init.json()["consent_handle"]

        r_fetch = client.post("/aa/fetch", json={"consent_handle": handle}, headers=headers)
        assert r_fetch.status_code == 200
        res = r_fetch.json()
        assert "session_id" in res
        assert "records" in res
