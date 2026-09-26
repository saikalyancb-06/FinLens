"""API-key authentication for the B2B API.

SECRET HANDLING — READ THIS BEFORE EDITING
------------------------------------------
The full key secret exists in exactly two places for exactly two moments: in the
response body of the call that created it, and in the ``Authorization`` header
of every request that uses it. It is never written to the database, never put in
a log line, and never placed in an exception message.

Concretely, in this module:

* :func:`generate_api_key` is the only function that produces a secret, and it
  returns it to its caller rather than storing it.
* Only ``sha256(secret)`` reaches the ``api_keys`` table. A dump of that table
  is an inventory of hashes, not a set of working credentials.
* Every object that transports a secret (:class:`IssuedKey`) overrides
  ``__repr__``/``__str__`` to redact it, because the realistic way a secret
  leaks is not a deliberate ``print`` — it is a ``logger.exception`` that
  formats a local variable, a traceback rendered by an error tracker, or a
  ``repr()`` in a debugger transcript pasted into a ticket.
* Nothing here interpolates a caller-supplied token into a log message, not even
  a prefix of it. The stored ``key_prefix`` is the identifier for humans; it is
  designed to be safe to log and is the *only* key material that ever should be.

WHY SHA-256 AND NOT ARGON2
--------------------------
User passwords in this codebase go through argon2 (``app/utils/security.py``)
because they are low-entropy and human-chosen, so an offline attacker guessing
them must be slowed down. An API secret here is 256 bits from
``secrets.token_urlsafe``; there is nothing to guess and no dictionary to try, so
a slow KDF would buy no security and would put ~100ms of CPU on the front of
every single API call. A single SHA-256 is the right hash for high-entropy
tokens — the same reasoning ``hash_token`` in ``app/utils/security.py`` already
applies to refresh tokens.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from typing import List, NamedTuple, Optional, Sequence, Tuple

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.b2b import errors as err
from app.b2b.errors import ApiError
from app.b2b.models import ApiClient, ApiKey
from app.database.session import SessionLocal, get_db

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Shape of a secret
# --------------------------------------------------------------------------- #

#: Environment marker. A key is visibly a Kredo live key at a glance, which is
#: what lets a secret scanner (GitHub's, or our own pre-commit hook) recognise
#: one that has been committed by accident. An opaque random string would be
#: unrecognisable and would sit in a repository undetected.
SECRET_NAMESPACE = "kl_live_"

#: ``secrets.token_urlsafe(32)`` is 32 random bytes rendered base64url without
#: padding — 43 characters, 256 bits of entropy. Brute force is not a threat
#: model at that size, which is what justifies the cheap hash above.
SECRET_ENTROPY_BYTES = 32
SECRET_TOKEN_CHARS = 43
SECRET_LENGTH = len(SECRET_NAMESPACE) + SECRET_TOKEN_CHARS  # 51

#: How much of the secret is stored in clear for identification. Sixteen
#: characters is the namespace plus eight characters of the token: enough for a
#: human to tell two keys apart in a list, far too little to be guessable —
#: 35 characters (~208 bits) of the secret remain unknown.
KEY_PREFIX_LENGTH = 16

DEFAULT_SCOPES = "analyze:write,analyze:read"


def hash_secret(secret: str) -> str:
    """SHA-256 hex digest of a secret — the only form that is ever persisted."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


#: A syntactically valid hash of a value nobody holds, used as the comparison
#: partner when no key row matched. See :func:`verify_api_key` for why.
_DUMMY_HASH = hashlib.sha256(b"kredo-b2b-no-such-key").hexdigest()


def generate_api_key() -> Tuple[str, str, str]:
    """Mint a new secret.

    Returns ``(full_secret, prefix, sha256_hash)``. The caller must persist only
    the second and third values.
    """
    token = secrets.token_urlsafe(SECRET_ENTROPY_BYTES)
    secret = f"{SECRET_NAMESPACE}{token}"
    return secret, secret[:KEY_PREFIX_LENGTH], hash_secret(secret)


class IssuedKey(NamedTuple):
    """``(ApiKey, full_secret)`` — with a repr that cannot leak the secret.

    This unpacks exactly like the tuple it is (``key, secret = issue_key(...)``),
    but formatting it — which is what ``logger.info("issued %s", result)`` and a
    traceback renderer both do — yields a redacted line instead of the
    credential.
    """

    api_key: ApiKey
    secret: str

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return (f"IssuedKey(api_key_id={getattr(self.api_key, 'id', None)!s}, "
                f"key_prefix={getattr(self.api_key, 'key_prefix', None)!s}, "
                f"secret='***redacted***')")

    __str__ = __repr__


# --------------------------------------------------------------------------- #
# Auth context
# --------------------------------------------------------------------------- #

@dataclass
class AuthContext:
    """Who is calling. Carries no secret — deliberately.

    The verified credential is represented by its database row and its prefix.
    Once :func:`verify_api_key` has returned, the raw secret is out of scope and
    nothing downstream can log it even by accident.
    """

    client: ApiClient
    api_key: ApiKey
    scopes: List[str] = field(default_factory=list)
    client_ip: Optional[str] = None
    #: Handle on the background last-used write, so a test can wait for it.
    #: Never awaited on the request path.
    touch_handle: Optional[threading.Thread] = None

    @property
    def client_id(self) -> uuid.UUID:
        return self.client.id

    @property
    def api_key_id(self) -> uuid.UUID:
        return self.api_key.id

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes

    def __repr__(self) -> str:
        return (f"AuthContext(client={getattr(self.client, 'slug', None)!s}, "
                f"key_prefix={getattr(self.api_key, 'key_prefix', None)!s}, "
                f"scopes={self.scopes!r})")

    __str__ = __repr__


def _parse_scopes(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


# --------------------------------------------------------------------------- #
# Client and key lifecycle
# --------------------------------------------------------------------------- #

def create_client(
    db: Session,
    name: str,
    slug: str,
    *,
    contact_email: Optional[str] = None,
    plan: str = "free",
    rate_limit_per_minute: Optional[int] = None,
    rate_limit_per_day: Optional[int] = None,
    rate_limit_per_month: Optional[int] = None,
    max_file_size_bytes: Optional[int] = None,
    result_retention_hours: Optional[int] = None,
    webhook_url: Optional[str] = None,
    webhook_secret: Optional[str] = None,
    is_active: bool = True,
) -> ApiClient:
    """Create an integrating company. Limits left as ``None`` take the column
    defaults, so a plan change is one column update rather than a migration."""
    client = ApiClient(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        contact_email=contact_email,
        plan=plan,
        is_active=is_active,
    )
    if rate_limit_per_minute is not None:
        client.rate_limit_per_minute = rate_limit_per_minute
    if rate_limit_per_day is not None:
        client.rate_limit_per_day = rate_limit_per_day
    if rate_limit_per_month is not None:
        client.rate_limit_per_month = rate_limit_per_month
    if max_file_size_bytes is not None:
        client.max_file_size_bytes = max_file_size_bytes
    if result_retention_hours is not None:
        client.result_retention_hours = result_retention_hours
    if webhook_url is not None:
        client.webhook_url = webhook_url
    if webhook_secret is not None:
        client.webhook_secret = webhook_secret

    db.add(client)
    db.commit()
    db.refresh(client)
    return client


def issue_key(
    db: Session,
    client: ApiClient,
    name: Optional[str] = None,
    scopes: str = DEFAULT_SCOPES,
    expires_at: Optional[datetime.datetime] = None,
    *,
    rotated_from_id: Optional[uuid.UUID] = None,
) -> IssuedKey:
    """Mint a key for ``client``.

    The returned secret is the ONLY copy that will ever exist. If the caller
    loses it the key must be rotated; there is no recovery path, and that is the
    property that makes the stored hash worth having.
    """
    secret, prefix, digest = generate_api_key()
    key = ApiKey(
        id=uuid.uuid4(),
        client_id=client.id,
        name=name,
        key_prefix=prefix,
        key_hash=digest,
        scopes=scopes or DEFAULT_SCOPES,
        is_active=True,
        expires_at=expires_at,
        rotated_from_id=rotated_from_id,
        use_count=0,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    # Logged by prefix only. The prefix is stored in clear precisely so that
    # this line can exist without being a leak.
    logger.info("[b2b-auth] issued key %s for client %s", prefix, client.slug)
    return IssuedKey(key, secret)


def revoke_key(db: Session, key_id: uuid.UUID) -> ApiKey:
    """Revoke immediately and irreversibly.

    Revocation is a stamp rather than a delete: an audit trail that says "this
    credential existed and was withdrawn at 14:02" is worth more than a clean
    table, and the row is what links a past request to the key that made it.
    """
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if key is None:
        raise ApiError(err.REQUEST_NOT_FOUND, "No such API key.")
    if key.revoked_at is None:
        key.revoked_at = datetime.datetime.now(datetime.timezone.utc)
    key.is_active = False
    db.commit()
    db.refresh(key)
    logger.info("[b2b-auth] revoked key %s", key.key_prefix)
    return key


def rotate_key(db: Session, key_id: uuid.UUID) -> IssuedKey:
    """Issue a replacement for ``key_id`` and link the two.

    GRACE WINDOW — DELIBERATE, AND THE WHOLE POINT OF ROTATION
    ---------------------------------------------------------
    The old key is NOT revoked here. It stays valid until somebody calls
    :func:`revoke_key` on it explicitly.

    A rotation that invalidated the old secret at the instant the new one was
    minted would be an outage, not a rotation: the integrator's fleet is still
    running with the old value in its environment, and every request in flight
    (plus every process not yet redeployed) would start returning 401 the moment
    the button was pressed. Two valid keys for a bounded window is what lets an
    integrator roll the new secret out, watch ``last_used_at`` on the old key
    stop moving, and only then revoke it.

    The cost of the window is that a *compromised* key stays usable during it —
    which is why the compromise path is :func:`revoke_key` (immediate) and not
    this function. Rotation is for hygiene; revocation is for incidents.
    """
    old = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if old is None:
        raise ApiError(err.REQUEST_NOT_FOUND, "No such API key.")
    client = db.query(ApiClient).filter(ApiClient.id == old.client_id).first()
    if client is None:
        raise ApiError(err.REQUEST_NOT_FOUND, "No such API client.")

    name = f"{old.name} (rotated)" if old.name else "rotated"
    return issue_key(
        db, client,
        name=name,
        scopes=old.scopes,
        expires_at=old.expires_at,
        rotated_from_id=old.id,
    )


# --------------------------------------------------------------------------- #
# Last-used tracking — best effort, off the request path
# --------------------------------------------------------------------------- #

#: Set to False in a test that wants the write to happen inline.
TOUCH_IN_BACKGROUND = True


def _touch_key_now(key_id: uuid.UUID, ip: Optional[str]) -> None:
    """Write last-used telemetry in its own session, swallowing every failure.

    Its own session because it must not join, and therefore must not be able to
    dirty or roll back, the transaction the request is using. Swallowing every
    failure because this is telemetry: a client whose perfectly valid key works
    but whose ``use_count`` did not increment has lost nothing, whereas a client
    whose request 500s because a counter update deadlocked has lost everything.
    """
    session = None
    try:
        session = SessionLocal()
        now = datetime.datetime.now(datetime.timezone.utc)
        # An UPDATE with a server-side increment rather than read-modify-write:
        # concurrent requests on the same key would otherwise lose counts, and
        # this way there is no row read to go stale between the two.
        session.query(ApiKey).filter(ApiKey.id == key_id).update(
            {
                ApiKey.last_used_at: now,
                ApiKey.last_used_ip: (ip or None),
                ApiKey.use_count: ApiKey.use_count + 1,
            },
            synchronize_session=False,
        )
        session.commit()
    except Exception:
        logger.debug("[b2b-auth] last-used update failed (ignored)", exc_info=True)
        try:
            if session is not None:
                session.rollback()
        except Exception:
            pass
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass


def _schedule_touch(key_id: uuid.UUID, ip: Optional[str]) -> Optional[threading.Thread]:
    """Hand the telemetry write to a daemon thread and return immediately."""
    if not TOUCH_IN_BACKGROUND:
        _touch_key_now(key_id, ip)
        return None
    try:
        thread = threading.Thread(
            target=_touch_key_now, args=(key_id, ip),
            name="b2b-key-touch", daemon=True,
        )
        thread.start()
        return thread
    except Exception:
        # Even failing to *start* the thread must not fail the request.
        logger.debug("[b2b-auth] could not schedule last-used update", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

def _extract_bearer(authorization_header: Optional[str]) -> str:
    """Pull the token out of an ``Authorization`` header, or raise.

    A malformed or missing header is MISSING_API_KEY, never INVALID_API_KEY: the
    caller has not presented a credential at all, and telling them "invalid"
    would send an integrator hunting for a wrong secret when the real problem is
    that their HTTP client dropped the header.
    """
    if authorization_header is None or not authorization_header.strip():
        raise ApiError(
            err.MISSING_API_KEY,
            "Authorization header is required. Send 'Authorization: Bearer <api key>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw = authorization_header.strip()
    parts = raw.split(None, 1)
    # The scheme is compared case-insensitively (RFC 7235 says it is
    # case-insensitive); the token is not touched.
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise ApiError(
            err.MISSING_API_KEY,
            "Authorization header must use the Bearer scheme: 'Authorization: Bearer <api key>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = parts[1].strip()
    if not token:
        raise ApiError(
            err.MISSING_API_KEY,
            "Authorization header carried an empty bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def verify_api_key(
    db: Session,
    authorization_header: Optional[str],
    *,
    client_ip: Optional[str] = None,
) -> AuthContext:
    """Authenticate a bearer token and return the calling context.

    TIMING
    ------
    The lookup is by ``key_hash``, never by the secret and never by a LIKE on
    the prefix, so the database only ever sees a digest.

    The final comparison goes through :func:`secrets.compare_digest` even though
    the row was fetched by equality on that same column and therefore *must*
    match. The reason is the branch that does not match: when no row is found we
    still run a compare against a fixed dummy digest, so the "unknown key" and
    "known key" paths execute the same comparison and take the same time. Doing
    it only on the found path would hand an attacker a way to distinguish "this
    prefix exists" from "it does not" by timing alone, which is exactly the
    oracle that turns key enumeration from impossible into merely slow.
    """
    token = _extract_bearer(authorization_header)
    digest = hash_secret(token)

    key = db.query(ApiKey).filter(ApiKey.key_hash == digest).first()

    # Same work on both branches. `compare_digest` on equal-length hex strings
    # does not short-circuit on the first differing character the way `==` does.
    if key is None:
        secrets.compare_digest(digest, _DUMMY_HASH)
        raise ApiError(
            err.INVALID_API_KEY,
            "The API key presented is not valid.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(str(key.key_hash), digest):  # pragma: no cover
        raise ApiError(
            err.INVALID_API_KEY,
            "The API key presented is not valid.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Order matters for the message the integrator reads. Revocation is checked
    # before expiry because a revoked key that has also expired is, operationally,
    # a revoked key — "it expired, mint another" would be the wrong instruction
    # for a credential that was withdrawn on purpose.
    if key.revoked_at is not None or not key.is_active:
        raise ApiError(
            err.REVOKED_API_KEY,
            "This API key has been revoked.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if key.expires_at is not None:
        expires_at = key.expires_at
        if expires_at.tzinfo is None:
            # Postgres hands back an aware value for timestamptz, but a row
            # written by a naive datetime in a test would not be; normalise
            # rather than raise a TypeError inside an auth check.
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        if expires_at <= datetime.datetime.now(datetime.timezone.utc):
            raise ApiError(
                err.EXPIRED_API_KEY,
                "This API key expired on "
                f"{expires_at.isoformat()}. Issue a new key.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    client = db.query(ApiClient).filter(ApiClient.id == key.client_id).first()
    if client is None:
        # Orphaned key: the FK cascade should make this impossible. Report it as
        # invalid rather than leaking that the key itself was fine.
        raise ApiError(
            err.INVALID_API_KEY,
            "The API key presented is not valid.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not client.is_active:
        # 403, not 401: the credential is genuine, the account is switched off.
        # Retrying with a different key will not help and the message says so.
        raise ApiError(
            err.CLIENT_DISABLED,
            "This account is disabled. Contact support.",
        )

    ctx = AuthContext(
        client=client,
        api_key=key,
        scopes=_parse_scopes(key.scopes),
        client_ip=client_ip,
    )
    ctx.touch_handle = _schedule_touch(key.id, client_ip)
    return ctx


def require_scope(ctx: AuthContext, scope: str) -> None:
    """Raise INSUFFICIENT_SCOPE unless ``ctx`` carries ``scope``.

    403 and not 404: hiding the existence of an endpoint from an authenticated
    client that merely lacks a grant makes integration debugging guesswork, and
    the endpoint list is public documentation anyway.
    """
    if not ctx.has_scope(scope):
        raise ApiError(
            err.INSUFFICIENT_SCOPE,
            f"This API key is not permitted to '{scope}'.",
            detail={"required_scope": scope, "granted_scopes": list(ctx.scopes)},
        )


def require_scopes(ctx: AuthContext, scopes: Sequence[str]) -> None:
    for scope in scopes:
        require_scope(ctx, scope)


# --------------------------------------------------------------------------- #
# FastAPI wiring
# --------------------------------------------------------------------------- #

def _request_ip(request: Request) -> Optional[str]:
    """Client IP, trusting ``X-Forwarded-For`` only behind a declared proxy.

    Same rule as ``main.py``: the header is caller-controlled, so honouring it
    unconditionally would let anyone write whatever they liked into our audit
    trail of who used a key.
    """
    try:
        from app.config import settings
        if getattr(settings, "TRUST_PROXY_HEADERS", False):
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()[:64]
    except Exception:
        pass
    if request.client and request.client.host:
        return request.client.host[:64]
    return None


def api_key_auth(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    """FastAPI dependency: ``ctx: AuthContext = Depends(api_key_auth)``."""
    ctx = verify_api_key(
        db,
        request.headers.get("authorization"),
        client_ip=_request_ip(request),
    )
    # Handy for the error handler, which wants the client on a request that
    # failed after authentication.
    try:
        request.state.b2b_auth = ctx
    except Exception:  # pragma: no cover - Starlette always provides state
        pass
    return ctx


__all__ = [
    "AuthContext", "IssuedKey", "DEFAULT_SCOPES", "SECRET_NAMESPACE",
    "KEY_PREFIX_LENGTH", "SECRET_LENGTH",
    "generate_api_key", "hash_secret", "create_client", "issue_key",
    "revoke_key", "rotate_key", "verify_api_key", "require_scope",
    "require_scopes", "api_key_auth",
]
