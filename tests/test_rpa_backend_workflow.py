"""RPA backend workflow tests.

Two architectures live side by side in this module, and it matters which one
each test covers:

* ``/rpa/start`` is now **metadata only**. Credentials are collected on the
  user's own PC by KredoAgent.exe (``agent/core/runner.py``), which drives the
  browser locally and uploads the finished file through ``/files/upload``. The
  server never sees a username, password, OTP or PDF password for a login it
  did not perform. ``test_start_job_never_receives_or_persists_credentials``
  covers that contract.

* ``app/rpa/runner.py`` is the older *server-side* Playwright runner. It is
  still present and still imports the in-memory session store, but nothing in
  the application reaches it: ``run_rpa_job`` has exactly one caller,
  ``app.rpa.routes._run_in_new_loop``, and that helper is itself never called
  by any route or background task. The ``test_legacy_server_side_runner_*``
  tests below therefore exercise it as a **unit**, driving the session store
  directly instead of pretending an HTTP endpoint fills it. They must not be
  read as evidence that this path runs in production.
"""
import asyncio
import datetime
import pathlib
import uuid

import pytest
from fastapi.testclient import TestClient

import app.database.session as db_session_module
from app.models.account import Account
from app.models.rpa_job import RpaJob, RpaJobStatus
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.models.user import User
from app.rpa.base_adapter import BaseBankAdapter, OTPRequired
from app.rpa.registry import ADAPTER_CLASSES, create_bank_adapter, get_supported_banks
from app.rpa.session_store import (
    clear_session,
    get_credentials,
    has_session,
    set_credentials,
    set_otp,
    set_pdf_password,
)
from app.rpa import runner as rpa_runner
from app.rpa import routes as rpa_routes_module
from app.services.parsing_queue import process_file_parsing_task

from app.services.reconciliation_engine import ReconciliationMatchingEngine





class FakePage:
    async def screenshot(self, path=None, full_page=True):
        if path:
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(path).write_bytes(b"fake-screenshot")


class FakeContext:
    async def new_page(self):
        return FakePage()

    async def close(self):
        return None


class FakeBrowser:
    async def new_context(self, **kwargs):
        return FakeContext()

    async def close(self):
        return None


class FakeChromium:
    async def launch(self, **kwargs):
        return FakeBrowser()


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeRpaAdapter(BaseBankAdapter):
    bank_display_name = "Fake Bank"
    bank_key = "testbank"
    statement_path = None
    correct_otp = "654321"

    def __init__(self, debug_mode: bool = False, job_id: str = ""):
        self.debug_mode = debug_mode
        self.job_id = job_id

    async def login(self, page, credentials):
        otp = credentials.get("otp")
        if otp is None:
            raise OTPRequired()
        if otp != self.correct_otp:
            raise OTPRequired()

    async def navigate_to_statements(self, page, params):
        return None

    async def download_statement(self, page, date_range):
        return self.statement_path


from app.utils.security import decode_token


def register_and_login(client: TestClient, email: str, password: str = "StrongPass123!") -> dict:
    client.post("/auth/register", json={"email": email, "password": password})
    res = client.post("/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    token = res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _get_or_create_user(client: TestClient, email: str, password: str = "StrongPass123!") -> uuid.UUID:
    headers = register_and_login(client, email, password)
    db = db_session_module.SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user:
            return user.id
        token = headers["Authorization"].split(" ")[1]
        payload = decode_token(token)
        return uuid.UUID(payload["sub"])
    finally:
        db.close()




def _add_account_for_user_id(user_id: uuid.UUID, bank_code: str = "TEST") -> uuid.UUID:
    db = db_session_module.SessionLocal()
    try:
        account_id = uuid.uuid4()
        account = Account(
            id=account_id,
            user_id=user_id,
            bank_code=bank_code,
            account_number_masked="****1234",
            account_type="CURRENT",
        )
        db.add(account)
        db.commit()
        return account_id
    finally:
        db.close()



def _create_queued_rpa_job(user_id: uuid.UUID, bank_name: str = "testbank") -> str:
    """Insert a QUEUED rpa_jobs row the way /rpa/start does — metadata only.

    Used by the legacy server-side runner tests. /rpa/start no longer accepts or
    stores credentials, so the runner's input (the in-memory session store) has
    to be primed directly by whoever drives the runner.
    """
    db = db_session_module.SessionLocal()
    try:
        job = RpaJob(
            id=uuid.uuid4(),
            user_id=user_id,
            bank_name=bank_name,
            date_range_start=datetime.datetime(2026, 8, 1),
            date_range_end=datetime.datetime(2026, 8, 31),
            status=RpaJobStatus.QUEUED,
            encrypted_credentials=None,
            user_acknowledged="yes",
        )
        db.add(job)
        db.commit()
        return str(job.id)
    finally:
        db.close()


def _assert_credentials_never_persisted(job_id: str) -> None:
    """The job row must never carry credentials, whatever else happened."""
    db = db_session_module.SessionLocal()
    try:
        job = db.query(RpaJob).filter(RpaJob.id == uuid.UUID(job_id)).one()
        assert job.encrypted_credentials is None
    finally:
        db.close()


def _create_statement_file(tmp_path: pathlib.Path) -> pathlib.Path:
    csv_file = tmp_path / "rpa_fixture.csv"
    csv_file.write_text(
        "Date,Particulars,Debit,Credit,Balance\n"
        "01/08/2026,SWIGGY BANGALORE ORDER #99,350.00,0.00,1000.00\n"
        "02/08/2026,BHARATPE PAYOUTS 123,0.00,2000.00,3000.00\n",
        encoding="utf-8",
    )
    return csv_file


def _create_uploaded_file_row(user_id: uuid.UUID, filename: str = "rpa_fixture.csv") -> uuid.UUID:
    """Insert the uploaded_files row that processed_transactions.file_id points at.

    PostgreSQL enforces that foreign key, so a randomly generated file_id with no
    parent row now fails the insert. SQLite accepted it silently.
    """
    from app.models.uploaded_file import UploadedFile

    db = db_session_module.SessionLocal()
    try:
        file_id = uuid.uuid4()
        db.add(UploadedFile(
            id=file_id,
            user_id=user_id,
            filename=filename,
            file_path=f"/tmp/{filename}",
            mime_type="text/csv",
            status="PENDING",
        ))
        db.commit()
        return file_id
    finally:
        db.close()


def _install_fake_bank(monkeypatch):
    monkeypatch.setitem(ADAPTER_CLASSES, "testbank", FakeRpaAdapter)


def _install_fake_playwright(monkeypatch):
    monkeypatch.setattr(rpa_runner, "async_playwright", lambda: FakePlaywright())


async def _fast_sleep(*args, **kwargs):
    return None


import app.services.parsing_queue as pq_module
from tests.conftest import TestingSessionLocal, test_engine

# Ensure in-memory database sharing across modules during tests
db_session_module.SessionLocal = TestingSessionLocal
rpa_runner.SessionLocal = TestingSessionLocal
pq_module.SessionLocal = TestingSessionLocal


def _async_run(coro):
    return asyncio.run(coro)










def test_registry_is_single_source_and_contract(monkeypatch):
    _install_fake_bank(monkeypatch)
    supported = {item["key"] for item in get_supported_banks()}
    assert supported == {"sbi", "icici", "hdfc", "canara", "testbank"}
    assert not hasattr(rpa_runner, "SUPPORTED_BANKS")

    for key in ("sbi", "hdfc", "icici", "canara", "testbank"):
        adapter = create_bank_adapter(key, debug_mode=False, job_id="job-1")
        assert isinstance(adapter, BaseBankAdapter)
        assert hasattr(adapter, "login")
        assert hasattr(adapter, "navigate_to_statements")
        assert hasattr(adapter, "download_statement")


def test_start_job_never_receives_or_persists_credentials(client: TestClient, monkeypatch):
    """/rpa/start queues metadata only — credentials never reach the server.

    Formerly `test_start_job_keeps_credentials_memory_only`, when /rpa/start took
    a username and password and parked them in the in-memory session store for a
    server-side Playwright runner. StartRpaRequest has no credential fields at
    all now (app/rpa/routes.py); KredoAgent.exe collects them on the user's PC.

    The security property that test guarded is preserved and tightened: the job
    row must never carry credentials, and the server-side session store must
    stay empty too — including when a stale client posts a username and password
    anyway, which pydantic silently drops.
    """
    _install_fake_bank(monkeypatch)
    headers = register_and_login(client, "rpa_memory_only@phase2.test")

    metadata_only = {
        "bank_name": "testbank",
        "start_date": "2026-08-01",
        "end_date": "2026-08-31",
        "user_acknowledged": True,
    }

    res = client.post("/rpa/start", json=metadata_only, headers=headers)
    assert res.status_code == 202, res.text
    job_id = res.json()["job_id"]

    db = db_session_module.SessionLocal()
    try:
        job = db.query(RpaJob).filter(RpaJob.id == uuid.UUID(job_id)).one()
        assert job.status == RpaJobStatus.QUEUED
        assert job.encrypted_credentials is None
    finally:
        db.close()

    # Nothing server-side holds credentials for this job — not the DB, not memory.
    assert get_credentials(job_id) == {}
    assert has_session(job_id) is False

    # A client still speaking the old protocol must not change any of that.
    legacy = dict(metadata_only, username="my_user", password="my_password")
    res = client.post("/rpa/start", json=legacy, headers=headers)
    assert res.status_code == 202, res.text
    legacy_job_id = res.json()["job_id"]

    assert "my_user" not in res.text
    assert "my_password" not in res.text
    _assert_credentials_never_persisted(legacy_job_id)
    assert get_credentials(legacy_job_id) == {}
    assert has_session(legacy_job_id) is False


def test_legacy_server_side_runner_otp_attempt_limit(client: TestClient, monkeypatch, tmp_path):
    """Server-side runner gives up after 3 rejected OTPs instead of looping.

    Unit test of app/rpa/runner.py, which no live route reaches (see module
    docstring). The runner reads credentials from the in-memory session store,
    so the store is primed here directly — /rpa/start does not and must not do it.
    """
    from tests.conftest import TestingSessionLocal
    monkeypatch.setattr(rpa_runner, "SessionLocal", TestingSessionLocal)
    _install_fake_bank(monkeypatch)
    user_id = _get_or_create_user(client, "rpa_otp_limit@phase2.test")
    _add_account_for_user_id(user_id)

    job_id = _create_queued_rpa_job(user_id)
    set_credentials(job_id, {"username": "user", "password": "pass"})

    async def _run():
        import app.rpa.runner as rpa_runner_module
        from tests.conftest import TestingSessionLocal
        rpa_runner_module.SessionLocal = TestingSessionLocal
        _install_fake_playwright(monkeypatch)
        monkeypatch.setattr(rpa_runner.asyncio, "sleep", _fast_sleep)
        monkeypatch.setattr(rpa_runner, "MAX_OTP_WAIT_SECONDS", 3)


        csv_file = _create_statement_file(tmp_path)
        FakeRpaAdapter.statement_path = str(csv_file)
        FakeRpaAdapter.correct_otp = "654321"

        task = asyncio.create_task(rpa_runner.run_rpa_job(job_id))
        await asyncio.sleep(0)
        set_otp(job_id, "111111")
        await asyncio.sleep(0)
        set_otp(job_id, "222222")
        await asyncio.sleep(0)
        set_otp(job_id, "333333")
        await task

        db = db_session_module.SessionLocal()
        try:
            job = db.query(RpaJob).filter(RpaJob.id == uuid.UUID(job_id)).one()
            assert job.status == RpaJobStatus.FAILED
            assert "OTP attempt limit exceeded" in (job.error_message or "")
        finally:
            db.close()

    _async_run(_run())

    # Credentials were in memory for the whole run and still never hit the DB.
    _assert_credentials_never_persisted(job_id)
    # _fail() wipes the session; nothing is left behind after a failed run.
    assert has_session(job_id) is False
    assert get_credentials(job_id) == {}


def test_legacy_server_side_runner_otp_timeout(client: TestClient, monkeypatch, tmp_path):
    """Server-side runner fails the job when no OTP is ever submitted.

    Unit test of app/rpa/runner.py — see this module's docstring for why it is
    driven directly rather than through /rpa/start.
    """
    from tests.conftest import TestingSessionLocal
    monkeypatch.setattr(rpa_runner, "SessionLocal", TestingSessionLocal)
    _install_fake_bank(monkeypatch)
    user_id = _get_or_create_user(client, "rpa_otp_timeout@phase2.test")
    _add_account_for_user_id(user_id)

    job_id = _create_queued_rpa_job(user_id)
    set_credentials(job_id, {"username": "user", "password": "pass"})

    async def _run():
        _install_fake_playwright(monkeypatch)
        monkeypatch.setattr(rpa_runner.asyncio, "sleep", _fast_sleep)
        monkeypatch.setattr(rpa_runner, "MAX_OTP_WAIT_SECONDS", 3)

        csv_file = _create_statement_file(tmp_path)
        FakeRpaAdapter.statement_path = str(csv_file)

        await rpa_runner.run_rpa_job(job_id)

        db = db_session_module.SessionLocal()
        try:
            job = db.query(RpaJob).filter(RpaJob.id == uuid.UUID(job_id)).one()
            assert job.status == RpaJobStatus.FAILED
            assert "OTP timeout" in (job.error_message or "")
        finally:
            db.close()

    _async_run(_run())

    _assert_credentials_never_persisted(job_id)
    assert has_session(job_id) is False
    assert get_credentials(job_id) == {}


def test_legacy_server_side_runner_pdf_password_pause_resume(client: TestClient, monkeypatch, tmp_path):
    """Server-side runner pauses for a PDF password and resumes with it.

    Unit test of app/rpa/runner.py — see this module's docstring. Login has to
    succeed for the run to reach the download step, so the primed credentials
    carry the OTP the fake adapter accepts; the OTP pause itself is covered by
    the two tests above.
    """
    from tests.conftest import TestingSessionLocal
    monkeypatch.setattr(rpa_runner, "SessionLocal", TestingSessionLocal)
    _install_fake_bank(monkeypatch)
    user_id = _get_or_create_user(client, "rpa_pdf_pw@phase2.test")
    _add_account_for_user_id(user_id)

    job_id = _create_queued_rpa_job(user_id)
    FakeRpaAdapter.correct_otp = "654321"
    set_credentials(job_id, {"username": "user", "password": "pass", "otp": "654321"})

    async def _run():
        _install_fake_playwright(monkeypatch)
        monkeypatch.setattr(rpa_runner.asyncio, "sleep", _fast_sleep)
        monkeypatch.setattr(rpa_runner, "MAX_OTP_WAIT_SECONDS", 3)

        pdf_file = tmp_path / "protected_NEEDS_PASSWORD.pdf"
        pdf_file.write_bytes(b"%PDF-1.4\n/Encrypt true\n")
        FakeRpaAdapter.statement_path = str(pdf_file)

        captured = {}

        def fake_process_file_parsing_task(*, file_id, file_path, user_id=None, account_id=None, pdf_password=None, rpa_job_id=None):
            captured["pdf_password"] = pdf_password
            return {
                "status": "COMPLETED",
                "total_extracted": 0,
                "total_valid": 0,
                "total_stored": 0,
                "error_message": None,
            }

        monkeypatch.setattr(rpa_runner, "process_file_parsing_task", fake_process_file_parsing_task)

        task = asyncio.create_task(rpa_runner.run_rpa_job(job_id))
        for _ in range(5):
            await asyncio.sleep(0)
        set_pdf_password(job_id, "pdf-secret")
        await task

        assert captured["pdf_password"] == "pdf-secret"

        db = db_session_module.SessionLocal()
        try:
            job = db.query(RpaJob).filter(RpaJob.id == uuid.UUID(job_id)).one()
            assert job.status == RpaJobStatus.SUCCESS
        finally:
            db.close()

    _async_run(_run())

    # Neither the credentials nor the PDF password may be persisted, and the
    # successful run must have cleared the in-memory session behind it.
    _assert_credentials_never_persisted(job_id)
    assert has_session(job_id) is False
    assert get_credentials(job_id) == {}

    clear_session(job_id)








def test_statement_pipeline_and_idempotency_and_reconciliation(client: TestClient, monkeypatch, tmp_path):
    user_id = _get_or_create_user(client, "rpa_pipeline@phase2.test")
    account_id = _add_account_for_user_id(user_id)

    csv_file = _create_statement_file(tmp_path)

    first = process_file_parsing_task(
        file_id=_create_uploaded_file_row(user_id),
        file_path=str(csv_file),
        user_id=user_id,
        account_id=account_id,
    )
    assert first["status"] == "COMPLETED"
    assert first["total_stored"] == 2

    db = db_session_module.SessionLocal()
    try:
        stmt = db.query(Statement).filter(Statement.user_id == user_id, Statement.account_id == account_id).one()
        txns = db.query(Transaction).filter(Transaction.statement_id == stmt.id).order_by(Transaction.row_index).all()
        assert len(txns) == 2
        assert stmt.closing_balance_paise == 300000

        engine = ReconciliationMatchingEngine(db, user_id, account_id, stmt.period_from, stmt.period_to)
        run = engine.execute_run(force=True, book_opening_paise=0)
        assert run.unmatched_bank_count == 2
    finally:
        db.close()

    second = process_file_parsing_task(
        file_id=_create_uploaded_file_row(user_id),
        file_path=str(csv_file),
        user_id=user_id,
        account_id=account_id,
    )
    assert second["status"] == "COMPLETED"

    db = db_session_module.SessionLocal()
    try:
        stmt = db.query(Statement).filter(Statement.user_id == user_id, Statement.account_id == account_id).one()
        txns = db.query(Transaction).filter(Transaction.statement_id == stmt.id).all()
        assert len(txns) == 2
    finally:
        db.close()




