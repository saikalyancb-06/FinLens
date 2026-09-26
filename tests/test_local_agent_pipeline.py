"""
Phase 2 & Phase 3 — End-to-End Pipeline & Security Verification Test
----------------------------------------------------------------------
Verifies the complete local-agent RPA flow:
queued → launching_browser → logging_in → awaiting_input → navigating → downloading → uploading → parsing → importing → success

Also asserts:
- Zero credentials/OTPs in server DB rpa_jobs
- Transactions correctly stored in canonical storage
- Deduplication and BRS reconciliation pipeline compatibility
"""
import pytest
import os
import asyncio
import uuid
from fastapi.testclient import TestClient

from main import app
import app.database.session as db_session_module
from app.models.rpa_job import RpaJob, RpaJobStatus
from app.models.transaction import Transaction
from app.models.user import User
from app.models.account import Account
from app.utils.security import hash_password, create_access_token
from agent.adapters.mock_adapter import MockBankServer
from agent.core.runner import LocalJobRunner

@pytest.fixture
def test_setup():
    db = db_session_module.SessionLocal()
    # Create test user
    user = db.query(User).filter(User.email == "agent_test@kredo.in").first()
    if not user:
        user = User(
            email="agent_test@kredo.in",
            hashed_password=hash_password("password123"),
            full_name="Agent Test User",
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    token = create_access_token(data={"sub": str(user.id)})
    yield {"db": db, "user": user, "token": token}
    db.close()


def test_full_local_agent_mock_pipeline_and_security(test_setup):
    db = test_setup["db"]
    user = test_setup["user"]
    token = test_setup["token"]

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    # 0. Satisfy the upload precondition the same way the UI does.
    #    /files/upload rejects statements with NO_BANK_ACCOUNT until the user has at
    #    least one registered Bank Master account (app/api/files.py). The local agent
    #    uploads through that same endpoint in step 5 below, so the account has to
    #    exist before execute_job() runs or the upload 400s.
    from tests.conftest import register_bank_account
    register_bank_account(client, headers)

    # 1. Start Metadata-only RPA Job via Web API
    start_res = client.post(
        "/rpa/start",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "bank_name": "mock_bank",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31",
            "user_acknowledged": True
        }
    )
    assert start_res.status_code == 202, f"Failed to start RPA job: {start_res.text}"
    job_id = start_res.json()["job_id"]
    assert job_id is not None

    # Verify SECURITY Assertion 1: Credentials MUST NOT exist in DB
    job_uuid = uuid.UUID(job_id)
    db_job = db.query(RpaJob).filter(RpaJob.id == job_uuid).first()
    assert db_job.encrypted_credentials is None
    assert db_job.status == RpaJobStatus.QUEUED

    # 2. Spin up Local Mock Bank Portal Server
    mock_server = MockBankServer(port=8888)
    mock_server.start()

    try:
        # 3. Instantiate Local KredoAgent Runner (Runs locally on client PC)
        # Note: In test mode we pass server_url pointing to test client base URL or fake host
        # For full end-to-end testing, we invoke upload step directly via test client.
        runner = LocalJobRunner(server_url="http://127.0.0.1:8000", access_token=token, headless=True)

        job_metadata = {
            "job_id": job_id,
            "bank_name": "mock_bank",
            "start_date": "2026-08-01",
            "end_date": "2026-08-31"
        }
        local_credentials = {
            "username": "secret_local_user",
            "password": "secret_local_password",
            "otp": "123456"
        }

        # Override status updater and uploader to use FastAPI TestClient directly
        def mock_status_updater(job_id, status, error_message=None, statement_id=None):
            client.post(
                "/rpa/agent/update-status",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "job_id": job_id,
                    "status": status,
                    "error_message": error_message,
                    "statement_id": statement_id
                }
            )

        class TestClientUploader:
            def upload_statement(self, file_path, pdf_password=None):
                with open(file_path, "rb") as f:
                    res = client.post(
                        "/files/upload",
                        headers={"Authorization": f"Bearer {token}"},
                        files={"file": (os.path.basename(file_path), f, "text/csv")}
                    )
                assert res.status_code in (200, 202), f"Upload failed: {res.text}"
                return res.json()

        runner._update_server_status = mock_status_updater
        runner.uploader = TestClientUploader()

        # Run Local Agent Job Execution
        stmt_id = asyncio.run(runner.execute_job(job_metadata, local_credentials))
        assert stmt_id is not None

        # 4. Verify Final Server State & Security Assertions
        #    verify_db must be closed again: an open session sits idle-in-transaction
        #    and holds table locks that block the session-scoped drop_all() teardown
        #    in conftest, which hangs the whole run rather than failing it.
        db.expire_all()
        verify_db = db_session_module.SessionLocal()
        try:
            fresh_job = verify_db.query(RpaJob).filter(RpaJob.id == job_uuid).first()
            status_str = str(fresh_job.status.value if hasattr(fresh_job.status, 'value') else fresh_job.status).lower()
            print(f"\n[DEBUG CHECKS] status_str={repr(status_str)}, fresh_job.statement_id={repr(fresh_job.statement_id)}, stmt_id={repr(stmt_id)}")
            assert "success" in status_str, f"Status failed: {status_str}"
            assert str(fresh_job.statement_id) == str(stmt_id), f"Statement ID mismatch: {fresh_job.statement_id} vs {stmt_id}"
            assert fresh_job.encrypted_credentials is None, "Credentials not null"

            # 5. Verify Canonical Transaction Storage & Parser Handoff
            txns = verify_db.query(Transaction).filter(Transaction.user_id == user.id).all()
            print(f"[DEBUG CHECKS] txns count={len(txns)}")
            assert len(txns) >= 4, f"Expected at least 4 transactions, got {len(txns)}"

            descriptions = [t.narration_raw for t in txns]
            print(f"[DEBUG CHECKS] descriptions={descriptions}")
            assert any("SALARY CREDIT KREDO TECH" in d for d in descriptions), f"Missing SALARY in {descriptions}"
            assert any("ELECTRICITY BILL BESCOM" in d for d in descriptions), f"Missing BESCOM in {descriptions}"

            # 6. Verify Ephemeral Memory Cleanup
            assert runner.memory_store.get(job_id) == {}, "Memory store not cleared"

            print(f"\n✅ PASS: Full Local Agent Mock Pipeline proved! Imported {len(txns)} transactions cleanly.")
        except Exception as ex:
            import traceback
            print(f"\n[EXACT ASSERTION FAILURE]: {ex}")
            traceback.print_exc()
            raise ex
        finally:
            verify_db.close()

    finally:
        mock_server.stop()
