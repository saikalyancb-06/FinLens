"""Tests for the B2B security and operations layer.

These run against a real PostgreSQL database, because most of what is being
asserted here is database behaviour: a unique index arbitrating a race, a
timestamptz round-tripping with its zone, a JSON column holding an envelope. A
SQLite stand-in would pass while proving nothing about production.

Every expected figure is worked out by hand in a comment beside the assertion.
"""
from __future__ import annotations

import asyncio
import datetime
import time
import uuid

import pytest
from sqlalchemy import text

# Importing the models registers them on Base.metadata, which the session-scoped
# `setup_test_db` fixture in tests/conftest.py turns into real tables. Test
# modules are imported at collection time, before that fixture runs, so this is
# early enough.
import app.b2b.models  # noqa: F401
from app.b2b import auth as b2b_auth
from app.b2b import errors as err
from app.b2b import idempotency, metering, ratelimit, webhooks
from app.b2b.errors import ApiError
from app.b2b.models import AnalysisRequest, ApiKey, RequestStatus, UsageRecord
from tests.conftest import TestingSessionLocal

UTC = datetime.timezone.utc


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _deterministic_touch():
    """Run the last-used write inline so assertions do not race a thread."""
    original = b2b_auth.TOUCH_IN_BACKGROUND
    b2b_auth.TOUCH_IN_BACKGROUND = False
    yield
    b2b_auth.TOUCH_IN_BACKGROUND = original


@pytest.fixture(autouse=True)
def _clean_limiter():
    ratelimit.reset_local_counters()
    yield
    ratelimit.reset_local_counters()


def make_client(db, **overrides):
    """A fresh client with a unique slug, so tests never collide."""
    suffix = uuid.uuid4().hex[:10]
    params = dict(
        name=f"Test Client {suffix}",
        slug=f"test-{suffix}",
        contact_email="ops@example.com",
    )
    params.update(overrides)
    return b2b_auth.create_client(db, **params)


def bearer(secret: str) -> str:
    return f"Bearer {secret}"


# --------------------------------------------------------------------------- #
# Key generation and storage
# --------------------------------------------------------------------------- #

def test_generated_secret_has_the_documented_shape():
    secret, prefix, digest = b2b_auth.generate_api_key()

    assert secret.startswith("kl_live_")
    # "kl_live_" is 8 chars; token_urlsafe(32) is 43 chars -> 51 total.
    assert len(secret) == 51
    assert len(secret) == b2b_auth.SECRET_LENGTH
    assert prefix == secret[:16]
    assert len(prefix) == 16
    # sha256 hex is 64 characters.
    assert len(digest) == 64
    assert digest == b2b_auth.hash_secret(secret)

    # Two calls must not collide.
    other, _, other_digest = b2b_auth.generate_api_key()
    assert other != secret
    assert other_digest != digest


def test_issue_key_returns_secret_matching_prefix_and_stored_hash(db):
    client = make_client(db)
    key, secret = b2b_auth.issue_key(db, client, name="primary")

    assert secret.startswith(key.key_prefix)
    assert key.key_prefix == secret[:16]
    assert key.key_hash == b2b_auth.hash_secret(secret)
    assert key.is_active is True
    assert key.revoked_at is None
    assert key.use_count == 0
    assert key.client_id == client.id


def test_raw_secret_is_nowhere_in_the_database(db):
    """The strongest form of this assertion: grep the table itself."""
    client = make_client(db)
    key, secret = b2b_auth.issue_key(db, client)

    # No column of the row holds it.
    row = db.query(ApiKey).filter(ApiKey.id == key.id).first()
    for column in ApiKey.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, str):
            assert secret not in value, f"secret leaked into api_keys.{column.name}"

    # And neither does any other row, cast to text wholesale.
    hits = db.execute(
        text("SELECT count(*) FROM api_keys WHERE api_keys::text LIKE :needle"),
        {"needle": f"%{secret}%"},
    ).scalar()
    assert hits == 0

    # The token part of the secret is 43 characters; only the first 8 of those
    # are stored in the prefix, so 35 characters of entropy remain unknown.
    assert len(secret) - len(key.key_prefix) == 35


def test_issued_key_repr_redacts_the_secret(db):
    client = make_client(db)
    issued = b2b_auth.issue_key(db, client)

    assert issued.secret not in repr(issued)
    assert issued.secret not in str(issued)
    assert "redacted" in repr(issued)

    # Still unpacks as the documented 2-tuple.
    key, secret = issued
    assert key.key_prefix == secret[:16]


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

def test_verification_succeeds_and_records_last_use(db):
    client = make_client(db)
    key, secret = b2b_auth.issue_key(db, client, scopes="analyze:write,analyze:read")

    ctx = b2b_auth.verify_api_key(db, bearer(secret), client_ip="203.0.113.7")

    assert ctx.client.id == client.id
    assert ctx.api_key.id == key.id
    assert ctx.scopes == ["analyze:write", "analyze:read"]

    # Telemetry was written (inline, per the autouse fixture) in its own session.
    db.expire_all()
    refreshed = db.query(ApiKey).filter(ApiKey.id == key.id).first()
    assert refreshed.use_count == 1
    assert refreshed.last_used_ip == "203.0.113.7"
    assert refreshed.last_used_at is not None


def test_auth_context_repr_carries_no_secret(db):
    client = make_client(db)
    _, secret = b2b_auth.issue_key(db, client)
    ctx = b2b_auth.verify_api_key(db, bearer(secret))
    assert secret not in repr(ctx)


def test_last_used_write_failure_does_not_fail_the_request(db, monkeypatch):
    client = make_client(db)
    _, secret = b2b_auth.issue_key(db, client)

    def exploding_session_factory():
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(b2b_auth, "SessionLocal", exploding_session_factory)

    # Must still authenticate. Telemetry is not worth a 500.
    ctx = b2b_auth.verify_api_key(db, bearer(secret))
    assert ctx.client.id == client.id


def test_unknown_key_is_invalid(db):
    make_client(db)
    unknown, _, _ = b2b_auth.generate_api_key()

    with pytest.raises(ApiError) as exc:
        b2b_auth.verify_api_key(db, bearer(unknown))
    assert exc.value.code == err.INVALID_API_KEY
    assert exc.value.status_code == 401
    # The message must not echo the token back — an error body ends up in logs
    # and in the integrator's terminal history.
    assert unknown not in exc.value.message


def test_revoked_key_is_rejected(db):
    client = make_client(db)
    key, secret = b2b_auth.issue_key(db, client)
    b2b_auth.revoke_key(db, key.id)

    with pytest.raises(ApiError) as exc:
        b2b_auth.verify_api_key(db, bearer(secret))
    assert exc.value.code == err.REVOKED_API_KEY
    assert exc.value.status_code == 401


def test_expired_key_is_rejected(db):
    client = make_client(db)
    past = datetime.datetime.now(UTC) - datetime.timedelta(seconds=1)
    _, secret = b2b_auth.issue_key(db, client, expires_at=past)

    with pytest.raises(ApiError) as exc:
        b2b_auth.verify_api_key(db, bearer(secret))
    assert exc.value.code == err.EXPIRED_API_KEY
    assert exc.value.status_code == 401


def test_key_expiring_in_the_future_still_works(db):
    client = make_client(db)
    future = datetime.datetime.now(UTC) + datetime.timedelta(days=1)
    _, secret = b2b_auth.issue_key(db, client, expires_at=future)

    ctx = b2b_auth.verify_api_key(db, bearer(secret))
    assert ctx.client.id == client.id


def test_disabled_client_is_rejected(db):
    client = make_client(db)
    _, secret = b2b_auth.issue_key(db, client)
    client.is_active = False
    db.commit()

    with pytest.raises(ApiError) as exc:
        b2b_auth.verify_api_key(db, bearer(secret))
    assert exc.value.code == err.CLIENT_DISABLED
    # 403, not 401: the credential is genuine, the account is off.
    assert exc.value.status_code == 403


@pytest.mark.parametrize("header", [
    None,
    "",
    "   ",
    "Basic dXNlcjpwYXNz",          # wrong scheme
    "Token kl_live_whatever",      # wrong scheme
    "kl_live_whatever",            # no scheme at all
    "Bearer",                      # scheme with no token
    "Bearer    ",                  # scheme with a blank token
])
def test_missing_or_malformed_header_is_missing_api_key(db, header):
    with pytest.raises(ApiError) as exc:
        b2b_auth.verify_api_key(db, header)
    assert exc.value.code == err.MISSING_API_KEY
    assert exc.value.status_code == 401
    assert exc.value.headers.get("WWW-Authenticate") == "Bearer"


def test_bearer_scheme_is_case_insensitive(db):
    client = make_client(db)
    _, secret = b2b_auth.issue_key(db, client)
    ctx = b2b_auth.verify_api_key(db, f"bearer {secret}")
    assert ctx.client.id == client.id


# --------------------------------------------------------------------------- #
# Scopes
# --------------------------------------------------------------------------- #

def test_scope_enforcement(db):
    client = make_client(db)
    _, secret = b2b_auth.issue_key(db, client, scopes="analyze:read")
    ctx = b2b_auth.verify_api_key(db, bearer(secret))

    # Granted scope passes silently.
    b2b_auth.require_scope(ctx, "analyze:read")

    with pytest.raises(ApiError) as exc:
        b2b_auth.require_scope(ctx, "analyze:write")
    assert exc.value.code == err.INSUFFICIENT_SCOPE
    assert exc.value.status_code == 403
    assert exc.value.detail["required_scope"] == "analyze:write"
    assert exc.value.detail["granted_scopes"] == ["analyze:read"]


def test_wildcard_scope_grants_everything(db):
    client = make_client(db)
    _, secret = b2b_auth.issue_key(db, client, scopes="*")
    ctx = b2b_auth.verify_api_key(db, bearer(secret))
    b2b_auth.require_scopes(ctx, ["analyze:write", "admin:anything"])


# --------------------------------------------------------------------------- #
# Rotation
# --------------------------------------------------------------------------- #

def test_rotation_issues_a_working_key_and_the_old_one_survives_until_revoked(db):
    client = make_client(db)
    old_key, old_secret = b2b_auth.issue_key(db, client, name="prod",
                                             scopes="analyze:read")

    new_key, new_secret = b2b_auth.rotate_key(db, old_key.id)

    # The replacement is a distinct, working credential that inherits the scopes.
    assert new_secret != old_secret
    assert new_key.id != old_key.id
    assert new_key.rotated_from_id == old_key.id
    assert new_key.scopes == "analyze:read"
    assert b2b_auth.verify_api_key(db, bearer(new_secret)).api_key.id == new_key.id

    # THE GRACE WINDOW: the old key still authenticates. Rotation without this
    # would be an outage for every process not yet redeployed.
    assert b2b_auth.verify_api_key(db, bearer(old_secret)).api_key.id == old_key.id

    # ...until it is revoked explicitly, which is immediate.
    b2b_auth.revoke_key(db, old_key.id)
    with pytest.raises(ApiError) as exc:
        b2b_auth.verify_api_key(db, bearer(old_secret))
    assert exc.value.code == err.REVOKED_API_KEY

    # And the replacement is unaffected by the old key's revocation.
    assert b2b_auth.verify_api_key(db, bearer(new_secret)).api_key.id == new_key.id


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

def test_minute_limit_allows_n_then_raises_on_n_plus_one(db):
    client = make_client(db, rate_limit_per_minute=5,
                         rate_limit_per_day=1000, rate_limit_per_month=10000)

    for expected_used in range(1, 6):          # 5 calls, all inside the limit
        state = ratelimit.check_and_consume(client, redis_ok=False)
        assert state.minute.used == expected_used
        assert state.minute.limit == 5
        assert state.minute.remaining == 5 - expected_used

    # The 6th is over.
    with pytest.raises(ApiError) as exc:
        ratelimit.check_and_consume(client, redis_ok=False)
    assert exc.value.code == err.RATE_LIMIT_EXCEEDED
    assert exc.value.status_code == 429

    headers = exc.value.headers
    assert headers["X-RateLimit-Limit"] == "5"
    assert headers["X-RateLimit-Remaining"] == "0"
    assert headers["X-RateLimit-Window"] == "minute"
    assert headers["X-RateLimit-Limit-Minute"] == "5"
    assert headers["X-RateLimit-Remaining-Minute"] == "0"
    # Retry-After points inside the current minute, so at most 60s away.
    assert 1 <= int(headers["Retry-After"]) <= 60
    # The reset is an absolute epoch on a minute boundary.
    reset = int(headers["X-RateLimit-Reset-Minute"])
    assert reset % 60 == 0
    assert reset > int(datetime.datetime.now(UTC).timestamp())


def test_success_headers_describe_every_window(db):
    client = make_client(db, rate_limit_per_minute=30,
                         rate_limit_per_day=100, rate_limit_per_month=1000)
    state = ratelimit.check_and_consume(client, redis_ok=False)
    headers = ratelimit.headers_for(state)

    # One call consumed: 29 / 99 / 999 left.
    assert headers["X-RateLimit-Remaining-Minute"] == "29"
    assert headers["X-RateLimit-Remaining-Day"] == "99"
    assert headers["X-RateLimit-Remaining-Month"] == "999"
    assert headers["X-RateLimit-Limit-Day"] == "100"
    assert headers["X-RateLimit-Limit-Month"] == "1000"
    # A successful response carries no Retry-After.
    assert "Retry-After" not in headers
    # The unsuffixed trio tracks the tightest window, which here is the minute
    # one (29 left, versus 99 and 999).
    assert headers["X-RateLimit-Window"] == "minute"
    assert headers["X-RateLimit-Remaining"] == "29"


def test_unsuffixed_headers_follow_the_tightest_window(db):
    client = make_client(db, rate_limit_per_minute=1000,
                         rate_limit_per_day=2, rate_limit_per_month=100000)
    state = ratelimit.check_and_consume(client, redis_ok=False)
    headers = ratelimit.headers_for(state)
    # Day has 1 left; minute has 999. The day window is what the client needs
    # to know about.
    assert headers["X-RateLimit-Window"] == "day"
    assert headers["X-RateLimit-Remaining"] == "1"
    assert headers["X-RateLimit-Limit"] == "2"


def test_day_quota_raises_quota_exceeded(db):
    client = make_client(db, rate_limit_per_minute=1000,
                         rate_limit_per_day=3, rate_limit_per_month=10000)
    for _ in range(3):
        ratelimit.check_and_consume(client, redis_ok=False)

    with pytest.raises(ApiError) as exc:
        ratelimit.check_and_consume(client, redis_ok=False)
    assert exc.value.code == err.QUOTA_EXCEEDED
    assert exc.value.status_code == 429
    assert exc.value.detail["window"] == "day"
    assert exc.value.headers["X-RateLimit-Window"] == "day"
    assert exc.value.headers["X-RateLimit-Remaining-Day"] == "0"
    # The day resets at UTC midnight, which is a multiple of 86400.
    assert int(exc.value.headers["X-RateLimit-Reset-Day"]) % 86400 == 0
    assert int(exc.value.headers["Retry-After"]) >= 1


def test_month_quota_raises_quota_exceeded(db):
    client = make_client(db, rate_limit_per_minute=1000,
                         rate_limit_per_day=1000, rate_limit_per_month=2)
    ratelimit.check_and_consume(client, redis_ok=False)
    ratelimit.check_and_consume(client, redis_ok=False)

    with pytest.raises(ApiError) as exc:
        ratelimit.check_and_consume(client, redis_ok=False)
    assert exc.value.code == err.QUOTA_EXCEEDED
    assert exc.value.detail["window"] == "month"
    assert exc.value.headers["X-RateLimit-Remaining-Month"] == "0"


def test_minute_breach_is_reported_before_a_quota_breach(db):
    """Both windows over at once: the client hears about the recoverable one."""
    client = make_client(db, rate_limit_per_minute=1,
                         rate_limit_per_day=1, rate_limit_per_month=1)
    ratelimit.check_and_consume(client, redis_ok=False)
    with pytest.raises(ApiError) as exc:
        ratelimit.check_and_consume(client, redis_ok=False)
    assert exc.value.code == err.RATE_LIMIT_EXCEEDED


def test_limits_are_per_client(db):
    a = make_client(db, rate_limit_per_minute=1)
    b = make_client(db, rate_limit_per_minute=1)
    ratelimit.check_and_consume(a, redis_ok=False)
    # b has its own counter and is unaffected by a exhausting its own.
    state = ratelimit.check_and_consume(b, redis_ok=False)
    assert state.minute.used == 1


def test_limiter_degrades_when_redis_is_down(db, monkeypatch):
    """Redis failing mid-check must not fail the request."""
    client = make_client(db, rate_limit_per_minute=3)

    calls = {"n": 0}

    def broken_redis(_keys_and_ttls):
        calls["n"] += 1
        return None                      # what _incr_redis returns on failure

    monkeypatch.setattr(ratelimit, "_incr_redis", broken_redis)

    # redis_ok=True, but the backend is dead: the limiter still answers, from
    # process memory, and still counts.
    first = ratelimit.check_and_consume(client, redis_ok=True)
    second = ratelimit.check_and_consume(client, redis_ok=True)
    assert calls["n"] == 2
    assert first.backend == "memory"
    assert first.minute.used == 1
    assert second.minute.used == 2

    # And it still enforces.
    ratelimit.check_and_consume(client, redis_ok=True)
    with pytest.raises(ApiError) as exc:
        ratelimit.check_and_consume(client, redis_ok=True)
    assert exc.value.code == err.RATE_LIMIT_EXCEEDED


def test_limiter_counts_the_same_whichever_backend_answers(db):
    """The Redis path is exercised when Redis is present, memory when it is not.

    Either way the caller sees identical counts — which is the property that
    makes the fallback safe to rely on.
    """
    client = make_client(db, rate_limit_per_minute=4)
    for expected in (1, 2, 3, 4):
        state = ratelimit.check_and_consume(client, redis_ok=None)
        assert state.minute.used == expected
        assert state.backend in ("redis", "memory")
    with pytest.raises(ApiError) as exc:
        ratelimit.check_and_consume(client, redis_ok=None)
    assert exc.value.code == err.RATE_LIMIT_EXCEEDED


def test_memory_counters_do_not_grow_without_bound(db, monkeypatch):
    """The sweep drops expired buckets rather than accumulating them.

    Asserted as "the dead keys are gone", not as "the dict holds exactly three".
    A total count is a claim about every test that has ever run in this process,
    since `_memory_counters` is module-level state — so the exact-count form
    failed for a reason that had nothing to do with the sweep. What this test
    exists to prove is that an expired bucket does not survive one, and that is
    a statement about specific keys.
    """
    ratelimit.reset_local_counters()
    first = make_client(db, rate_limit_per_minute=1000)
    ratelimit.check_and_consume(first, redis_ok=False)

    doomed = [k for k in ratelimit._memory_counters if str(first.id) in k]
    assert len(doomed) == 3, doomed          # minute, day, month

    # Age every bucket past its expiry, then force the sweep interval to elapse.
    with ratelimit._memory_lock:
        for key, (count, _) in list(ratelimit._memory_counters.items()):
            ratelimit._memory_counters[key] = (count, 0.0)
    monkeypatch.setattr(ratelimit, "_last_sweep", 0.0)

    second = make_client(db)
    ratelimit.check_and_consume(second, redis_ok=False)

    survivors = set(ratelimit._memory_counters)
    assert not [k for k in doomed if k in survivors], "expired buckets survived the sweep"
    assert len([k for k in survivors if str(second.id) in k]) == 3
    # And nothing expired is left anywhere, which is the property that actually
    # bounds the dict.
    now = time.monotonic()
    assert not [k for k, (_, exp) in ratelimit._memory_counters.items() if exp <= now]


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #

FILE_A = "a" * 64
FILE_B = "b" * 64


def test_fingerprint_ignores_parameter_order_but_not_content():
    one = idempotency.fingerprint(FILE_A, {"country": "IN", "currency": "INR"})
    two = idempotency.fingerprint(FILE_A, {"currency": "INR", "country": "IN"})
    assert one == two                                   # order is not meaning

    assert idempotency.fingerprint(FILE_B, {"country": "IN"}) != one
    assert idempotency.fingerprint(FILE_A, {"country": "GB"}) != one
    # A None-valued optional is the same as not sending it.
    assert idempotency.fingerprint(FILE_A, {"country": "IN", "currency": "INR",
                                            "password": None}) == one


def test_no_idempotency_key_always_proceeds(db):
    client = make_client(db)
    fp = idempotency.fingerprint(FILE_A, {})

    first = idempotency.begin(db, client, None, fp, file_sha256=FILE_A)
    second = idempotency.begin(db, client, None, fp, file_sha256=FILE_A)

    assert first.replay is False and second.replay is False
    # Two independent requests, two ids. Without a key there is nothing to
    # deduplicate against and pretending otherwise would be a guess.
    assert first.request_id != second.request_id


def test_first_use_of_a_key_claims_it(db):
    client = make_client(db)
    fp = idempotency.fingerprint(FILE_A, {"country": "IN"})

    outcome = idempotency.begin(db, client, "key-1", fp,
                                filename="statement.pdf", file_sha256=FILE_A,
                                file_size_bytes=2048, country="IN")

    assert outcome.replay is False
    assert outcome.request.status == RequestStatus.PROCESSING
    assert outcome.request.idempotency_key == "key-1"
    assert outcome.request.filename == "statement.pdf"
    assert outcome.request.file_size_bytes == 2048
    assert outcome.request_id.startswith("req_")


def test_retry_while_in_progress_raises_request_in_progress(db):
    client = make_client(db)
    fp = idempotency.fingerprint(FILE_A, {})
    first = idempotency.begin(db, client, "key-inflight", fp, file_sha256=FILE_A)

    with pytest.raises(ApiError) as exc:
        idempotency.begin(db, client, "key-inflight", fp, file_sha256=FILE_A)

    assert exc.value.code == err.REQUEST_IN_PROGRESS
    assert exc.value.status_code == 409
    # The caller is told which request to poll rather than being left to guess.
    assert exc.value.detail["request_id"] == first.request_id


def test_replay_returns_the_stored_result(db):
    client = make_client(db)
    fp = idempotency.fingerprint(FILE_A, {"country": "IN"})
    first = idempotency.begin(db, client, "key-replay", fp, file_sha256=FILE_A,
                              country="IN")

    payload = {"verdict": "PASSED", "transactions": 42, "net": "195000.00"}
    idempotency.complete(db, first.request, payload,
                         transaction_count=42, detected_format="pdf",
                         duration_ms=1234, retention_hours=24)

    replay = idempotency.begin(db, client, "key-replay", fp, file_sha256=FILE_A,
                               country="IN")

    assert replay.replay is True
    assert replay.result == payload                    # byte-identical answer
    assert replay.request_id == first.request_id       # same handle, not a new one
    assert replay.request.status == RequestStatus.COMPLETED
    assert replay.request.transaction_count == 42

    # Exactly one request row exists for this key — the retry parsed nothing.
    assert db.query(AnalysisRequest).filter(
        AnalysisRequest.client_id == client.id,
        AnalysisRequest.idempotency_key == "key-replay").count() == 1


def test_replay_of_a_failure_returns_the_failure(db):
    client = make_client(db)
    fp = idempotency.fingerprint(FILE_A, {})
    first = idempotency.begin(db, client, "key-failed", fp, file_sha256=FILE_A)
    idempotency.fail(db, first.request, err.PARSE_FAILED,
                     "The file could not be parsed.")

    replay = idempotency.begin(db, client, "key-failed", fp, file_sha256=FILE_A)
    assert replay.replay is True
    assert replay.request.status == RequestStatus.FAILED
    assert replay.request.error_code == err.PARSE_FAILED


def test_same_key_different_file_is_loud(db):
    client = make_client(db)
    fp_a = idempotency.fingerprint(FILE_A, {"country": "IN"})
    fp_b = idempotency.fingerprint(FILE_B, {"country": "IN"})
    first = idempotency.begin(db, client, "key-reused", fp_a, file_sha256=FILE_A)
    idempotency.complete(db, first.request, {"verdict": "PASSED"})

    with pytest.raises(ApiError) as exc:
        idempotency.begin(db, client, "key-reused", fp_b, file_sha256=FILE_B)

    assert exc.value.code == err.IDEMPOTENCY_KEY_REUSED
    assert exc.value.status_code == 409
    assert exc.value.detail["request_id"] == first.request_id


def test_same_key_different_params_is_also_reuse(db):
    """Same file, different analysis parameters, is a different question."""
    client = make_client(db)
    fp_in = idempotency.fingerprint(FILE_A, {"country": "IN"})
    fp_gb = idempotency.fingerprint(FILE_A, {"country": "GB"})
    idempotency.begin(db, client, "key-params", fp_in, file_sha256=FILE_A)

    with pytest.raises(ApiError) as exc:
        idempotency.begin(db, client, "key-params", fp_gb, file_sha256=FILE_A)
    assert exc.value.code == err.IDEMPOTENCY_KEY_REUSED


def test_reuse_is_checked_before_in_progress(db):
    """A mismatched fingerprint on a still-running request is still reuse.

    Reporting REQUEST_IN_PROGRESS here would send the integrator to look at our
    latency when the actual problem is their key generation.
    """
    client = make_client(db)
    fp_a = idempotency.fingerprint(FILE_A, {})
    fp_b = idempotency.fingerprint(FILE_B, {})
    idempotency.begin(db, client, "key-both", fp_a, file_sha256=FILE_A)

    with pytest.raises(ApiError) as exc:
        idempotency.begin(db, client, "key-both", fp_b, file_sha256=FILE_B)
    assert exc.value.code == err.IDEMPOTENCY_KEY_REUSED


def test_the_same_key_belongs_to_each_client_separately(db):
    """Uniqueness is (client, key) — one tenant cannot collide with another."""
    a = make_client(db)
    b = make_client(db)
    fp = idempotency.fingerprint(FILE_A, {})

    first = idempotency.begin(db, a, "shared-key", fp, file_sha256=FILE_A)
    second = idempotency.begin(db, b, "shared-key", fp, file_sha256=FILE_A)

    assert second.replay is False
    assert second.request_id != first.request_id


def test_the_unique_index_is_what_arbitrates(db):
    """Prove the guard is the database, not a Python check.

    A raw INSERT that bypasses `begin` entirely must still be refused, which is
    what makes the two-workers-at-once case safe.
    """
    from sqlalchemy.exc import IntegrityError

    client = make_client(db)
    fp = idempotency.fingerprint(FILE_A, {})
    idempotency.begin(db, client, "key-race", fp, file_sha256=FILE_A)

    other = TestingSessionLocal()
    try:
        other.add(AnalysisRequest(
            id=uuid.uuid4(), request_id=f"req_{uuid.uuid4().hex}",
            client_id=client.id, idempotency_key="key-race",
            status=RequestStatus.PROCESSING,
        ))
        with pytest.raises(IntegrityError):
            other.commit()
    finally:
        other.rollback()
        other.close()


def test_an_over_long_key_is_rejected_before_the_database_sees_it(db):
    client = make_client(db)
    with pytest.raises(ApiError) as exc:
        idempotency.begin(db, client, "x" * 129,
                          idempotency.fingerprint(FILE_A, {}))
    assert exc.value.code == err.INVALID_PARAMETER


# --------------------------------------------------------------------------- #
# Metering
# --------------------------------------------------------------------------- #

def test_record_usage_writes_a_row(db):
    client = make_client(db)
    _, secret = b2b_auth.issue_key(db, client)
    ctx = b2b_auth.verify_api_key(db, bearer(secret))

    record = metering.record_usage(
        db, ctx, endpoint="/v1/analyze", method="POST", status_code=200,
        succeeded=True, request_id="req_abc", file_processed=True,
        file_size_bytes=4096, detected_format="pdf", transaction_count=180,
        duration_ms=900,
    )

    assert record is not None
    stored = db.query(UsageRecord).filter(UsageRecord.id == record.id).one()
    assert stored.client_id == client.id
    assert stored.api_key_id == ctx.api_key.id
    assert stored.succeeded is True
    assert stored.detected_format == "pdf"
    assert stored.transaction_count == 180


def test_record_usage_never_raises(db, monkeypatch):
    """A broken metering write must not turn a good response into a 500."""
    client = make_client(db)

    class Exploding:
        def add(self, _obj):
            raise RuntimeError("connection reset by peer")

        def commit(self):  # pragma: no cover - never reached
            raise RuntimeError

        def rollback(self):
            raise RuntimeError("rollback also failed")

    result = metering.record_usage(
        Exploding(), client, endpoint="/v1/analyze", method="POST",
        status_code=200, succeeded=True,
    )
    assert result is None                      # reported by returning None


def test_unauthenticated_traffic_is_not_metered(db):
    assert metering.record_usage(db, None, endpoint="/v1/analyze",
                                 method="POST", status_code=401,
                                 succeeded=False) is None


def test_usage_summary_aggregates(db):
    client = make_client(db)
    now = datetime.datetime.now(UTC)
    base = now - datetime.timedelta(hours=1)

    # Ten calls, durations 100..1000ms, so by hand:
    #   count 10, successes 8, failures 2
    #   files processed 8, bytes 8 * 1000 = 8000
    #   mean duration (100+...+1000)/10 = 5500/10 = 550.0
    #   P95 nearest-rank = ceil(0.95 * 10) = rank 10 = 1000.0
    #   by_format  {"pdf": 6, "csv": 2}   (only the 8 successes carry a format)
    #   by_error   {"PARSE_FAILED": 1, "FILE_TOO_LARGE": 1}
    for i in range(8):
        metering.record_usage(
            db, client, endpoint="/v1/analyze", method="POST", status_code=200,
            succeeded=True, file_processed=True, file_size_bytes=1000,
            detected_format="pdf" if i < 6 else "csv",
            transaction_count=10, duration_ms=(i + 1) * 100,
            occurred_at=base + datetime.timedelta(minutes=i),
        )
    metering.record_usage(
        db, client, endpoint="/v1/analyze", method="POST", status_code=422,
        succeeded=False, error_code=err.PARSE_FAILED, duration_ms=900,
        occurred_at=base + datetime.timedelta(minutes=8))
    metering.record_usage(
        db, client, endpoint="/v1/analyze", method="POST", status_code=413,
        succeeded=False, error_code=err.FILE_TOO_LARGE, duration_ms=1000,
        occurred_at=base + datetime.timedelta(minutes=9))

    summary = metering.usage_summary(db, client.id, now - datetime.timedelta(days=1),
                                     now + datetime.timedelta(days=1))

    assert summary["request_count"] == 10
    assert summary["successes"] == 8
    assert summary["failures"] == 2
    assert summary["files_processed"] == 8
    assert summary["total_file_bytes"] == 8000        # 8 x 1000
    assert summary["total_transactions"] == 80        # 8 x 10
    assert summary["mean_duration_ms"] == 550.0
    assert summary["p95_duration_ms"] == 1000.0
    assert summary["by_format"] == {"pdf": 6, "csv": 2}
    assert summary["by_error_code"] == {err.PARSE_FAILED: 1,
                                        err.FILE_TOO_LARGE: 1}


def test_usage_summary_window_is_half_open(db):
    """Consecutive periods must not double-count the boundary row."""
    client = make_client(db)
    boundary = datetime.datetime.now(UTC).replace(microsecond=0)

    metering.record_usage(db, client, endpoint="/v1/analyze", method="POST",
                          status_code=200, succeeded=True, occurred_at=boundary)

    before = metering.usage_summary(db, client.id,
                                    boundary - datetime.timedelta(hours=1), boundary)
    after = metering.usage_summary(db, client.id, boundary,
                                   boundary + datetime.timedelta(hours=1))

    # The row sits exactly on the boundary: counted once, by the later window.
    assert before["request_count"] == 0
    assert after["request_count"] == 1


def test_usage_summary_is_scoped_to_one_client(db):
    a = make_client(db)
    b = make_client(db)
    now = datetime.datetime.now(UTC)
    metering.record_usage(db, a, endpoint="/v1/analyze", method="POST",
                          status_code=200, succeeded=True)
    metering.record_usage(db, b, endpoint="/v1/analyze", method="POST",
                          status_code=200, succeeded=True)

    summary = metering.usage_summary(db, a.id, now - datetime.timedelta(hours=1),
                                     now + datetime.timedelta(hours=1))
    assert summary["request_count"] == 1


def test_usage_summary_of_an_empty_window(db):
    client = make_client(db)
    now = datetime.datetime.now(UTC)
    summary = metering.usage_summary(db, client.id,
                                     now - datetime.timedelta(days=2),
                                     now - datetime.timedelta(days=1))
    assert summary["request_count"] == 0
    assert summary["failures"] == 0
    # No observations, so no invented latency figure.
    assert summary["mean_duration_ms"] is None
    assert summary["p95_duration_ms"] is None


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #

SECRET = "whsec_" + "z" * 32
BODY = '{"event":"analysis.completed","request_id":"req_1"}'


def test_signature_round_trips():
    ts = int(datetime.datetime.now(UTC).timestamp())
    header = webhooks.signature_header(SECRET, BODY, timestamp=ts)

    assert header.startswith(f"t={ts},v1=")
    assert webhooks.verify_signature(SECRET, header, BODY) is True


def test_signature_matches_a_hand_computed_hmac():
    """The scheme is what the docstring says it is, not whatever the code does."""
    import hashlib
    import hmac

    ts = 1757404800
    expected = hmac.new(SECRET.encode(), f"{ts}.{BODY}".encode(),
                        hashlib.sha256).hexdigest()
    assert webhooks.sign_payload(SECRET, ts, BODY) == expected
    assert webhooks.signature_header(SECRET, BODY, timestamp=ts) == \
        f"t={ts},v1={expected}"


def test_tampered_body_fails():
    header = webhooks.signature_header(SECRET, BODY)
    tampered = BODY.replace("req_1", "req_2")
    assert webhooks.verify_signature(SECRET, header, tampered) is False


def test_tampered_timestamp_fails():
    ts = int(datetime.datetime.now(UTC).timestamp())
    header = webhooks.signature_header(SECRET, BODY, timestamp=ts)
    # Same signature, a different `t`: the timestamp is inside the signed
    # material, so swapping it invalidates the digest.
    forged = header.replace(f"t={ts}", f"t={ts - 1}")
    assert webhooks.verify_signature(SECRET, forged, BODY) is False


def test_wrong_secret_fails():
    header = webhooks.signature_header(SECRET, BODY)
    assert webhooks.verify_signature("whsec_wrong", header, BODY) is False


def test_stale_timestamp_fails_even_with_a_valid_signature():
    """Replay protection: an old capture is correctly signed and still refused."""
    old = int(datetime.datetime.now(UTC).timestamp()) - 600      # 10 minutes ago
    header = webhooks.signature_header(SECRET, BODY, timestamp=old)

    # The digest itself is genuine...
    assert webhooks.sign_payload(SECRET, old, BODY) in header
    # ...but 600s is outside the 300s default tolerance.
    assert webhooks.verify_signature(SECRET, header, BODY) is False
    # Widening the tolerance accepts it, which shows freshness is the only
    # thing that rejected it.
    assert webhooks.verify_signature(SECRET, header, BODY,
                                     tolerance_seconds=900) is True


def test_a_future_timestamp_is_also_refused():
    future = int(datetime.datetime.now(UTC).timestamp()) + 600
    header = webhooks.signature_header(SECRET, BODY, timestamp=future)
    assert webhooks.verify_signature(SECRET, header, BODY) is False


@pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=deadbeef",
                                    "v1=deadbeef", "t=1757404800"])
def test_malformed_signature_headers_fail_closed(header):
    assert webhooks.verify_signature(SECRET, header, BODY) is False


def test_event_id_is_stable(db):
    client = make_client(db, webhook_url="https://example.test/hook",
                         webhook_secret=SECRET)
    first = webhooks.enqueue(db, client, "analysis.completed", "req_1", {"a": 1})
    second = webhooks.enqueue(db, client, "analysis.completed", "req_1", {"a": 1})

    # Same event, same id, one row — the receiver can dedupe on it.
    assert first.event_id == second.event_id
    assert first.id == second.id
    assert first.event_id.startswith("evt_")
    # A different request is a different event.
    third = webhooks.enqueue(db, client, "analysis.completed", "req_2", {"a": 1})
    assert third.event_id != first.event_id


def test_enqueue_without_a_url_is_a_no_op(db):
    client = make_client(db)                     # no webhook_url configured
    assert webhooks.enqueue(db, client, "analysis.completed", "req_1", {}) is None


# ---- delivery -------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient, recording what was actually sent."""

    sent = []
    behaviour = 200                # an int status, or an exception instance

    def __init__(self, *_args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, content=None, headers=None):
        type(self).sent.append({"url": url, "content": content,
                                "headers": headers, "timeout": self.timeout})
        if isinstance(type(self).behaviour, Exception):
            raise type(self).behaviour
        return _FakeResponse(type(self).behaviour)


@pytest.fixture
def fake_http(monkeypatch):
    import httpx
    _FakeAsyncClient.sent = []
    _FakeAsyncClient.behaviour = 200
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


def test_successful_delivery_is_signed_and_marked_delivered(db, fake_http):
    client = make_client(db, webhook_url="https://example.test/hook",
                         webhook_secret=SECRET)
    delivery = webhooks.enqueue(db, client, "analysis.completed", "req_ok",
                                {"status": "completed"})

    asyncio.run(webhooks.deliver(db, delivery))

    assert delivery.delivered is True
    assert delivery.last_status_code == 200
    assert delivery.attempts == 1
    assert delivery.next_attempt_at is None
    assert delivery.delivered_at is not None

    sent = fake_http.sent[-1]
    assert sent["timeout"] == webhooks.DELIVERY_TIMEOUT_SECONDS   # 10s
    assert sent["headers"]["X-Kredo-Event-Id"] == delivery.event_id

    # The signature must verify against the EXACT bytes that were sent — this is
    # the property integrators trip over, so it is asserted directly.
    body = sent["content"].decode("utf-8")
    assert webhooks.verify_signature(SECRET, sent["headers"]["X-Kredo-Signature"],
                                     body) is True


def test_5xx_is_retried_with_growing_backoff(db, fake_http):
    client = make_client(db, webhook_url="https://example.test/hook",
                         webhook_secret=SECRET)
    delivery = webhooks.enqueue(db, client, "analysis.completed", "req_5xx", {})
    fake_http.behaviour = 503

    before = datetime.datetime.now(UTC)
    asyncio.run(webhooks.deliver(db, delivery))

    assert delivery.delivered is False
    assert delivery.attempts == 1
    assert delivery.last_status_code == 503
    # First retry is BACKOFF_BASE_SECONDS (30s) away.
    assert delivery.next_attempt_at is not None
    gap = (delivery.next_attempt_at - before).total_seconds()
    assert 25 <= gap <= 35

    asyncio.run(webhooks.deliver(db, delivery))
    assert delivery.attempts == 2
    # Second retry doubles: 60s.
    gap2 = (delivery.next_attempt_at - datetime.datetime.now(UTC)).total_seconds()
    assert 55 <= gap2 <= 65


def test_timeout_is_retried(db, fake_http):
    import httpx

    client = make_client(db, webhook_url="https://example.test/hook",
                         webhook_secret=SECRET)
    delivery = webhooks.enqueue(db, client, "analysis.completed", "req_to", {})
    fake_http.behaviour = httpx.ReadTimeout("timed out")

    asyncio.run(webhooks.deliver(db, delivery))

    assert delivery.delivered is False
    assert delivery.attempts == 1
    assert delivery.next_attempt_at is not None
    # The class name only — an exception string can carry the full URL.
    assert delivery.last_error == "ReadTimeout"


def test_429_is_retried(db, fake_http):
    client = make_client(db, webhook_url="https://example.test/hook",
                         webhook_secret=SECRET)
    delivery = webhooks.enqueue(db, client, "analysis.completed", "req_429", {})
    fake_http.behaviour = 429

    asyncio.run(webhooks.deliver(db, delivery))
    assert delivery.next_attempt_at is not None       # the one retryable 4xx


@pytest.mark.parametrize("status", [400, 404, 410, 422])
def test_other_4xx_is_not_retried(db, fake_http, status):
    client = make_client(db, webhook_url="https://example.test/hook",
                         webhook_secret=SECRET)
    delivery = webhooks.enqueue(db, client, "analysis.completed",
                                f"req_{status}", {})
    fake_http.behaviour = status

    asyncio.run(webhooks.deliver(db, delivery))

    assert delivery.delivered is False
    assert delivery.last_status_code == status
    # Nothing to gain from repeating a request the receiver has rejected.
    assert delivery.next_attempt_at is None


def test_attempts_are_capped(db, fake_http):
    client = make_client(db, webhook_url="https://example.test/hook",
                         webhook_secret=SECRET)
    delivery = webhooks.enqueue(db, client, "analysis.completed", "req_cap", {})
    fake_http.behaviour = 500

    for _ in range(webhooks.MAX_ATTEMPTS):
        asyncio.run(webhooks.deliver(db, delivery))

    assert delivery.attempts == webhooks.MAX_ATTEMPTS
    # Given up: nothing will pick it up again, and the row records why.
    assert delivery.next_attempt_at is None
    assert delivery.delivered is False
    assert delivery.event_id not in (delivery.last_error or "")


def test_due_deliveries_excludes_delivered_and_exhausted(db, fake_http):
    client = make_client(db, webhook_url="https://example.test/hook",
                         webhook_secret=SECRET)
    done = webhooks.enqueue(db, client, "analysis.completed", "req_done", {})
    asyncio.run(webhooks.deliver(db, done))            # 200 -> delivered

    fake_http.behaviour = 500
    pending = webhooks.enqueue(db, client, "analysis.completed", "req_pending", {})
    asyncio.run(webhooks.deliver(db, pending))
    # Backdate the retry so it is due now.
    pending.next_attempt_at = datetime.datetime.now(UTC) - datetime.timedelta(seconds=1)
    db.commit()

    due_ids = {d.event_id for d in webhooks.due_deliveries(db)}
    assert pending.event_id in due_ids
    assert done.event_id not in due_ids
