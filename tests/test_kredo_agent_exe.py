"""
Phase 4 — Real Executable (KredoAgent.exe) Integration Test
------------------------------------------------------------
Executes dist/KredoAgent.exe directly as a standalone Windows process
(without Python interpreter) to verify that bundled Playwright/Chromium and
local RPA pipeline execute cleanly end-to-end.
"""
import pytest
import os
import subprocess
import uuid
from fastapi.testclient import TestClient

from main import app
import app.database.session as db_session_module
from app.models.rpa_job import RpaJob, RpaJobStatus
from app.models.transaction import Transaction
from app.models.user import User
from app.utils.security import hash_password, create_access_token


@pytest.fixture
def test_setup():
    db = db_session_module.SessionLocal()
    user = db.query(User).filter(User.email == "exe_test@kredo.in").first()
    if not user:
        user = User(
            email="exe_test@kredo.in",
            hashed_password=hash_password("password123"),
            full_name="EXE Test User",
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    token = create_access_token(data={"sub": str(user.id)})
    yield {"db": db, "user": user, "token": token}
    db.close()


def test_standalone_kredo_agent_exe_execution(test_setup, monkeypatch):
    db = test_setup["db"]
    user = test_setup["user"]
    token = test_setup["token"]

    exe_path = os.path.abspath(os.path.join("dist", "KredoAgent.exe"))
    assert os.path.exists(exe_path), f"KredoAgent.exe not found at {exe_path}"

    client = TestClient(app)

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
    job_uuid = uuid.UUID(job_id)

    # In test environment without a running live uvicorn server on port 8000,
    # we intercept server_url inside python or verify Playwright execution directly.
    # To test actual EXE process cleanly: start lightweight local HTTP adapter test server in python.
    import http.server
    import socketserver
    import threading

    class TestServerHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)

            if "/rpa/agent/update-status" in self.path:
                import json
                body = json.loads(post_data.decode('utf-8'))
                res = client.post("/rpa/agent/update-status", headers={"Authorization": f"Bearer {token}"}, json=body)
                self.send_response(res.status_code)
                self.end_headers()
                self.wfile.write(res.content)
            elif "/files/upload" in self.path:
                # Proxy file upload to TestClient
                content_type = self.headers.get('Content-Type')
                res = client.post(
                    "/files/upload",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
                    content=post_data
                )
                self.send_response(res.status_code)
                self.end_headers()
                self.wfile.write(res.content)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 8000), TestServerHandler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()

    # 2. Execute actual KredoAgent.exe process with parameters
    cmd = [
        exe_path,
        "--server", "http://127.0.0.1:8000",
        "--token", token,
        "--job-id", job_id,
        "--bank", "mock_bank",
        "--username", "exe_local_user",
        "--password", "exe_local_pass",
        "--headless"
    ]

    print(f"\n[Phase 4 Test] Running command: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        print(f"[Phase 4 Test] KredoAgent.exe STDOUT:\n{proc.stdout}")
        print(f"[Phase 4 Test] KredoAgent.exe STDERR:\n{proc.stderr}")

        assert proc.returncode == 0, f"KredoAgent.exe failed with exit code {proc.returncode}:\n{proc.stderr}"
        assert "SUCCESS: Statement imported with ID" in proc.stdout

        # 3. Verify Server State & Security Assertions
        db.expire_all()
        verify_db = db_session_module.SessionLocal()
        fresh_job = verify_db.query(RpaJob).filter(RpaJob.id == job_uuid).first()

        status_str = str(fresh_job.status.value if hasattr(fresh_job.status, 'value') else fresh_job.status).lower()
        assert "success" in status_str, f"Expected success status, got {status_str}"
        assert fresh_job.statement_id is not None
        assert fresh_job.encrypted_credentials is None  # Memory-only verification

        # 4. Verify Transactions imported into DB
        txns = verify_db.query(Transaction).filter(Transaction.user_id == user.id).all()
        assert len(txns) >= 4, f"Expected at least 4 transactions, found {len(txns)}"

        print(f"\n✅ PASS: Real KredoAgent.exe executed cleanly with 0 python dependencies!")
    finally:
        server.shutdown()
        server.server_close()
