"""Re-uploading a statement has to be able to fix a statement that never landed.

WHAT HAPPENED. A parsing job died on `column counterparty_memory.kind does not
exist`. Two separate defects then combined to make that one-off fault permanent:

1. The except block recorded the failure with `db_file.status = "FAILED";
   db.commit()` — on the session postgres had already poisoned. The commit
   raised from inside the handler and escaped, so the status stayed
   'PROCESSING', the value written when the job started.

2. /files/upload treated 'PROCESSING' as already-ingested. Re-uploading the
   same statement returned the dead file id, queued no parse, and answered 202.

So the schema got fixed, the user re-uploaded their statement, and nothing
happened — no transactions, no dashboard, no reports, and no error anywhere
saying why. Re-uploading is the only recovery action a user has; it must not be
a no-op.
"""

import datetime
import io
import os
import uuid

import pytest
from sqlalchemy import text

import app.api.files as files_api
import app.services.parsing_queue as pq
from app.models.account import Account
from app.models.statement import Statement
from app.models.transaction import Direction, SourceType, Transaction
from app.models.uploaded_file import UploadedFile
from tests.conftest import TestingSessionLocal, register_bank_account

CSV_BYTES = (
    b"Date,Description,Debit,Credit,Balance\n"
    b"2026-08-10,RETRY TEST PAYMENT,500,0,5000\n"
)


@pytest.fixture
def auth(client):
    email = f"retry_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    tok = client.post("/auth/login",
                      json={"email": email, "password": "Password123!"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}
    user_id = uuid.UUID(client.get("/auth/me", headers=headers).json()["id"])
    register_bank_account(client, headers,
                          account_number=f"5070{uuid.uuid4().int % 10**8:08d}")
    db = TestingSessionLocal()
    try:
        account_id = db.query(Account).filter(Account.user_id == user_id).first().id
    finally:
        db.close()

    yield headers, user_id, account_id

    db = TestingSessionLocal()
    try:
        db.query(Transaction).filter(Transaction.user_id == user_id).delete(
            synchronize_session=False)
        db.query(Statement).filter(Statement.user_id == user_id).delete(
            synchronize_session=False)
        db.query(UploadedFile).filter(UploadedFile.user_id == user_id).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def spy_parse(monkeypatch):
    """Stand in for the background parse so DB state is set by the test, not by luck."""
    calls = []

    def spy(file_id, file_path, user_id=None, account_id=None, **kwargs):
        calls.append(file_id)
        return {"status": "COMPLETED", "total_stored": 0}

    monkeypatch.setattr(files_api, "process_file_parsing_task", spy)
    return calls


def _upload(client, headers):
    return client.post(
        "/files/upload",
        files={"file": ("retry_statement.csv", io.BytesIO(CSV_BYTES), "text/csv")},
        headers=headers,
    )


def _set_status(file_id, status):
    db = TestingSessionLocal()
    try:
        row = db.query(UploadedFile).filter(UploadedFile.id == uuid.UUID(str(file_id))).first()
        row.status = status
        db.commit()
    finally:
        db.close()


def _seed_ledger(user_id, account_id, file_id):
    """One statement with one transaction, linked to the uploaded file."""
    db = TestingSessionLocal()
    try:
        stmt = Statement(
            id=uuid.uuid4(), user_id=user_id, account_id=account_id,
            uploaded_file_id=uuid.UUID(str(file_id)),
            file_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            source_channel="upload", status="parsed",
        )
        db.add(stmt)
        db.flush()
        db.add(Transaction(
            id=uuid.uuid4(), user_id=user_id, account_id=account_id,
            statement_id=stmt.id, direction=Direction.DEBIT,
            debit_paise=500_00, balance_paise=5000_00,
            txn_date=datetime.date(2026, 8, 10),
            narration_raw="RETRY TEST PAYMENT",
            narration_clean="RETRY TEST PAYMENT",
            source_type=SourceType.STATEMENT, booked_currency="INR",
        ))
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# 1. The failure has to be recordable on a session the failure already broke.
# --------------------------------------------------------------------------

def test_a_parse_that_dies_on_a_poisoned_session_is_recorded_as_FAILED(
        auth, tmp_path, monkeypatch):
    """The named regression.

    `db.commit()` on a session postgres has aborted raises. Doing that inside
    the except block loses the FAILED write AND replaces the logged cause with
    a second traceback. The row is then stuck in PROCESSING forever.
    """
    _headers, user_id, _account_id = auth

    path = tmp_path / "poisoned.csv"
    path.write_bytes(CSV_BYTES)

    db = TestingSessionLocal()
    try:
        db_file = UploadedFile(
            id=uuid.uuid4(), user_id=user_id, filename="poisoned.csv",
            file_path=str(path), file_size=len(CSV_BYTES),
            mime_type="text/csv", file_sha256=uuid.uuid4().hex, status="QUEUED",
        )
        db.add(db_file)
        db.commit()
        file_id = db_file.id
    finally:
        db.close()

    class PoisoningStorage:
        def store_processed_transactions(self, db=None, **_kwargs):
            # Exactly the shape of the real incident: a statement postgres
            # refuses, leaving the transaction unusable until someone rolls back.
            db.execute(text("SELECT column_that_does_not_exist"))

    monkeypatch.setattr(pq, "TransactionStorageService", PoisoningStorage)

    summary = pq.process_file_parsing_task(file_id, str(path), user_id)

    assert summary["status"] == "FAILED"
    assert summary["error_message"]

    db = TestingSessionLocal()
    try:
        row = db.query(UploadedFile).filter(UploadedFile.id == file_id).first()
        assert row.status == "FAILED", (
            f"status is {row.status!r}. A file left in PROCESSING is "
            f"indistinguishable from one still being parsed, and the upload "
            f"endpoint will refuse to re-queue it."
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# 2-4. Re-upload has to retry anything that did not reach the ledger.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stuck_status", ["QUEUED", "PROCESSING", "FAILED"])
def test_reupload_requeues_a_file_that_never_reached_the_ledger(
        client, auth, spy_parse, stuck_status):
    headers, _user_id, _account_id = auth

    first = _upload(client, headers)
    assert first.status_code == 202, first.text
    file_id = first.json()["file_id"]
    assert spy_parse == [uuid.UUID(str(file_id))]

    _set_status(file_id, stuck_status)

    second = _upload(client, headers)
    assert second.status_code == 202, second.text
    # Same record — this is still idempotent, no second row for the same bytes.
    assert second.json()["file_id"] == file_id
    # ...but the parse is queued again, which is the whole point.
    assert len(spy_parse) == 2, (
        f"a file left at {stuck_status} with no transactions was treated as "
        f"already ingested; the user has no way to recover it"
    )


def test_reupload_requeues_a_COMPLETED_file_that_stored_nothing(
        client, auth, spy_parse):
    """COMPLETED records how far the job got, not whether the user has the data.

    A run that parsed 60 rows and wrote none used to leave COMPLETED behind. If
    the upload endpoint trusts that label, the statement can never be recovered.
    """
    headers, _user_id, _account_id = auth

    first = _upload(client, headers)
    file_id = first.json()["file_id"]
    _set_status(file_id, "COMPLETED")

    second = _upload(client, headers)
    assert second.json()["file_id"] == file_id
    assert len(spy_parse) == 2


def test_reupload_of_a_genuinely_ingested_file_does_not_reparse(
        client, auth, spy_parse):
    """The other half: idempotency still has to hold, or every re-upload reparses."""
    headers, user_id, account_id = auth

    first = _upload(client, headers)
    file_id = first.json()["file_id"]
    _set_status(file_id, "COMPLETED")
    _seed_ledger(user_id, account_id, file_id)

    second = _upload(client, headers)
    assert second.json()["file_id"] == file_id
    assert len(spy_parse) == 1, "an already-ingested file was parsed again"


def test_a_missing_stored_copy_is_repointed_at_the_new_upload(
        client, auth, spy_parse):
    """Re-queueing a path that no longer resolves would fail all over again."""
    headers, _user_id, _account_id = auth

    first = _upload(client, headers)
    file_id = first.json()["file_id"]

    db = TestingSessionLocal()
    try:
        row = db.query(UploadedFile).filter(
            UploadedFile.id == uuid.UUID(str(file_id))).first()
        old_path = row.file_path
        row.file_path = str(row.file_path) + ".gone"
        row.status = "PROCESSING"
        db.commit()
    finally:
        db.close()

    _upload(client, headers)

    db = TestingSessionLocal()
    try:
        row = db.query(UploadedFile).filter(
            UploadedFile.id == uuid.UUID(str(file_id))).first()
        assert os.path.exists(row.file_path), (
            "the record still points at a file that is not there"
        )
        assert row.file_path != old_path + ".gone"
    finally:
        db.close()
