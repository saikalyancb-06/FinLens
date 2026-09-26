"""Internal administration for the B2B API.

This router mints credentials. That makes it the highest-value surface in the
whole service — a caller who reaches it can issue themselves a key for any
client — so it is deliberately the least convenient thing here:

* It is guarded by a single shared token, ``B2B_ADMIN_TOKEN``, read from the
  environment at CALL time.
* **If that variable is not set, every endpoint refuses.** There is no default
  token, no "development mode" bypass and no fallback to open. The failure mode
  of a missing secret must be "nobody can administer the API", never "anybody
  can": the first is noticed within minutes of a deploy, the second is noticed
  when somebody else's keys turn up in a breach.
* It should not be exposed publicly at all. Mount it behind whatever network
  control the deployment has — the token is the second lock, not the first.

The token is compared with ``secrets.compare_digest`` for the same reason the
API key hashes are: ``==`` leaks its answer through timing, and a shared secret
that is guessable one byte at a time is not a shared secret.

Key secrets appear in exactly two responses — create and rotate — and both say
so in the payload. Nothing here can read a secret back, because nothing stored
can be turned back into one.
"""
from __future__ import annotations

import datetime
import os
import secrets
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.b2b import auth as b2b_auth
from app.b2b import errors as err
from app.b2b import metering
from app.b2b.errors import ApiError
from app.b2b.models import ApiClient, ApiKey
from app.database.session import get_db

router = APIRouter(prefix="/internal/clients", tags=["b2b-admin"])

ADMIN_TOKEN_ENV = "B2B_ADMIN_TOKEN"

#: A token shorter than this is refused outright. A four-character "admin token"
#: set as a placeholder during a deploy is functionally the same as no token,
#: and it would pass a naive truthiness check.
MIN_ADMIN_TOKEN_LENGTH = 16


def _configured_admin_token() -> Optional[str]:
    """Read at call time, never cached.

    At call time so that rotating the token is a restart-free operation and so a
    test can set it without having imported this module in a particular order.
    Settings first, environment second — the settings object is where the rest
    of the application looks, and the env var is the deployment's way in when
    ``app/config.py`` has no field for it.
    """
    value = None
    try:
        from app.config import settings
        value = getattr(settings, ADMIN_TOKEN_ENV, None)
    except Exception:
        value = None
    if not value:
        value = os.getenv(ADMIN_TOKEN_ENV)
    value = (value or "").strip()
    return value or None


def require_admin(x_admin_token: Optional[str] = Header(default=None,
                                                        alias="X-Admin-Token")) -> str:
    """Dependency guarding every endpoint in this router."""
    configured = _configured_admin_token()

    if configured is None or len(configured) < MIN_ADMIN_TOKEN_LENGTH:
        # CLOSED, not open. 503 rather than 401 because the caller has done
        # nothing wrong and no credential they could present would help — the
        # server is misconfigured and an operator has to fix it.
        raise ApiError(
            err.SERVICE_UNAVAILABLE,
            "The administration API is not configured on this deployment.",
        )

    if not x_admin_token or not x_admin_token.strip():
        raise ApiError(err.MISSING_API_KEY,
                       "X-Admin-Token header is required.")

    if not secrets.compare_digest(x_admin_token.strip(), configured):
        raise ApiError(err.INVALID_API_KEY, "Administration token is not valid.")

    return configured


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class CreateClientBody(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str = Field(min_length=1, max_length=80)
    contact_email: Optional[str] = Field(default=None, max_length=255)
    plan: str = "free"
    rate_limit_per_minute: Optional[int] = Field(default=None, ge=1)
    rate_limit_per_day: Optional[int] = Field(default=None, ge=1)
    rate_limit_per_month: Optional[int] = Field(default=None, ge=1)
    max_file_size_bytes: Optional[int] = Field(default=None, ge=1)
    result_retention_hours: Optional[int] = Field(default=None, ge=0)
    webhook_url: Optional[str] = Field(default=None, max_length=500)
    webhook_secret: Optional[str] = Field(default=None, max_length=128)


class IssueKeyBody(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    scopes: str = b2b_auth.DEFAULT_SCOPES
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


SECRET_NOTICE = ("This is the only time this secret is shown. It is stored as a "
                 "SHA-256 hash and cannot be recovered — if it is lost, rotate "
                 "the key.")


def _client_json(client: ApiClient) -> Dict[str, Any]:
    return {
        "id": str(client.id),
        "name": client.name,
        "slug": client.slug,
        "contact_email": client.contact_email,
        "is_active": client.is_active,
        "plan": client.plan,
        "rate_limit_per_minute": client.rate_limit_per_minute,
        "rate_limit_per_day": client.rate_limit_per_day,
        "rate_limit_per_month": client.rate_limit_per_month,
        "max_file_size_bytes": client.max_file_size_bytes,
        "result_retention_hours": client.result_retention_hours,
        "webhook_url": client.webhook_url,
        # Whether a webhook secret exists, never what it is.
        "webhook_secret_set": bool(client.webhook_secret),
        "created_at": _iso(client.created_at),
    }


def _key_json(key: ApiKey) -> Dict[str, Any]:
    """A key, described by its prefix. There is no field here for the secret."""
    return {
        "id": str(key.id),
        "name": key.name,
        "key_prefix": key.key_prefix,
        "scopes": key.scopes,
        "is_active": key.is_active,
        "revoked_at": _iso(key.revoked_at),
        "expires_at": _iso(key.expires_at),
        "rotated_from_id": str(key.rotated_from_id) if key.rotated_from_id else None,
        "last_used_at": _iso(key.last_used_at),
        "last_used_ip": key.last_used_ip,
        "use_count": key.use_count,
        "created_at": _iso(key.created_at),
    }


def _iso(value: Optional[datetime.datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _get_client(db: Session, slug_or_id: str) -> ApiClient:
    """Resolve by slug or id, so a caller holding either can use these routes."""
    client = db.query(ApiClient).filter(ApiClient.slug == slug_or_id).first()
    if client is None:
        try:
            client = (db.query(ApiClient)
                        .filter(ApiClient.id == uuid.UUID(slug_or_id)).first())
        except (ValueError, AttributeError):
            client = None
    if client is None:
        raise ApiError(err.REQUEST_NOT_FOUND, f"No client '{slug_or_id}'.")
    return client


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #

@router.post("", status_code=201)
def create_client_endpoint(body: CreateClientBody,
                           _: str = Depends(require_admin),
                           db: Session = Depends(get_db)) -> Dict[str, Any]:
    existing = db.query(ApiClient).filter(ApiClient.slug == body.slug).first()
    if existing is not None:
        raise ApiError(err.RESOURCE_ALREADY_EXISTS,
                       f"A client with slug '{body.slug}' already exists.")
    client = b2b_auth.create_client(
        db, body.name, body.slug,
        contact_email=body.contact_email,
        plan=body.plan,
        rate_limit_per_minute=body.rate_limit_per_minute,
        rate_limit_per_day=body.rate_limit_per_day,
        rate_limit_per_month=body.rate_limit_per_month,
        max_file_size_bytes=body.max_file_size_bytes,
        result_retention_hours=body.result_retention_hours,
        webhook_url=body.webhook_url,
        webhook_secret=body.webhook_secret,
    )
    return {"client": _client_json(client)}


@router.get("")
def list_clients(_: str = Depends(require_admin),
                 db: Session = Depends(get_db)) -> Dict[str, Any]:
    clients = db.query(ApiClient).order_by(ApiClient.created_at.desc()).all()
    return {"clients": [_client_json(c) for c in clients]}


@router.get("/{slug}")
def get_client(slug: str, _: str = Depends(require_admin),
               db: Session = Depends(get_db)) -> Dict[str, Any]:
    return {"client": _client_json(_get_client(db, slug))}


@router.post("/{slug}/disable")
def disable_client(slug: str, _: str = Depends(require_admin),
                   db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Switch a client off without deleting anything.

    Every one of its keys stops working immediately (CLIENT_DISABLED), and the
    usage history stays intact — which is what a billing dispute needs.
    """
    client = _get_client(db, slug)
    client.is_active = False
    db.commit()
    db.refresh(client)
    return {"client": _client_json(client)}


@router.post("/{slug}/enable")
def enable_client(slug: str, _: str = Depends(require_admin),
                  db: Session = Depends(get_db)) -> Dict[str, Any]:
    client = _get_client(db, slug)
    client.is_active = True
    db.commit()
    db.refresh(client)
    return {"client": _client_json(client)}


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #

@router.post("/{slug}/keys", status_code=201)
def issue_key_endpoint(slug: str, body: IssueKeyBody,
                       _: str = Depends(require_admin),
                       db: Session = Depends(get_db)) -> Dict[str, Any]:
    client = _get_client(db, slug)
    expires_at = None
    if body.expires_in_days:
        expires_at = (datetime.datetime.now(datetime.timezone.utc)
                      + datetime.timedelta(days=body.expires_in_days))
    key, secret = b2b_auth.issue_key(db, client, name=body.name,
                                     scopes=body.scopes, expires_at=expires_at)
    return {
        "key": _key_json(key),
        "secret": secret,
        "secret_notice": SECRET_NOTICE,
    }


@router.get("/{slug}/keys")
def list_keys(slug: str, _: str = Depends(require_admin),
              db: Session = Depends(get_db)) -> Dict[str, Any]:
    client = _get_client(db, slug)
    keys = (db.query(ApiKey).filter(ApiKey.client_id == client.id)
              .order_by(ApiKey.created_at.desc()).all())
    return {"client_id": str(client.id), "keys": [_key_json(k) for k in keys]}


@router.post("/{slug}/keys/{key_id}/revoke")
def revoke_key_endpoint(slug: str, key_id: uuid.UUID,
                        _: str = Depends(require_admin),
                        db: Session = Depends(get_db)) -> Dict[str, Any]:
    client = _get_client(db, slug)
    key = (db.query(ApiKey)
             .filter(ApiKey.id == key_id, ApiKey.client_id == client.id).first())
    if key is None:
        # Scoped to the client in the path, so an admin cannot revoke another
        # client's key by pasting the wrong id into the wrong URL.
        raise ApiError(err.REQUEST_NOT_FOUND, "No such key for this client.")
    return {"key": _key_json(b2b_auth.revoke_key(db, key.id))}


@router.post("/{slug}/keys/{key_id}/rotate")
def rotate_key_endpoint(slug: str, key_id: uuid.UUID,
                        _: str = Depends(require_admin),
                        db: Session = Depends(get_db)) -> Dict[str, Any]:
    client = _get_client(db, slug)
    key = (db.query(ApiKey)
             .filter(ApiKey.id == key_id, ApiKey.client_id == client.id).first())
    if key is None:
        raise ApiError(err.REQUEST_NOT_FOUND, "No such key for this client.")
    new_key, secret = b2b_auth.rotate_key(db, key.id)
    return {
        "key": _key_json(new_key),
        "secret": secret,
        "secret_notice": SECRET_NOTICE,
        # Stated in the response because the grace window is the one part of
        # rotation an operator can get wrong by forgetting it exists.
        "rotation_notice": (
            f"The previous key ({key.key_prefix}) is STILL VALID. Deploy this "
            "secret, confirm the old key has stopped being used, then revoke it "
            f"at POST /internal/clients/{client.slug}/keys/{key.id}/revoke."
        ),
        "previous_key_id": str(key.id),
    }


# --------------------------------------------------------------------------- #
# Usage
# --------------------------------------------------------------------------- #

@router.get("/{slug}/usage")
def client_usage(slug: str,
                 since: Optional[datetime.datetime] = Query(default=None),
                 until: Optional[datetime.datetime] = Query(default=None),
                 _: str = Depends(require_admin),
                 db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Usage for a client. Defaults to the last 30 days."""
    client = _get_client(db, slug)
    now = datetime.datetime.now(datetime.timezone.utc)
    until = until or now
    since = since or (until - datetime.timedelta(days=30))
    return {
        "client": {"id": str(client.id), "slug": client.slug},
        "usage": metering.usage_summary(db, client.id, since, until),
    }


__all__ = ["router", "require_admin", "ADMIN_TOKEN_ENV"]
