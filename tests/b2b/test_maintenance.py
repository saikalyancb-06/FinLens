"""Tests for the fixes made during the 2026-09-28 pre-deploy check.

* result retention is enforced (on read, on replay, and by the purge job)
* stuck `processing` requests are failed and their idempotency key released
* failed webhook deliveries are actually retried
* a password-protected PDF yields PDF_PASSWORD_REQUIRED / PDF_PASSWORD_INVALID
  instead of NO_TRANSACTIONS_FOUND
* DATABASE_URL normalisation pins psycopg2
"""
from __future__ import annotations

import asyncio
import datetime
import uuid

import pytest
from fastapi.testclient import TestClient

import app.b2b.models  # noqa: F401
from app.b2b import auth as b2b_auth
from app.b2b import idempotency, maintenance, webhooks
from app.b2b.errors import ApiError
from app.b2b.models import AnalysisRequest, RequestStatus, WebhookDelivery
from main import app
from tests.conftest import TestingSessionLocal

UTC = datetime.timezone.utc


@pytest.fixture
def db():
    s = TestingSessionLocal()
    try:
        yield s
    finally:
        s.close()


def _client(db, **kw):
    suffix = uuid.uuid4().hex[:10]
    c = b2b_auth.create_client(db, name=f"M {suffix}", slug=f"maint-{suffix}", **kw)
    c.rate_limit_per_minute = 10000
    c.rate_limit_per_day = 100000
    c.rate_limit_per_month = 1000000
    db.commit()
    return c


def _completed_request(db, client, *, expires_in_hours: float, key=None):
    fp = idempotency.fingerprint("a" * 64, {"x": 1})
    out = idempotency.begin(db, client, key, fp)
    req = out.request
    now = datetime.datetime.now(UTC)
    idempotency.complete(db, req, {"data": {"transactions": [{"n": "SALARY"}]},
                                   "quality": {}},
                         retention_hours=1)
    req.result_expires_at = now + datetime.timedelta(hours=expires_in_hours)
    db.commit()
    return req, fp


# ------------------------------------------------------------- retention

def test_stored_result_respects_expiry(db):
    c = _client(db)
    live, _ = _completed_request(db, c, expires_in_hours=1)
    dead, _ = _completed_request(db, c, expires_in_hours=-1)
    assert idempotency.stored_result(live) is not None
    assert idempotency.stored_result(dead) is None


def test_purge_drops_payload_but_keeps_fingerprint(db):
    c = _client(db)
    dead, fp = _completed_request(db, c, expires_in_hours=-1, key="k-purge")
    live, _ = _completed_request(db, c, expires_in_hours=5)

    purged = maintenance.purge_expired_results(db)
    assert purged >= 1
    db.refresh(dead)
    db.refresh(live)

    # Statement data is gone...
    assert idempotency.stored_result(dead) is None
    assert "SALARY" not in str(dead.result)
    # ...the fingerprint survives, so key reuse is still detected...
    assert idempotency._stored_fingerprint(dead) == fp
    with pytest.raises(ApiError) as exc:
        idempotency.begin(db, c, "k-purge", "different-fingerprint")
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
    # ...and an unexpired result is untouched.
    assert idempotency.stored_result(live) is not None
    # Second pass has nothing left to do for this row.
    assert dead.result_expires_at is None


def test_get_and_replay_return_410_after_expiry(db):
    c = _client(db)
    issued = b2b_auth.issue_key(db, c, name="t")
    secret = issued.secret if hasattr(issued, "secret") else issued[1]
    h = {"Authorization": f"Bearer {secret}", "Idempotency-Key": "k-410"}
    csv = (b"Date,Narration,Debit,Credit,Balance\n"
           b"2026-01-01,SALARY CREDIT ACME,,78000,178000.00\n"
           b"2026-01-05,UPI-BIGBASKET,8000,,170000.00\n")
    with TestClient(app) as tc:
        r = tc.post("/v1/analyze", headers=h,
                    files={"file": ("s.csv", csv, "text/csv")})
        assert r.status_code == 200, r.text
        rid = r.json()["request_id"]

        req = db.query(AnalysisRequest).filter_by(request_id=rid).one()
        req.result_expires_at = datetime.datetime.now(UTC) - datetime.timedelta(minutes=1)
        db.commit()

        g = tc.get(f"/v1/analyze/{rid}", headers={"Authorization": h["Authorization"]})
        assert g.status_code == 410
        assert g.json()["status"] == "expired"

        replay = tc.post("/v1/analyze", headers=h,
                         files={"file": ("s.csv", csv, "text/csv")})
        assert replay.status_code == 410
        assert replay.headers.get("Idempotent-Replay") == "true"
        assert "data" not in replay.json()


# ------------------------------------------------------------ stuck rows

def test_reap_stuck_request_fails_it_and_releases_key(db):
    c = _client(db)
    out = idempotency.begin(db, c, "k-stuck", "fp-1")
    req = out.request
    assert req.status == RequestStatus.PROCESSING
    req.created_at = datetime.datetime.now(UTC) - datetime.timedelta(hours=2)
    db.commit()

    # Before reaping, a retry is told to wait — forever, without this fix.
    with pytest.raises(ApiError) as exc:
        idempotency.begin(db, c, "k-stuck", "fp-1")
    assert exc.value.code == "REQUEST_IN_PROGRESS"
    assert f"/v1/analyze/{req.request_id}" in exc.value.message

    assert maintenance.reap_stuck_requests(db) >= 1
    db.refresh(req)
    assert req.status == RequestStatus.FAILED
    assert req.error_code == "SERVICE_UNAVAILABLE"
    assert req.idempotency_key is None

    # The same key now starts a fresh request.
    again = idempotency.begin(db, c, "k-stuck", "fp-1")
    assert again.replay is False
    assert again.request.request_id != req.request_id


def test_recent_processing_request_is_not_reaped(db):
    c = _client(db)
    req = idempotency.begin(db, c, None, "fp").request
    maintenance.reap_stuck_requests(db)
    db.refresh(req)
    assert req.status == RequestStatus.PROCESSING


# -------------------------------------------------------------- webhooks

class _Resp:
    def __init__(self, code):
        self.status_code = code


class _FakeHttp:
    calls = []
    codes = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def post(self, url, content=None, headers=None):
        type(self).calls.append(url)
        return _Resp(type(self).codes.pop(0) if type(self).codes else 200)


def test_failed_webhook_is_retried_by_maintenance(db, monkeypatch):
    import httpx
    _FakeHttp.calls, _FakeHttp.codes = [], [503]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeHttp)

    c = _client(db)
    c.webhook_url, c.webhook_secret = "https://hooks.example/cb", "s" * 32
    db.commit()

    d = webhooks.enqueue(db, c, "analysis.completed", "req_retry", {"ok": True})
    asyncio.run(webhooks.deliver(db, d))          # first attempt: receiver is down
    db.refresh(d)
    assert d.delivered is False and d.next_attempt_at is not None

    d.next_attempt_at = datetime.datetime.now(UTC) - datetime.timedelta(seconds=1)
    db.commit()

    assert maintenance.retry_due_webhooks() >= 1  # the worker that was missing
    db.expire_all()
    d = db.query(WebhookDelivery).filter_by(id=d.id).one()
    assert d.delivered is True
    assert d.attempts == 2
    assert _FakeHttp.calls.count("https://hooks.example/cb") == 2


# ------------------------------------------------------ encrypted PDFs

@pytest.fixture
def locked_pdf(tmp_path):
    fitz = pytest.importorskip("fitz")
    from fpdf import FPDF
    pdf = FPDF(); pdf.add_page(); pdf.set_font("Helvetica", size=9)
    pdf.cell(0, 6, "Date Narration Debit Credit Balance", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "01/01/2026 SALARY 0 78000 178000", new_x="LMARGIN", new_y="NEXT")
    plain = tmp_path / "p.pdf"
    pdf.output(str(plain))
    locked = tmp_path / "locked.pdf"
    doc = fitz.open(str(plain))
    doc.save(str(locked), encryption=fitz.PDF_ENCRYPT_AES_256,
             owner_pw="owner", user_pw="1234")
    doc.close()
    return str(locked)


def test_locked_pdf_without_password_asks_for_one(locked_pdf):
    from app.b2b.parsers import legacy
    with pytest.raises(ApiError) as exc:
        legacy.parse(locked_pdf)
    assert exc.value.code == "PDF_PASSWORD_REQUIRED"
    assert "pdf_password" in exc.value.message


def test_locked_pdf_with_wrong_password(locked_pdf):
    from app.b2b.parsers import legacy
    with pytest.raises(ApiError) as exc:
        legacy.parse(locked_pdf, password="wrong")
    assert exc.value.code == "PDF_PASSWORD_INVALID"


# ------------------------------------------------------------ DB driver

@pytest.mark.parametrize("given,expected", [
    ("postgresql://u:p@h:5432/d", "postgresql+psycopg2://u:p@h:5432/d"),
    ("postgres://u:p@h/d", "postgresql+psycopg2://u:p@h/d"),
    ("postgresql+psycopg2://u:p@h/d", "postgresql+psycopg2://u:p@h/d"),
    ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
])
def test_database_url_pins_psycopg2(given, expected):
    from app.database.session import normalize_postgres_url
    assert normalize_postgres_url(given) == expected


# ------------------------------------------------- per-IP limiter on /v1

def test_ip_rate_limit_on_v1_uses_the_api_error_envelope(monkeypatch):
    import main as main_module
    from app.config import settings
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE", 2)
    main_module.rate_limit_records.clear()
    with TestClient(app) as tc:
        codes = [tc.get("/v1/formats").status_code for _ in range(3)]
        r = tc.get("/v1/formats")
    main_module.rate_limit_records.clear()
    assert codes[:2] == [200, 200]
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert body["error"]["request_id"] == r.headers["X-Request-Id"]
    assert r.headers["Retry-After"] == "60"
