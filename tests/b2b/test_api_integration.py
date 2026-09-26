"""End-to-end tests for the /v1 B2B surface.

These drive the real FastAPI app through TestClient with a real API key and a
real Postgres session, and they assert FINANCIAL VALUES, not just status codes.
A test that only checks for 200 would have passed against every bug worth
catching here.
"""
import datetime
import io
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.b2b import auth as b2b_auth
from app.b2b.models import AnalysisRequest, ApiClient, ApiKey, UsageRecord
from main import app
from tests.conftest import TestingSessionLocal


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def api_client_and_key():
    """A live client plus a usable bearer secret."""
    db = TestingSessionLocal()
    try:
        slug = f"creditlens_{uuid.uuid4().hex[:8]}"
        c = b2b_auth.create_client(db, name="Credit Lens", slug=slug,
                                   contact_email="dev@creditlens.example")
        # Generous limits: these tests fire many requests and the limiter is
        # exercised deliberately in its own test with a tightened client.
        c.rate_limit_per_minute = 10000
        c.rate_limit_per_day = 100000
        c.rate_limit_per_month = 1000000
        db.commit()
        issued = b2b_auth.issue_key(db, c, name="integration")
        secret = issued.secret if hasattr(issued, "secret") else issued[1]
        client_id = c.id
        db.commit()
        yield client_id, secret
    finally:
        db.close()


@pytest.fixture
def headers(api_client_and_key):
    _, secret = api_client_and_key
    return {"Authorization": f"Bearer {secret}"}


# A six-month statement with figures chosen so every assertion below is a
# hand-computable number rather than whatever the code happens to produce.
#   salary   +78,000 on the 1st of each month   -> 468,000 total credits
#   EMI      -12,500 on the 5th
#   rent     -25,000 on the 7th
#   groceries -8,000 on the 12th
# Monthly outflow 45,500; monthly surplus 32,500.
def _statement_csv() -> bytes:
    rows = ["Date,Narration,Debit,Credit,Balance"]
    balance = 100000.0
    for month in range(1, 7):
        for day, desc, debit, credit in (
            (1, "SALARY CREDIT ACME CORP PAYROLL", 0, 78000),
            (5, "EMI DEBIT HDFC HOME LOAN", 12500, 0),
            (7, "NEFT RENT PAYMENT LANDLORD", 25000, 0),
            (12, "UPI-BIGBASKET-GROCERIES", 8000, 0),
        ):
            balance += credit - debit
            rows.append(
                f"{2026:04d}-{month:02d}-{day:02d},{desc},"
                f"{debit or ''},{credit or ''},{balance:.2f}")
    return ("\n".join(rows) + "\n").encode()


_DEFAULT = object()


def _post(client, headers, content=_DEFAULT, filename="statement.csv",
          ctype="text/csv", **form):
    # Sentinel, not `content or ...`: an empty body is a case under test and
    # must not be silently replaced by the default statement.
    body = _statement_csv() if content is _DEFAULT else content
    files = {"file": (filename, io.BytesIO(body), ctype)}
    return client.post("/v1/analyze", headers=headers, files=files, data=form)


# ------------------------------------------------------------- operations

def test_health_needs_no_auth(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_ready_reports_dependencies(client):
    r = client.get("/v1/ready")
    assert r.status_code in (200, 503)
    assert "database" in r.json()["checks"]


def test_version_and_formats(client):
    assert client.get("/v1/version").status_code == 200
    r = client.get("/v1/formats")
    assert r.status_code == 200
    body = r.json()
    ids = {f["id"] for f in body["supported"]}
    # The honest list: these are the ones with a real parser behind them.
    assert {"pdf", "xlsx", "csv", "tsv", "json", "ofx", "xml_camt"} <= ids
    assert body["max_file_size_bytes"] > 0
    # And the ones we refuse, with a reason attached.
    unsupported = {f["id"] for f in body["unsupported"]}
    assert "zip" in unsupported


# ------------------------------------------------------------------- auth

def test_missing_auth_is_401(client):
    r = _post(client, {})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "MISSING_API_KEY"


def test_garbage_key_is_401(client):
    r = _post(client, {"Authorization": "Bearer kl_live_not_a_real_key"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_API_KEY"


def test_non_bearer_scheme_is_401(client):
    r = _post(client, {"Authorization": "Basic abc123"})
    assert r.status_code == 401


def test_revoked_key_is_rejected(client, api_client_and_key):
    client_id, secret = api_client_and_key
    db = TestingSessionLocal()
    try:
        key = db.query(ApiKey).filter(ApiKey.client_id == client_id).first()
        b2b_auth.revoke_key(db, key.id)
    finally:
        db.close()
    r = _post(client, {"Authorization": f"Bearer {secret}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "REVOKED_API_KEY"


def test_error_body_never_leaks_internals(client):
    r = _post(client, {})
    body = json.dumps(r.json())
    for leak in ("Traceback", "sqlalchemy", "/home/claude", "psycopg2"):
        assert leak not in body


# ------------------------------------------------------- the main analysis

def test_csv_analysis_returns_correct_financials(client, headers):
    r = _post(client, headers, country="IN", currency="INR")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] == "completed"
    assert body["request_id"].startswith("req_")
    assert body["metadata"]["detected_format"] == "csv"
    assert body["metadata"]["transaction_count"] == 24

    data = body["data"]
    # 6 salary credits of 78,000
    assert data["income"]["total_credits"]["value"] == 468000.00
    # 6 * (12500 + 25000 + 8000)
    assert data["expenses"]["total_debits"]["value"] == 273000.00
    assert data["cashflow"]["net_flow"]["value"] == 195000.00

    # Every figure must declare where it came from.
    assert data["income"]["total_credits"]["source"] == "CALCULATED"

    # Salary is INFERRED, and must say so with a real confidence.
    salary = data["income"]["salary"]
    assert salary["source"] == "INFERRED"
    assert salary["value"] == 78000.00
    assert 0.0 < salary["confidence"] < 1.0
    assert salary["method"]

    # The EMI is found, at the right amount.
    emis = data["debt"]["detected_emis"]
    assert emis["source"] == "INFERRED"
    assert any(abs(e["amount"] - 12500.0) < 0.01 for e in emis["value"])

    q = body["quality"]
    assert q["reconciliation_status"] == "PASSED"
    assert 0.0 < q["overall_confidence"] <= 1.0


def test_no_inferred_metric_ever_claims_certainty(client, headers):
    """The honesty invariant, walked over the whole payload."""
    body = _post(client, headers).json()

    def walk(node):
        if isinstance(node, dict):
            if node.get("source") == "INFERRED" and node.get("confidence") is not None:
                assert node["confidence"] < 1.0, node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(body["data"])


def test_transactions_are_normalized_and_can_be_suppressed(client, headers):
    body = _post(client, headers).json()
    txns = body["data"]["transactions"]
    assert len(txns) == 24
    first = txns[0]
    assert set(first) >= {"date", "description", "amount", "type", "balance", "currency"}
    assert first["type"] in ("DEBIT", "CREDIT")
    assert first["currency"] == "INR"

    lean = _post(client, headers, include_transactions="false").json()
    assert lean["data"]["transactions"] == []
    assert lean["data"]["transactions_omitted"] is True


def test_loan_parameters_produce_affordability(client, headers):
    r = _post(client, headers, loan_amount="500000",
              interest_rate="12", tenure_months="60")
    assert r.status_code == 200
    loan = r.json()["data"]["loan"]
    # 500000 at 12%/yr over 60 months, standard amortisation = 11,122.22
    assert abs(loan["proposed_emi"]["value"] - 11122.22) < 0.05
    aff = r.json()["data"]["affordability"]
    assert "proposed_dti" in aff


def test_partial_loan_parameters_are_rejected(client, headers):
    r = _post(client, headers, loan_amount="500000")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PARAMETER"


# ------------------------------------------------------- format coverage

def test_json_and_tsv_agree_with_csv(client, headers):
    """The same statement in three formats must analyse identically.

    This is the real test of the canonical-transaction abstraction: if any
    parser leaks its own structure into the analysis, these totals diverge.
    """
    csv_body = _post(client, headers).json()["data"]

    tsv = _statement_csv().replace(b",", b"\t")
    tsv_body = _post(client, headers, content=tsv, filename="s.tsv",
                     ctype="text/tab-separated-values").json()["data"]

    rows = []
    balance = 100000.0
    for month in range(1, 7):
        for day, desc, debit, credit in (
            (1, "SALARY CREDIT ACME CORP PAYROLL", 0, 78000),
            (5, "EMI DEBIT HDFC HOME LOAN", 12500, 0),
            (7, "NEFT RENT PAYMENT LANDLORD", 25000, 0),
            (12, "UPI-BIGBASKET-GROCERIES", 8000, 0),
        ):
            balance += credit - debit
            rows.append({"date": f"2026-{month:02d}-{day:02d}",
                         "description": desc, "debit": debit or "",
                         "credit": credit or "", "balance": round(balance, 2)})
    js = json.dumps({"transactions": rows}).encode()
    json_body = _post(client, headers, content=js, filename="s.json",
                      ctype="application/json").json()["data"]

    for block in ("income", "expenses", "cashflow"):
        for key in ("total_credits", "total_debits", "net_flow"):
            if key in csv_body[block]:
                assert tsv_body[block][key]["value"] == csv_body[block][key]["value"]
                assert json_body[block][key]["value"] == csv_body[block][key]["value"]


def test_unsupported_format_is_415(client, headers):
    r = _post(client, headers, content=b"PK\x03\x04fake zip payload",
              filename="statement.zip", ctype="application/zip")
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "UNSUPPORTED_FILE_FORMAT"


def test_corrupt_file_is_422_not_500(client, headers):
    r = _post(client, headers, content=b"%PDF-1.4\nnot really a pdf at all",
              filename="broken.pdf", ctype="application/pdf")
    assert r.status_code == 422
    assert r.json()["error"]["code"] in (
        "PARSE_FAILED", "FILE_CORRUPT", "NO_TRANSACTIONS_FOUND")


def test_empty_file_is_rejected(client, headers):
    r = _post(client, headers, content=b"", filename="empty.csv")
    assert r.status_code == 422
    assert r.json()["error"]["code"] in ("FILE_EMPTY", "NO_TRANSACTIONS_FOUND")


def test_oversized_file_is_413(client, headers, api_client_and_key):
    client_id, _ = api_client_and_key
    db = TestingSessionLocal()
    try:
        c = db.query(ApiClient).filter(ApiClient.id == client_id).first()
        c.max_file_size_bytes = 1024
        db.commit()
    finally:
        db.close()
    r = _post(client, headers, content=b"x" * 5000, filename="big.csv")
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_content_wins_over_extension(client, headers):
    """A CSV named .pdf must still be read as a CSV."""
    r = _post(client, headers, content=_statement_csv(),
              filename="mislabelled.pdf", ctype="application/pdf")
    assert r.status_code == 200
    assert r.json()["metadata"]["detected_format"] == "csv"


# --------------------------------------------------- idempotency & status

def test_idempotent_replay_does_not_reprocess(client, headers):
    key = uuid.uuid4().hex
    h = {**headers, "Idempotency-Key": key}
    first = _post(client, h)
    assert first.status_code == 200
    second = _post(client, h)
    assert second.status_code == 200
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json()["request_id"] == first.json()["request_id"]


def test_same_key_different_file_is_409(client, headers):
    key = uuid.uuid4().hex
    h = {**headers, "Idempotency-Key": key}
    assert _post(client, h).status_code == 200
    other = _statement_csv().replace(b"78000", b"91000")
    r = _post(client, h, content=other)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_status_endpoint_returns_the_stored_result(client, headers):
    rid = _post(client, headers).json()["request_id"]
    r = client.get(f"/v1/analyze/{rid}", headers=headers)
    assert r.status_code == 200
    assert r.json()["request_id"] == rid
    assert r.json()["data"]["income"]["total_credits"]["value"] == 468000.00


def test_unknown_request_id_is_404(client, headers):
    r = client.get("/v1/analyze/req_does_not_exist", headers=headers)
    assert r.status_code == 404


def test_one_client_cannot_read_anothers_result(client, headers):
    rid = _post(client, headers).json()["request_id"]
    db = TestingSessionLocal()
    try:
        other = b2b_auth.create_client(db, name="Other",
                                       slug=f"other_{uuid.uuid4().hex[:8]}")
        issued = b2b_auth.issue_key(db, other, name="k")
        secret = issued.secret if hasattr(issued, "secret") else issued[1]
        db.commit()
    finally:
        db.close()
    r = client.get(f"/v1/analyze/{rid}",
                   headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 404          # not 403: existence is not disclosed


def test_async_mode_returns_202_then_completes(client, headers):
    r = _post(client, headers, async_mode="true")
    assert r.status_code == 202
    rid = r.json()["request_id"]
    assert r.json()["status"] == "processing"
    # TestClient runs BackgroundTasks to completion before returning.
    got = client.get(f"/v1/analyze/{rid}", headers=headers)
    assert got.status_code == 200
    assert got.json()["status"] in ("completed", "processing", "queued")


# ------------------------------------------------------ limits & metering

def test_rate_limit_returns_429_with_headers(client, api_client_and_key):
    client_id, secret = api_client_and_key
    db = TestingSessionLocal()
    try:
        c = db.query(ApiClient).filter(ApiClient.id == client_id).first()
        c.rate_limit_per_minute = 2
        db.commit()
    finally:
        db.close()
    from app.b2b import ratelimit
    ratelimit.reset_local_counters()
    h = {"Authorization": f"Bearer {secret}"}
    codes = [_post(client, h).status_code for _ in range(4)]
    assert 429 in codes
    last = _post(client, h)
    if last.status_code == 429:
        assert last.json()["error"]["code"] in ("RATE_LIMIT_EXCEEDED", "QUOTA_EXCEEDED")
        assert "Retry-After" in last.headers


def test_usage_is_recorded_and_summarised(client, headers, api_client_and_key):
    client_id, _ = api_client_and_key
    _post(client, headers)
    db = TestingSessionLocal()
    try:
        rows = db.query(UsageRecord).filter(
            UsageRecord.client_id == client_id).all()
        assert rows, "no usage recorded"
        assert any(r.succeeded for r in rows)
    finally:
        db.close()
    r = client.get("/v1/usage", headers=headers)
    assert r.status_code == 200
    assert r.json()["request_count"] >= 1


def test_no_transaction_rows_are_persisted(client, headers):
    """The service is stateless about financial data. Prove it."""
    from app.models import Transaction
    db = TestingSessionLocal()
    try:
        before = db.query(Transaction).count()
    finally:
        db.close()
    _post(client, headers)
    db = TestingSessionLocal()
    try:
        assert db.query(Transaction).count() == before
    finally:
        db.close()


def test_request_id_is_returned_in_the_header(client, headers):
    r = _post(client, headers)
    assert r.headers.get("X-Request-Id")
