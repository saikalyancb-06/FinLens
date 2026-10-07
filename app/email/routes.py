"""Mailbox connection and statement-discovery API.

Endpoint groups
---------------
``/email/providers``            what can be connected, and how
``/email/connections*``         connect, list, reconnect, disconnect (any provider)
``/email/oauth/*``              OAuth callbacks (Google, Microsoft)
``/email/scans*``               background scans with live progress
``/email/statements*``          discovered documents, account mapping, import

Authorisation rule, without exception: **every** query that touches a mailbox,
a discovered document, a scan or a credential is filtered on
``current_user.id``. No endpoint accepts a user id from the client, and an id
that belongs to another user returns 404 rather than 403, so the API does not
confirm the existence of other people's records.

The endpoints that existed before the multi-provider work — ``/email/connect``,
``/email/scan``, ``/email/import/{id}`` and the rest — are preserved with their
original request and response shapes so nothing that already calls them breaks.
"""
from __future__ import annotations

import datetime
import html
import logging
import os
import secrets
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database.session import get_db
from app.email.models import (
    CONNECTION_CONNECTED,
    CONNECTION_DISCONNECTED,
    ConnectedAccount,
    EmailAttachment,
    MailboxScan,
)
from app.email.utils import encrypt_token
from app.mailbox.errors import MailboxError, ProviderNotSupported
from app.mailbox.imap import suggest_imap_host
from app.mailbox.oauth import get_oauth_client
from app.mailbox.registry import PROVIDER_CATALOG, normalise_provider
from app.models.oauth_state import OAuthState
from app.models.user import User
from app.statements.ingest import ingest_attachment
from app.statements.scan_runner import create_scan, execute_scan, start_scan_in_background

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email", tags=["Email Statement Import"])

OAUTH_STATE_TTL_MINUTES = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_uuid(value: Any, field: str = "id") -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid {field} format.")


def _format_dt(value) -> Optional[str]:
    if not value:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _user_connections(db: Session, user: User, *, active_only: bool = True) -> List[ConnectedAccount]:
    query = db.query(ConnectedAccount).filter(ConnectedAccount.user_id == user.id)
    if active_only:
        query = query.filter(ConnectedAccount.is_active.is_(True))
    return query.order_by(ConnectedAccount.created_at.asc()).all()


def _owned_connection(db: Session, user: User, connection_id: Any) -> ConnectedAccount:
    """Fetch a connection *by owner and id together*.

    Written as one query on purpose. Fetching by id and then comparing owners is
    the shape that grows an early-return bug; this cannot be got wrong.
    """
    connection = db.query(ConnectedAccount).filter(
        ConnectedAccount.id == _as_uuid(connection_id, "connection_id"),
        ConnectedAccount.user_id == user.id,
    ).first()
    if connection is None:
        raise HTTPException(status_code=404, detail="Email connection not found.")
    return connection


def _owned_attachment(db: Session, user: User, attachment_id: Any) -> EmailAttachment:
    attachment = db.query(EmailAttachment).filter(
        EmailAttachment.id == _as_uuid(attachment_id, "attachment_id"),
        EmailAttachment.user_id == user.id,
    ).first()
    if attachment is None:
        raise HTTPException(status_code=404, detail="Email attachment record not found.")
    return attachment


def _callback_uri(request: Request, path: str, fallback: str, env_name: Optional[str] = None) -> str:
    """The redirect URI for one sign-in, identical for both legs of the flow.

    OAuth requires byte-identical redirect URIs between the authorization
    request and the token exchange, so both are derived here the same way.

    * When ``GOOGLE_REDIRECT_URI`` / ``MICROSOFT_REDIRECT_URI`` is set it wins:
      it is the address registered in the provider console, and a URI derived
      from the request (127.0.0.1 instead of localhost, a second domain) is
      rejected by Google as ``redirect_uri_mismatch``. The exception is a
      localhost value left over from development on a server reached by its
      public name, which can never work.
    * Otherwise it is built from the host serving the request, https when the
      proxy in front says so.
    """
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    configured = (os.getenv(env_name) or "").strip() if env_name else ""
    if configured:
        local_cfg = any(h in configured for h in ("://localhost", "://127.0.0.1"))
        local_req = (host or "").split(":")[0] in ("localhost", "127.0.0.1", "testserver")
        if not local_cfg or local_req:
            return configured
    if host:
        scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
        return f"{scheme}://{host}{path}"
    return fallback


def _redirect_env(provider_key: str) -> str:
    return "GOOGLE_REDIRECT_URI" if provider_key == "gmail" else "MICROSOFT_REDIRECT_URI"


def _issue_state(db: Session, user: User) -> str:
    now = datetime.datetime.utcnow()
    db.query(OAuthState).filter(OAuthState.expires_at < now).delete(synchronize_session=False)
    state = f"st_{secrets.token_urlsafe(32)}"
    db.add(OAuthState(
        user_id=user.id,
        state_value=state,
        expires_at=now + datetime.timedelta(minutes=OAUTH_STATE_TTL_MINUTES),
        consumed=False,
    ))
    db.commit()
    return state


def _consume_state(db: Session, state: Optional[str]) -> Optional[User]:
    found = _consume_state_record(db, state)
    return found[0] if found else None


def _consume_state_record(db: Session, state: Optional[str]):
    """Validate and burn an anti-CSRF state, returning the user who created it.

    Single use and server-side: the state is the only thing tying a callback
    from the user's browser back to an application account, so a replayed or
    guessed value must not resolve to anybody.
    """
    if not state:
        return None
    record = db.query(OAuthState).filter(
        OAuthState.state_value == state,
        OAuthState.consumed.is_(False),
        OAuthState.expires_at > datetime.datetime.utcnow(),
    ).first()
    if record is None:
        return None
    record.consumed = True
    db.commit()
    user = db.query(User).filter(User.id == record.user_id).first()
    return (user, record) if user else None


def _record_result(db: Session, record: Optional[OAuthState], *, status_: str, detail: str = "",
                   email: str = "", connection_id=None, scan_id=None) -> None:
    if record is None:
        return
    try:
        record.result_status = status_
        record.result_detail = (detail or "")[:1000] or None
        record.result_email = email or None
        record.connection_id = connection_id
        record.scan_id = scan_id
        # Keep it readable for the page that is waiting on it.
        record.expires_at = max(record.expires_at,
                                datetime.datetime.utcnow() + datetime.timedelta(minutes=10))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[Email OAuth] could not record the sign-in result")


def _render_result_html(title: str, message: str, *, ok: bool, email_address: str = "",
                        state: str = "") -> HTMLResponse:
    """The page the OAuth popup lands on. Escaped: every value here is external."""
    accent = "#16a34a" if ok else "#dc2626"
    background = "#f8fafc" if ok else "#fef2f2"
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    safe_email = html.escape(email_address)
    payload_type = "MAILBOX_CONNECTED" if ok else "MAILBOX_CONNECT_FAILED"
    safe_state = html.escape(state)
    import json as _json
    detail_js = _json.dumps(message).replace("<", "\\u003c")
    ok_js = "true" if ok else "false"
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{safe_title}</title></head>
<body style="font-family: system-ui, -apple-system, sans-serif; text-align:center; padding:40px; background:{background}; color:#0f172a;">
  <div style="background:#fff; max-width:460px; margin:0 auto; padding:28px; border-radius:12px; box-shadow:0 4px 12px rgba(0,0,0,.08); border:1px solid #e2e8f0;">
    <h2 style="color:{accent}; margin:0 0 10px 0; font-size:19px;">{safe_title}</h2>
    <p style="color:#475569; font-size:14px; margin:6px 0;">{safe_message}</p>
    <p style="color:#94a3b8; font-size:12px; margin-top:18px;">You can close this window, or <a href="/ingestion">return to Treasury Lens</a>.</p>
  </div>
  <script>
    (function () {{
      var msg = {{ type: '{payload_type}', email: '{safe_email}', state: '{safe_state}', detail: {detail_js} }};
      try {{
        if (window.opener && !window.opener.closed) {{
          window.opener.postMessage(msg, window.location.origin);
          // Kept for the previous front-end build, which listens for this name.
          if ({ok_js}) window.opener.postMessage({{ type: 'GMAIL_CONNECTED', email: '{safe_email}' }}, window.location.origin);
        }}
      }} catch (e) {{}}
      // Reaches the app tab even when the provider's page cut the popup off
      // from its opener (Cross-Origin-Opener-Policy).
      try {{ var bc = new BroadcastChannel('kredo-mailbox'); bc.postMessage(msg); bc.close(); }} catch (e) {{}}
      // Closed only on success: a failure stays on screen so it can be read.
      if ({ok_js}) setTimeout(function () {{ try {{ window.close(); }} catch (e) {{}} }}, 1500);
    }})();
  </script>
</body></html>""")


def _connection_payload(connection: ConnectedAccount) -> Dict[str, Any]:
    """Public view of a connection. Contains no credential material of any kind."""
    return {
        "id": str(connection.id),
        "provider": connection.provider,
        "provider_label": next(
            (p["label"] for p in PROVIDER_CATALOG
             if p["key"] == normalise_provider(connection.provider)),
            connection.provider,
        ),
        "email_address": connection.email_address,
        "display_name": connection.display_name,
        "auth_type": connection.auth_type,
        "status": connection.status or (CONNECTION_CONNECTED if connection.is_active
                                        else CONNECTION_DISCONNECTED),
        "status_detail": connection.status_detail,
        "is_active": bool(connection.is_active),
        "last_scan": _format_dt(connection.last_scan_at),
        "connected_at": _format_dt(connection.created_at),
        "imap_host": connection.imap_host,
    }


def _scan_payload(scan: MailboxScan) -> Dict[str, Any]:
    return {
        "id": str(scan.id),
        "scan_id": str(scan.id),
        "connection_id": str(scan.connection_id) if scan.connection_id else None,
        "provider": scan.provider,
        "email_address": scan.email_address,
        "status": scan.status,
        "stage": scan.stage,
        "progress_pct": scan.progress_pct,
        "messages_scanned": scan.messages_scanned,
        "candidates_found": scan.candidates_found,
        "documents_downloaded": scan.documents_downloaded,
        "statements_found": scan.statements_found,
        "duplicates_skipped": scan.duplicates_skipped,
        "transactions_extracted": scan.transactions_extracted,
        "failures": scan.failures,
        "error_reason": scan.error_reason,
        "error_detail": scan.error_detail,
        "started_at": _format_dt(scan.started_at),
        "finished_at": _format_dt(scan.finished_at),
    }


def _attachment_payload(attachment: EmailAttachment) -> Dict[str, Any]:
    extension = os.path.splitext(attachment.filename or "")[1].replace(".", "").upper()
    return {
        "id": str(attachment.id),
        "bank": attachment.institution_name or attachment.bank_name,
        "institution": attachment.institution_name or attachment.bank_name,
        "institution_confidence": attachment.institution_confidence,
        "document_type": attachment.document_type,
        "date": _format_dt(attachment.received_at) or "",
        "filename": attachment.filename,
        "size": attachment.file_size_bytes,
        "subject": attachment.subject,
        "sender": attachment.sender,
        "attachment_type": extension,
        "source_kind": attachment.source_kind or EmailAttachment.SOURCE_ATTACHMENT,
        "is_duplicate": bool(attachment.is_duplicate),
        "import_status": attachment.import_status,
        "classification": attachment.classification or "BANK_STATEMENT_CONFIRMED",
        "classification_reason": attachment.classification_reason or "",
        "detected_account_number": attachment.detected_account_number,
        "statement_period_start": attachment.statement_period_start.isoformat()
        if attachment.statement_period_start else None,
        "statement_period_end": attachment.statement_period_end.isoformat()
        if attachment.statement_period_end else None,
        "currency": attachment.currency,
        "connection_id": str(attachment.connected_account_id)
        if attachment.connected_account_id else None,
        "account_id": str(attachment.account_id) if attachment.account_id else None,
        "bank_account_id": str(attachment.account_id) if attachment.account_id else None,
    }


def _upsert_connection(
    db: Session,
    user: User,
    *,
    provider: str,
    email_address: str,
    provider_account_id: Optional[str],
    display_name: Optional[str],
    tokens: Dict[str, Any],
    scopes: str,
) -> ConnectedAccount:
    """Create or refresh the stored credential for one mailbox.

    Re-connecting the same mailbox updates the existing row rather than adding a
    second one, so a user who reconnects after a token expiry does not
    accumulate dead connections — and the documents already discovered stay
    attached to the connection they came from.
    """
    provider = normalise_provider(provider)
    connection = db.query(ConnectedAccount).filter(
        ConnectedAccount.user_id == user.id,
        ConnectedAccount.provider == provider,
        ConnectedAccount.email_address == email_address,
    ).first()

    if connection is None:
        connection = ConnectedAccount(user_id=user.id, provider=provider,
                                      email_address=email_address)
        db.add(connection)

    connection.provider_account_id = provider_account_id
    connection.display_name = display_name
    connection.auth_type = "oauth"
    connection.scopes = scopes
    if tokens.get("access_token"):
        connection.access_token = encrypt_token(tokens["access_token"])
    if tokens.get("refresh_token"):
        # Absent on a re-consent that Google chooses not to re-issue: keeping the
        # previous one is correct, wiping it would silently break the connection.
        connection.encrypted_refresh_token = encrypt_token(tokens["refresh_token"])
    connection.token_expires_at = datetime.datetime.utcnow() + datetime.timedelta(
        seconds=int(tokens.get("expires_in") or 3600)
    )
    connection.is_active = True
    connection.status = CONNECTION_CONNECTED
    connection.status_detail = None
    connection.last_error_at = None
    db.commit()
    db.refresh(connection)
    return connection


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

@router.get("/providers", summary="List connectable email providers")
def list_providers(current_user: User = Depends(get_current_user)):
    """What the connect screen should offer, and whether each is configured.

    An OAuth provider with no client credentials on the server is reported as
    unavailable rather than offered and then failing at the consent screen.
    """
    from app.config import settings

    out = []
    for entry in PROVIDER_CATALOG:
        item = dict(entry)
        if entry["auth"] == "oauth":
            try:
                client = get_oauth_client(entry["key"])
                configured = bool(client.is_configured) or settings.DEMO_MODE
                item["configured"] = configured
                # Outside production an unconfigured provider is still offered,
                # because the demo path makes it usable.
                item["available"] = configured or settings.ENVIRONMENT != "production"
            except ProviderNotSupported:
                item["configured"] = False
                item["available"] = False
        else:
            item["configured"] = True
            item["available"] = True
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

@router.get("/connections", summary="List this user's mailbox connections")
def list_connections(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return [_connection_payload(c) for c in _user_connections(db, current_user, active_only=False)]


@router.post("/connections/{provider}/authorize", summary="Start an OAuth connection")
def authorize_provider(
    provider: str,
    request: Request,
    login_hint: Optional[str] = Query(None, description="Address to open the sign-in page on"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the provider's consent URL for the *current* user.

    The client never receives a client secret or a token — only a URL it is
    expected to open. The state parameter is minted and stored server-side here
    and consumed in the callback.
    """
    key = normalise_provider(provider)
    try:
        client = get_oauth_client(key)
    except ProviderNotSupported as exc:
        raise HTTPException(status_code=400, detail=exc.detail)

    from app.config import settings
    env_prefix = "GOOGLE" if key == "gmail" else "MICROSOFT"
    configured = client.is_configured or settings.DEMO_MODE
    if not configured and settings.ENVIRONMENT == "production":
        # Refused only in production. In development the flow stays available so
        # the demo path (see the OAuth clients' demo handling) can be exercised
        # without real credentials; failing hard there would make the app
        # unrunnable out of the box.
        raise HTTPException(
            status_code=503,
            detail=(f"{key} sign-in is not configured on this server. Set "
                    f"{env_prefix}_CLIENT_ID and {env_prefix}_CLIENT_SECRET."),
        )

    callback_path = "/email/oauth/callback" if key == "gmail" else f"/email/oauth/{key}/callback"
    redirect_uri = _callback_uri(request, callback_path, client.default_redirect_uri, _redirect_env(key))
    state = _issue_state(db, current_user)
    logger.info("[Email OAuth] user %s starting %s connection", current_user.id, key)
    hint = (login_hint or "").strip() or None
    if hint and "@" not in hint:
        hint = None
    return {
        "authorization_url": client.authorization_url(state, redirect_uri=redirect_uri, login_hint=hint),
        "state": state,
        "provider": key,
        "redirect_uri": redirect_uri,
        "scopes": client.scopes,
        "is_demo": settings.DEMO_MODE,
        "configured": configured,
        "warning": None if configured else
        (f"{env_prefix}_CLIENT_ID / {env_prefix}_CLIENT_SECRET are not set, so this "
         "consent screen will not complete against the real provider."),
    }


class RouteEmailPayload(BaseModel):
    """Input payload for email-first router."""
    email_address: str = Field(..., min_length=3, max_length=255)


@router.post("/route", summary="Resolve email address to provider connector")
async def route_email_endpoint(
    payload: RouteEmailPayload,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Email-first router endpoint: determines the correct connector for any email address.

    Routes:
    1. Direct domain check:
       - gmail.com / googlemail.com -> Google OAuth
       - outlook.com / hotmail.com / live.com / msn.com -> Microsoft OAuth
    2. MX record lookup:
       - MX contains google.com -> Google OAuth
       - MX contains outlook.com / protection.outlook.com -> Microsoft OAuth
    3. Fallback:
       - Returns IMAP pre-fill settings
    """
    from app.mailbox.router import route_email

    try:
        route_res = await route_email(payload.email_address)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    provider_key = route_res["provider"]
    auth_type = route_res["auth_type"]

    response_data: Dict[str, Any] = {
        "email_address": route_res["email_address"],
        "domain": route_res["domain"],
        "provider": provider_key,
        "auth_type": auth_type,
        "matched_by": route_res.get("matched_by"),
        "provider_label": next(
            (p["label"] for p in PROVIDER_CATALOG if p["key"] == provider_key),
            provider_key.capitalize(),
        ),
    }

    if auth_type == "oauth":
        auth_data = authorize_provider(provider_key, request, login_hint=route_res["email_address"],
                                       current_user=current_user, db=db)
        response_data.update({
            "authorization_url": auth_data["authorization_url"],
            "state": auth_data["state"],
            "redirect_uri": auth_data["redirect_uri"],
            "scopes": auth_data["scopes"],
            "configured": auth_data["configured"],
            "is_demo": auth_data["is_demo"],
            "warning": auth_data.get("warning"),
        })
    elif auth_type == "app_password":
        response_data["imap_settings"] = route_res.get("imap_settings")
        response_data["alternatives"] = route_res.get("alternatives") or []

    return response_data


@router.post("/connect", summary="Initiate a mailbox connection (Gmail by default)")
def connect_mailbox(
    request: Request,
    provider: str = Query("gmail", description="gmail | microsoft"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Backwards-compatible entry point; defaults to Gmail as it always did."""
    return authorize_provider(provider, request, login_hint=None, current_user=current_user, db=db)


class ImapConnectPayload(BaseModel):
    """Everything needed to reach a standards-compliant IMAP mailbox.

    ``app_password`` is a provider-issued, application-specific credential
    (Yahoo, iCloud, Fastmail and Google all mint these on request) — not the
    account's login password. The distinction is the product's, not a
    formality: an app password is scoped, individually revocable, and useless
    for signing in to the account itself.
    """

    email_address: str = Field(..., min_length=3, max_length=255)
    app_password: str = Field(..., min_length=1, max_length=512)
    imap_host: Optional[str] = Field(None, max_length=255)
    imap_port: int = 993
    use_ssl: bool = True
    imap_username: Optional[str] = Field(None, max_length=255)
    mailbox: str = "INBOX"
    display_name: Optional[str] = Field(None, max_length=255)


@router.get("/imap/suggest", summary="Suggest IMAP settings for an address")
def imap_suggestion(
    email_address: str = Query(..., min_length=3),
    current_user: User = Depends(get_current_user),
):
    """Prefill hint for the connect form. Purely cosmetic; the user can override."""
    suggestion = suggest_imap_host(email_address)
    if suggestion:
        host, port = suggestion
        return {"known": True, "imap_host": host, "imap_port": port, "use_ssl": True}

    try:
        from app.mailbox.router import parse_email_domain
        domain = parse_email_domain(email_address)
        return {"known": False, "imap_host": f"mail.{domain}", "imap_port": 993, "use_ssl": True}
    except Exception:
        return {"known": False, "imap_host": None, "imap_port": 993, "use_ssl": True}


@router.post("/connections/imap", summary="Connect an IMAP mailbox")
async def connect_imap(
    payload: ImapConnectPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify the credentials by logging in once, then store them encrypted.

    The login happens before anything is written: a connection that is saved and
    only fails later, at scan time, is a worse experience than one that refuses
    to be created.
    """
    from app.mailbox.imap import ImapProvider

    host = (payload.imap_host or "").strip()
    port = int(payload.imap_port or 993)
    if not host:
        suggestion = suggest_imap_host(payload.email_address)
        if not suggestion:
            raise HTTPException(
                status_code=400,
                detail=("The IMAP server for this address is not known. Enter the IMAP "
                        "host and port from your provider's documentation."),
            )
        host, port = suggestion

    username = (payload.imap_username or payload.email_address).strip()
    provider = ImapProvider(
        host=host,
        port=port,
        use_ssl=bool(payload.use_ssl),
        username=username,
        password=payload.app_password,
        mailbox=payload.mailbox or "INBOX",
        email_address=payload.email_address.strip(),
    )
    try:
        await provider.connect()
        await provider.authenticate()
    except MailboxError as exc:
        raise HTTPException(status_code=400, detail=exc.detail)
    finally:
        try:
            await provider.disconnect()
        except Exception:
            pass

    connection = db.query(ConnectedAccount).filter(
        ConnectedAccount.user_id == current_user.id,
        ConnectedAccount.provider == "imap",
        ConnectedAccount.email_address == payload.email_address.strip(),
    ).first()
    if connection is None:
        connection = ConnectedAccount(
            user_id=current_user.id, provider="imap",
            email_address=payload.email_address.strip(),
        )
        db.add(connection)

    connection.auth_type = "app_password"
    connection.display_name = payload.display_name or username
    connection.provider_account_id = f"{host}:{username}"
    connection.imap_host = host
    connection.imap_port = port
    connection.imap_use_ssl = bool(payload.use_ssl)
    connection.imap_username = username
    connection.imap_mailbox = payload.mailbox or "INBOX"
    connection.encrypted_secret = encrypt_token(payload.app_password)
    connection.is_active = True
    connection.status = CONNECTION_CONNECTED
    connection.status_detail = None
    db.commit()
    db.refresh(connection)

    logger.info("[Email] user %s connected IMAP mailbox on %s", current_user.id, host)
    return {"status": "success", "connection": _connection_payload(connection)}


@router.delete("/connections/{connection_id}", summary="Disconnect one mailbox")
async def disconnect_connection(
    connection_id: str,
    purge_documents: bool = Query(True, description="Delete documents discovered from this mailbox"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke at the provider where possible, then destroy the stored credential.

    Deleting the credential is what actually ends this application's access; the
    revocation call is a courtesy that also invalidates the token at Google's
    end. It is attempted, and its failure never blocks the disconnect.
    """
    connection = _owned_connection(db, current_user, connection_id)
    await _revoke_quietly(db, connection)
    _clear_credentials(connection)

    removed = 0
    if purge_documents:
        removed = _purge_documents(db, current_user, connection_id=connection.id)

    db.commit()
    logger.info("[Email] user %s disconnected %s mailbox %s",
                current_user.id, connection.provider, connection.id)
    return {
        "status": "success",
        "message": f"Disconnected {connection.email_address}.",
        "documents_removed": removed,
    }


async def _revoke_quietly(db: Session, connection: ConnectedAccount) -> None:
    from app.mailbox.registry import open_provider

    try:
        provider = await open_provider(db, connection)
        try:
            await provider.revoke_access()
        finally:
            await provider.disconnect()
    except Exception as exc:
        logger.info("[Email] provider-side revocation skipped for %s (%s); the stored "
                    "credential is deleted regardless", connection.id, exc)


def _clear_credentials(connection: ConnectedAccount) -> None:
    connection.is_active = False
    connection.status = CONNECTION_DISCONNECTED
    connection.status_detail = None
    connection.access_token = None
    connection.encrypted_refresh_token = None
    connection.encrypted_secret = None
    connection.token_expires_at = None


def _purge_documents(db: Session, user: User, *, connection_id=None) -> int:
    """Delete discovered documents and the files behind them.

    Statements the user has already imported keep their transactions: those live
    in the ledger and are the user's data, not a cache of the mailbox.
    """
    query = db.query(EmailAttachment).filter(EmailAttachment.user_id == user.id)
    if connection_id is not None:
        query = query.filter(EmailAttachment.connected_account_id == connection_id)
    rows = query.all()
    for row in rows:
        if row.local_path and os.path.exists(row.local_path):
            try:
                os.remove(row.local_path)
            except OSError:
                logger.info("[Email] could not delete %s from disk", row.local_path)
        db.delete(row)
    return len(rows)


@router.delete("/disconnect", summary="Disconnect every mailbox for this user")
async def disconnect_email(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Backwards-compatible bulk disconnect, unchanged in behaviour."""
    connections = _user_connections(db, current_user, active_only=True)
    for connection in connections:
        await _revoke_quietly(db, connection)
        _clear_credentials(connection)

    removed = _purge_documents(db, current_user)
    db.commit()
    logger.info("[Email] user %s disconnected %s mailbox(es)", current_user.id, len(connections))
    return {
        "status": "success",
        "message": "Successfully disconnected email account.",
        "disconnected": len(connections),
        "documents_removed": removed,
    }


# ---------------------------------------------------------------------------
# OAuth callbacks
# ---------------------------------------------------------------------------

async def _handle_oauth_callback(
    request: Request,
    db: Session,
    *,
    provider: str,
    code: Optional[str],
    state: Optional[str],
    error: Optional[str],
    callback_path: str,
) -> Any:
    wants_html = "text/html" in request.headers.get("accept", "") and \
                 "application/json" not in request.headers.get("accept", "")
    record: Optional[OAuthState] = None

    def fail(message: str, http_status: int = 400):
        logger.warning("[Email OAuth] %s connection failed: %s", provider, message)
        _record_result(db, record, status_="failed", detail=message)
        if wants_html:
            return _render_result_html("Mailbox connection failed", message, ok=False, state=state or "")
        raise HTTPException(status_code=http_status, detail=message)

    # The state is read first, so that even a refusal on the provider's page is
    # recorded against the sign-in the user started and the app can show it.
    found = _consume_state_record(db, state)
    if found is not None:
        user, record = found
    else:
        user = None

    error_description = request.query_params.get("error_description", "")
    if error:
        err_combined = f"{error} {error_description}".lower()
        if any(code in err_combined for code in ("aadsts65001", "aadsts90094", "aadsts90093", "admin_consent")) or (provider == "microsoft" and "admin" in err_combined and "consent" in err_combined):
            friendly = "Your organization's admin needs to approve this app once before you can connect your work or school Microsoft account."
        elif "admin_policy_enforced" in err_combined or "org_internal" in err_combined:
            friendly = "Your Google Workspace administrator has restricted access to this app."
        elif provider == "gmail" and ("unverified" in err_combined or "testing" in err_combined or "test user" in err_combined):
            friendly = "This app is still being verified by Google — if you're a test user, continue past the warning screen; otherwise access isn't available yet."
        elif error in ("access_denied", "consent_required"):
            friendly = "You cancelled the connection."
        else:
            friendly = f"The provider returned an error: {error_description or error}"
        return fail(friendly)

    if user is None:
        # Wording kept verbatim from the previous implementation: it is asserted
        # on by the OAuth-CSRF regression test, and it is already the clearest
        # description of all three cases (never issued, expired, replayed).
        return fail("Invalid, expired, or already-used OAuth state. "
                    "Please re-initiate the mailbox connection.")

    if not code:
        return fail("The provider did not return an authorization code.")

    client = get_oauth_client(provider)
    redirect_uri = _callback_uri(request, callback_path, client.default_redirect_uri, _redirect_env(provider))

    try:
        tokens = await client.exchange_code(code, redirect_uri=redirect_uri)
    except MailboxError as exc:
        return fail(exc.detail)
    except Exception as exc:
        logger.exception("[Email OAuth] token exchange failed for %s", provider)
        return fail(f"Could not complete the connection: {exc}")

    email_address = (tokens.get("email") or "").strip()
    if not email_address:
        return fail("The provider did not disclose which mailbox was authorised.")

    # Google lets the user untick the mail permission on its consent page; the
    # sign-in then "succeeds" with a token that cannot read a single message.
    granted = tokens.get("scope") or " ".join(client.scopes)
    needed = "gmail.readonly" if provider == "gmail" else "mail.read"
    if needed not in granted.lower():
        return fail("Permission to read your mail was not granted. Connect again and "
                    "leave the 'Read your email' box ticked on the consent page.")

    # Prove a mailbox exists behind the account before saving it: a Google
    # account can exist for an address whose mail is on Microsoft, and the
    # reverse; either way nothing could be picked up.
    try:
        await _check_mailbox(provider, tokens)
    except MailboxError as exc:
        return fail(exc.detail)

    connection = _upsert_connection(
        db, user,
        provider=provider,
        email_address=email_address,
        provider_account_id=tokens.get("provider_account_id"),
        display_name=tokens.get("name"),
        tokens=tokens,
        scopes=granted,
    )
    logger.info("[Email OAuth] user %s connected %s mailbox %s",
                user.id, provider, connection.id)

    # Pick-up starts at once: connecting a mailbox is asking for its statements.
    scan_id = None
    try:
        if os.getenv("MAILBOX_SCAN_ON_CONNECT", "true").lower() == "true":
            scan = create_scan(db, user_id=user.id, connection=connection, auto_import=True)
            start_scan_in_background(scan.id)
            scan_id = scan.id
    except Exception:
        logger.exception("[Email OAuth] could not start the first scan for %s", connection.id)

    _record_result(db, record, status_="connected", email=email_address,
                   connection_id=connection.id, scan_id=scan_id)

    if wants_html:
        return _render_result_html(
            "Mailbox connected",
            f"Successfully authorised {email_address}. Looking for statements now.",
            ok=True, email_address=email_address, state=state or "",
        )
    return {
        "status": "success",
        "message": f"Successfully connected {provider} account {email_address}",
        "provider": provider,
        "email_address": email_address,
        "connection_id": str(connection.id),
        "scan_id": str(scan_id) if scan_id else None,
        "connected_at": datetime.datetime.utcnow().isoformat(),
    }


async def _check_mailbox(provider: str, tokens: Dict[str, Any]) -> None:
    from app.config import settings

    access = tokens.get("access_token") or ""
    if settings.DEMO_MODE or access.startswith("demo_"):
        return
    from app.mailbox.registry import PROVIDER_CLASSES

    conn = PROVIDER_CLASSES[normalise_provider(provider)](
        access_token=access, refresh_token=tokens.get("refresh_token"))
    try:
        await conn.connect()
        await conn.check_mailbox()
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass


@router.get("/oauth/result", summary="Outcome of a mailbox sign-in started by this user")
def oauth_result(
    state: str = Query(..., min_length=8, max_length=255),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """``pending`` until the provider sends the user back, then ``connected``
    (with the connection and the first scan) or ``failed`` (with the reason)."""
    record = db.query(OAuthState).filter(OAuthState.state_value == state,
                                         OAuthState.user_id == current_user.id).first()
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown or expired sign-in.")
    return {
        "status": record.result_status or "pending",
        "detail": record.result_detail,
        "email_address": record.result_email,
        "connection_id": str(record.connection_id) if record.connection_id else None,
        "scan_id": str(record.scan_id) if record.scan_id else None,
    }


@router.get("/oauth/callback", summary="Google OAuth callback")
@router.get("/oauth/callback/", summary="Google OAuth callback (trailing slash)")
async def google_oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    return await _handle_oauth_callback(
        request, db, provider="gmail", code=code, state=state, error=error,
        callback_path="/email/oauth/callback",
    )


@router.get("/oauth/microsoft/callback", summary="Microsoft OAuth callback")
@router.get("/oauth/microsoft/callback/", summary="Microsoft OAuth callback (trailing slash)")
async def microsoft_oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    return await _handle_oauth_callback(
        request, db, provider="microsoft", code=code, state=state, error=error,
        callback_path="/email/oauth/microsoft/callback",
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@router.get("/status", summary="Mailbox connection and discovery status")
def get_email_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    connections = _user_connections(db, current_user, active_only=True)
    total_statements = db.query(EmailAttachment).filter(
        EmailAttachment.user_id == current_user.id
    ).count()
    pending_imports = db.query(EmailAttachment).filter(
        EmailAttachment.user_id == current_user.id,
        EmailAttachment.import_status == "PENDING",
    ).count()

    latest_scan = db.query(MailboxScan).filter(
        MailboxScan.user_id == current_user.id
    ).order_by(MailboxScan.created_at.desc()).first()

    if not connections:
        return {
            "connected": False,
            "provider": None,
            "email_address": None,
            "last_scan": None,
            "total_statements_found": total_statements,
            "pending_imports": pending_imports,
            "connections": [],
            "latest_scan": _scan_payload(latest_scan) if latest_scan else None,
        }

    primary = connections[0]
    last_scan = max((c.last_scan_at for c in connections if c.last_scan_at), default=None)
    return {
        "connected": True,
        # The single-mailbox fields describe the first connection, for callers
        # written before multiple mailboxes were possible.
        "provider": primary.provider,
        "email_address": primary.email_address,
        "last_scan": _format_dt(last_scan),
        "total_statements_found": total_statements,
        "pending_imports": pending_imports,
        "connections": [_connection_payload(c) for c in connections],
        "latest_scan": _scan_payload(latest_scan) if latest_scan else None,
    }


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

@router.post("/scans", summary="Start a background mailbox scan")
def start_scan(
    connection_id: Optional[str] = Query(None, description="Defaults to every active mailbox"),
    auto_import: bool = Query(True, description="Extract transactions from confident matches"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Queue a scan and return immediately.

    Long mailboxes are why this exists: the HTTP request records the intent and
    a worker does the work, with progress readable from ``GET /email/scans/{id}``.
    """
    if connection_id:
        connections = [_owned_connection(db, current_user, connection_id)]
    else:
        connections = _user_connections(db, current_user, active_only=True)

    if not connections:
        raise HTTPException(
            status_code=400,
            detail="No mailbox is connected. Connect an email account first.",
        )

    scans = []
    for connection in connections:
        scan = create_scan(db, user_id=current_user.id, connection=connection,
                           auto_import=auto_import)
        start_scan_in_background(scan.id)
        scans.append(scan)

    return {"status": "queued", "scans": [_scan_payload(s) for s in scans]}


@router.get("/scans", summary="Recent scans")
def list_scans(
    limit: int = Query(10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    scans = db.query(MailboxScan).filter(
        MailboxScan.user_id == current_user.id
    ).order_by(MailboxScan.created_at.desc()).limit(limit).all()
    return [_scan_payload(s) for s in scans]


@router.get("/scans/{scan_id}", summary="Scan progress")
def get_scan(
    scan_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    scan = db.query(MailboxScan).filter(
        MailboxScan.id == _as_uuid(scan_id, "scan_id"),
        MailboxScan.user_id == current_user.id,
    ).first()
    if scan is None:
        raise HTTPException(status_code=404, detail="Scan not found.")
    db.refresh(scan)
    return _scan_payload(scan)


@router.post("/scan", summary="Scan mailboxes for statements (waits for the result)")
def scan_mailbox(
    connection_id: Optional[str] = Query(None),
    auto_import: bool = Query(False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Run a scan and return its result in the response.

    Kept because existing callers depend on the synchronous shape. New callers
    should prefer ``POST /email/scans``, which does not hold a request open for
    the length of the scan.
    """
    if connection_id:
        connections = [_owned_connection(db, current_user, connection_id)]
    else:
        connections = _user_connections(db, current_user, active_only=True)

    if not connections:
        return {
            "status": "error",
            "message": "No active connected email account found. Please connect a mailbox first.",
            "reason": "no_connected_account",
            "statements": [],
            "total_found": 0,
        }

    totals = {"documents": 0, "statements": 0, "duplicates": 0, "failures": 0}
    reasons: List[str] = []
    scan_ids: List[str] = []

    for connection in connections:
        scan = create_scan(db, user_id=current_user.id, connection=connection,
                           auto_import=auto_import)
        scan_ids.append(str(scan.id))
        execute_scan(scan.id)
        db.expire_all()
        refreshed = db.query(MailboxScan).filter(MailboxScan.id == scan.id).first()
        if refreshed is None:
            continue
        totals["documents"] += refreshed.documents_downloaded
        totals["statements"] += refreshed.statements_found
        totals["duplicates"] += refreshed.duplicates_skipped
        totals["failures"] += refreshed.failures
        if refreshed.status == MailboxScan.STATUS_FAILED:
            reasons.append(refreshed.error_reason or "scan_failed")
        elif refreshed.error_detail:
            reasons.append(refreshed.error_detail)

    statements = _list_statements(db, current_user)
    found = totals["documents"] + totals["duplicates"]
    reason = reasons[0] if reasons else ("ok" if found else "no_statements_found")

    return {
        "status": "success",
        "email_address": connections[0].email_address,
        "last_scan": _format_dt(connections[0].last_scan_at),
        "total_found": found,
        "statements_found": totals["statements"],
        "new_found": totals["documents"],
        "duplicates_found": totals["duplicates"],
        "failures": totals["failures"],
        "scan_ids": scan_ids,
        "statements": statements,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Discovered documents
# ---------------------------------------------------------------------------

def _list_statements(db: Session, user: User) -> List[Dict[str, Any]]:
    """Documents this user has discovered, actionable ones first.

    Ordered so the list opens on what the user can do something about: pending
    statements before imported ones, and rejected non-statements last.
    """
    rows = db.query(EmailAttachment).filter(
        EmailAttachment.user_id == user.id
    ).order_by(EmailAttachment.created_at.desc()).all()

    def rank(row: EmailAttachment) -> int:
        if row.classification == "NOT_BANK_STATEMENT":
            return 2
        if row.import_status == "IMPORTED":
            return 1
        return 0

    rows.sort(key=lambda r: (rank(r), -(r.received_at or datetime.datetime.min).timestamp()
                             if r.received_at else 0))
    return [_attachment_payload(r) for r in rows]


@router.get("/statements", summary="List discovered financial documents")
def list_email_statements(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _list_statements(db, current_user)


class AccountMappingPayload(BaseModel):
    bank_account_id: Optional[str] = None
    account_id: Optional[str] = None
    pdf_password: Optional[str] = None

    def get_bank_account_id(self) -> Optional[str]:
        return self.bank_account_id or self.account_id


def _validated_account(db: Session, user: User, account_id_input: Optional[Any]):
    """Resolve a bank-master account that belongs to this user, or fail.

    The ownership check is the anti-IDOR boundary for imports: without it a user
    could file another user's statement against their own account, or worse,
    file their own statement into someone else's ledger.
    """
    if not account_id_input:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("Target bank account selection is required before importing document. "
                    "Please select a bank account."),
        )
    from app.models.account import Account

    account_uuid = _as_uuid(account_id_input, "bank_account_id")
    account = db.query(Account).filter(Account.id == account_uuid).first()
    if account is None:
        raise HTTPException(status_code=404, detail=f"Bank account '{account_uuid}' not found.")

    if str(account.user_id).replace("-", "").lower() != str(user.id).replace("-", "").lower():
        logger.warning("[Security] user %s attempted to use bank account %s owned by %s",
                       user.id, account.id, account.user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Selected bank account belongs to another user.",
        )
    if account.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected bank account has been deleted and cannot accept new imports.",
        )
    return account


@router.post("/statements/{attachment_id}/map-account", summary="Assign a document to an account")
def map_account_to_attachment(
    attachment_id: str,
    payload: AccountMappingPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    attachment = _owned_attachment(db, current_user, attachment_id)
    account = _validated_account(db, current_user, payload.get_bank_account_id())

    attachment.account_id = account.id
    if attachment.classification not in ("PDF_PASSWORD_REQUIRED", "PASSWORD_INVALID"):
        attachment.classification = "BANK_STATEMENT_CONFIRMED"
        attachment.classification_reason = (
            f"Mapped to Bank Master account {account.account_number_masked}"
        )
    db.commit()
    return {
        "status": "success",
        "message": f"Successfully associated statement with Bank Master account "
                   f"{account.account_number_masked}",
        "attachment_id": str(attachment.id),
        "bank_account_id": str(account.id),
        "account_id": str(account.id),
    }


def _reclassify_with_password(
    db: Session, attachment: EmailAttachment, pdf_password: Optional[str],
) -> None:
    """Re-read a document now that a password may be available.

    Raises the appropriate 400 when the document is still locked or turns out
    not to be a statement, so the caller does not have to interpret a verdict.
    """
    from app.statements.classifier import classify_file

    if not attachment.local_path or not os.path.exists(attachment.local_path):
        raise HTTPException(status_code=400,
                            detail="Downloaded attachment file is missing on server.")

    verdict = classify_file(
        attachment.local_path, attachment.filename,
        password=pdf_password,
        sender_name=attachment.sender or "",
        subject=attachment.subject or "",
    )

    if verdict.password_required:
        attachment.classification = "PDF_PASSWORD_REQUIRED"
        attachment.classification_reason = verdict.reason
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=verdict.reason)
    if verdict.password_invalid:
        attachment.classification = "PASSWORD_INVALID"
        attachment.classification_reason = verdict.reason
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=verdict.reason)
    if not verdict.is_transactional:
        attachment.classification = "NOT_BANK_STATEMENT"
        attachment.classification_reason = verdict.reason
        attachment.document_type = verdict.document_type
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot import '{attachment.filename}': {verdict.reason}",
        )

    attachment.classification = "BANK_STATEMENT_CONFIRMED"
    attachment.classification_reason = verdict.reason
    attachment.document_type = verdict.document_type
    attachment.institution_name = verdict.institution_name
    attachment.institution_confidence = verdict.institution.confidence
    attachment.statement_period_start = verdict.period_start
    attachment.statement_period_end = verdict.period_end
    attachment.currency = verdict.currency
    if verdict.account_identifier and not attachment.detected_account_number:
        attachment.detected_account_number = verdict.account_identifier
    db.commit()


@router.post("/import/{attachment_id}", summary="Import one discovered statement")
def import_email_statement(
    attachment_id: str,
    payload: Optional[AccountMappingPayload] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    attachment = _owned_attachment(db, current_user, attachment_id)

    requested = (payload.get_bank_account_id() if payload else None) or attachment.account_id
    if not requested:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("Target Bank Master account selection is required before importing "
                    "statement. Please select a bank account."),
        )
    account = _validated_account(db, current_user, requested)
    attachment.account_id = account.id

    pdf_password = (payload.pdf_password or "").strip() if payload and payload.pdf_password else None
    _reclassify_with_password(db, attachment, pdf_password)

    connection = None
    if attachment.connected_account_id:
        connection = db.query(ConnectedAccount).filter(
            ConnectedAccount.id == attachment.connected_account_id,
            ConnectedAccount.user_id == current_user.id,
        ).first()

    result = ingest_attachment(
        db, attachment,
        user_id=current_user.id,
        account_id=account.id,
        pdf_password=pdf_password,
        provider=connection.provider if connection else None,
    )
    db.commit()

    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.error_message or f"Import of '{attachment.filename}' produced no transactions.",
        )

    return {
        "status": "success",
        "message": f"Successfully imported {attachment.filename}",
        "file_id": result.file_id,
        "bank_account_id": str(account.id),
        "account_id": str(account.id),
        "summary": result.summary,
        "transactions_created": result.transactions_created,
    }


@router.post("/import-all", summary="Import every pending discovered statement")
def import_all_pending_statements(
    payload: Optional[AccountMappingPayload] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    fallback_account_id = payload.get_bank_account_id() if payload else None
    if fallback_account_id:
        _validated_account(db, current_user, fallback_account_id)

    pending = db.query(EmailAttachment).filter(
        EmailAttachment.user_id == current_user.id,
        EmailAttachment.import_status == "PENDING",
        EmailAttachment.classification != "NOT_BANK_STATEMENT",
    ).all()

    if not pending:
        return {
            "status": "info",
            "message": "No pending email statement attachments with assigned accounts to import.",
            "count": 0,
            "imported_count": 0,
            "details": [],
        }

    results: List[Dict[str, Any]] = []
    for attachment in pending:
        target = attachment.account_id or fallback_account_id
        if not target:
            results.append({"filename": attachment.filename, "status": "skipped",
                            "reason": "No bank account assigned"})
            continue
        try:
            account = _validated_account(db, current_user, target)
            attachment.account_id = account.id
            result = ingest_attachment(
                db, attachment, user_id=current_user.id, account_id=account.id,
            )
            db.commit()
            if result.ok:
                results.append({
                    "filename": attachment.filename, "status": "success",
                    "file_id": result.file_id,
                    "bank_account_id": str(account.id),
                    "transactions_created": result.transactions_created,
                })
            else:
                results.append({"filename": attachment.filename, "status": "failed",
                                "error": result.error_message or "Zero transactions parsed"})
        except HTTPException as exc:
            results.append({"filename": attachment.filename, "status": "skipped",
                            "reason": exc.detail})
        except Exception as exc:
            db.rollback()
            logger.exception("[Email Import] failed for attachment %s", attachment.id)
            results.append({"filename": attachment.filename, "status": "failed",
                            "error": str(exc)})

    return {
        "status": "success",
        "message": f"Processed {len(results)} statement attachments.",
        "count": len(results),
        "imported_count": len([r for r in results if r["status"] == "success"]),
        "details": results,
    }
