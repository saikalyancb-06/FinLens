"""Provider registry — the only place that turns a stored connection into a live
mailbox client.

Everything security-relevant about mailbox access converges here:

* credentials are decrypted at the last possible moment and never leave this
  module in plaintext except into the connector that needs them;
* a connection is opened *from a row that the caller has already proved belongs
  to the authenticated user* — this module deliberately has no "find the
  connection for user X" helper that takes an id from a request body;
* an expired access token is refreshed and the new one re-encrypted and
  persisted before the caller sees the provider.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Callable, Dict, List, Optional, Type

from sqlalchemy.orm import Session

# app.database.session must be imported before any individual model module.
# Importing it runs the mapper-registration loop at the bottom of that file,
# which is what puts `users` (and everything else) on Base.metadata. Reaching
# app.email.models first instead leaves that loop re-entering a half-initialised
# app.models package, and create_all then fails looking for a `users` table that
# has not been declared yet.
import app.database.session  # noqa: F401  (imported for its side effect)
from app.email.models import (
    CONNECTION_CONNECTED,
    CONNECTION_NEEDS_REAUTH,
    ConnectedAccount,
)
from app.email.utils import decrypt_token, encrypt_token
from app.mailbox.base import EmailProvider
from app.mailbox.errors import (
    AuthenticationExpired,
    ConnectionConfigurationError,
    CredentialsUnreadable,
    MailboxError,
    ProviderNotSupported,
)
from app.mailbox.gmail import GmailProvider
from app.mailbox.imap import ImapProvider
from app.mailbox.microsoft import MicrosoftGraphProvider

logger = logging.getLogger(__name__)

#: registry key -> connector class. Adding a provider is this line plus a module.
PROVIDER_CLASSES: Dict[str, Type[EmailProvider]] = {
    "gmail": GmailProvider,
    "microsoft": MicrosoftGraphProvider,
    "imap": ImapProvider,
}

#: What the connect screen offers. ``oauth`` providers redirect to a consent
#: page; ``app_password`` providers collect host + application password.
PROVIDER_CATALOG: List[dict] = [
    {
        "key": "gmail",
        "label": "Gmail",
        "auth": "oauth",
        "description": "Google Workspace and personal Gmail accounts.",
    },
    {
        "key": "microsoft",
        "label": "Microsoft / Outlook",
        "auth": "oauth",
        "description": "Outlook.com, Hotmail, Live and Microsoft 365 mailboxes.",
    },
    {
        "key": "imap",
        "label": "Other email provider",
        "auth": "app_password",
        "description": "Any IMAP mailbox: custom domains, university, company, "
                       "Yahoo, Zoho, Fastmail and similar.",
    },
]

#: Legacy aliases so rows written before the multi-provider work still resolve.
PROVIDER_ALIASES = {
    "google": "gmail",
    "outlook": "microsoft",
    "hotmail": "microsoft",
    "office365": "microsoft",
    "o365": "microsoft",
    "yahoo": "imap",
    "other": "imap",
}


def normalise_provider(provider: Optional[str]) -> str:
    key = (provider or "gmail").strip().lower()
    return PROVIDER_ALIASES.get(key, key)


def is_supported(provider: Optional[str]) -> bool:
    return normalise_provider(provider) in PROVIDER_CLASSES


def _decrypt(value: Optional[str], *, what: str) -> Optional[str]:
    if not value:
        return None
    try:
        return decrypt_token(value)
    except ValueError as exc:
        raise CredentialsUnreadable(
            f"The stored {what} could not be decrypted. The encryption key has "
            "changed; this mailbox must be reconnected.",
        ) from exc


def _persist_refreshed_tokens(db: Session, connection: ConnectedAccount) -> Callable[[dict], None]:
    """Build the callback a connector invokes after a silent token refresh.

    Written back immediately rather than at the end of the scan: a scan that
    crashes halfway should not throw away a perfectly good new token and force
    the user to reconnect.
    """

    def _apply(bundle: dict) -> None:
        try:
            if bundle.get("access_token"):
                connection.access_token = encrypt_token(bundle["access_token"])
            if bundle.get("refresh_token"):
                connection.encrypted_refresh_token = encrypt_token(bundle["refresh_token"])
            expires_in = int(bundle.get("expires_in") or 3600)
            connection.token_expires_at = _dt.datetime.utcnow() + _dt.timedelta(seconds=expires_in)
            connection.status = CONNECTION_CONNECTED
            connection.status_detail = None
            db.commit()
            logger.info("[Mailbox] refreshed access token for connection %s (%s)",
                        connection.id, connection.provider)
        except Exception:
            db.rollback()
            logger.exception("[Mailbox] failed to persist refreshed token for connection %s",
                             connection.id)

    return _apply


def _token_is_stale(connection: ConnectedAccount) -> bool:
    if connection.token_expires_at is None:
        return True
    # 120s of slack: a token that expires mid-scan is worse than one refreshed
    # slightly early.
    return connection.token_expires_at <= _dt.datetime.utcnow() + _dt.timedelta(seconds=120)


def _is_demo_connection(connection: ConnectedAccount) -> bool:
    """True when this connection should be served by the fixture mailbox.

    Either the whole deployment is in demo mode, or this particular row holds a
    credential minted by the demo OAuth path. The second case matters because a
    demo connection must keep working after DEMO_MODE is switched off rather
    than start firing real requests at Google with a fake token.
    """
    from app.config import settings

    if settings.DEMO_MODE:
        return True
    for value in (connection.access_token, connection.encrypted_refresh_token):
        if not value:
            continue
        try:
            if decrypt_token(value).startswith("demo_"):
                return True
        except ValueError:
            continue
    return False


async def open_provider(db: Session, connection: ConnectedAccount) -> EmailProvider:
    """Return a connected, authenticated provider for ``connection``.

    The caller is responsible for having established that ``connection`` belongs
    to the authenticated user *before* calling this. Passing in a row fetched by
    a client-supplied id without that check is the IDOR this design is shaped to
    prevent, which is why no id is accepted here.
    """
    provider_key = normalise_provider(connection.provider)
    cls = PROVIDER_CLASSES.get(provider_key)
    if cls is None:
        raise ProviderNotSupported(f"No mailbox connector is registered for '{connection.provider}'.")

    if _is_demo_connection(connection):
        from app.mailbox.demo import DemoProvider

        demo = DemoProvider(email_address=connection.email_address, provider=provider_key)
        await demo.connect()
        return demo

    if provider_key == "imap":
        provider = _build_imap(connection)
        await provider.connect()
        await provider.authenticate()
        return provider

    refresh_token = _decrypt(connection.encrypted_refresh_token, what="refresh token")
    access_token = _decrypt(connection.access_token, what="access token")

    provider = cls(  # type: ignore[call-arg]
        access_token=access_token or "",
        refresh_token=refresh_token,
        on_token_refresh=_persist_refreshed_tokens(db, connection),
    )
    await provider.connect()

    if refresh_token and _token_is_stale(connection):
        await provider.refresh_authentication()
    elif not access_token:
        if not refresh_token:
            raise AuthenticationExpired(
                "This mailbox has no usable stored credential. Please reconnect it.",
                provider=provider_key,
            )
        await provider.refresh_authentication()

    try:
        await provider.authenticate()
    except AuthenticationExpired:
        # One retry: a token can expire between the staleness check and the call.
        if not refresh_token:
            raise
        await provider.refresh_authentication()
        await provider.authenticate()

    return provider


def _build_imap(connection: ConnectedAccount) -> ImapProvider:
    if not connection.imap_host:
        raise ConnectionConfigurationError(
            "This IMAP connection has no server host recorded; please reconnect it.",
            provider="imap",
        )
    password = _decrypt(connection.encrypted_secret, what="application password")
    access_token = _decrypt(connection.access_token, what="access token")
    return ImapProvider(
        host=connection.imap_host,
        port=int(connection.imap_port or 993),
        use_ssl=bool(connection.imap_use_ssl if connection.imap_use_ssl is not None else True),
        username=connection.imap_username or connection.email_address,
        password=password,
        access_token=access_token,
        mailbox=connection.imap_mailbox or "INBOX",
        email_address=connection.email_address,
    )


def mark_connection_failed(db: Session, connection: ConnectedAccount, error: MailboxError) -> None:
    """Record why a mailbox stopped working, so the UI can ask for the right fix.

    A revoked token and a network blip need different words in front of the
    user; collapsing both to "scan failed" is what made the previous version
    unactionable.
    """
    try:
        connection.status = error.connection_status or CONNECTION_NEEDS_REAUTH
        connection.status_detail = error.detail[:1000]
        connection.last_error_at = _dt.datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[Mailbox] could not record failure on connection %s", connection.id)


def mark_connection_healthy(db: Session, connection: ConnectedAccount) -> None:
    try:
        if connection.status != CONNECTION_CONNECTED or connection.status_detail:
            connection.status = CONNECTION_CONNECTED
            connection.status_detail = None
            db.commit()
    except Exception:
        db.rollback()
