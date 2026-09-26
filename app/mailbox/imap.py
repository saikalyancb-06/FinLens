"""Generic IMAP connector, for mailboxes without a dedicated API integration.

Covers custom domains, university and company mailboxes, Yahoo, Zoho, Fastmail,
iCloud and anything else that speaks standards-compliant IMAP over TLS.

Authentication, in order of preference:

1. ``XOAUTH2`` — used whenever the connection carries an OAuth access token, so
   a provider that supports OAuth over IMAP never sees a password.
2. A provider-issued **application-specific password** (Yahoo "app password",
   iCloud "app-specific password", Google "App Password", Fastmail "app
   password"). These are per-application credentials that the user generates and
   can revoke individually; they are not the account's login password.

The connector never asks for and never accepts a primary account password as a
matter of product policy — see ``app/mailbox/registry.py``, which is where the
credential bundle is assembled, and the ``/email/connect/imap`` route, which
labels the field accordingly.

IMAP is used *only* to read. SMTP appears nowhere in this codebase's mailbox
path: it is a sending protocol and cannot retrieve anything.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import email
import email.utils
import imaplib
import email.header
import logging
import socket
from email.message import Message
from typing import AsyncIterator, Dict, List, Optional, Tuple

from app.mailbox.base import EmailProvider
from app.mailbox.criteria import SearchCriteria
from app.mailbox.errors import (
    AuthenticationRevoked,
    ConnectionConfigurationError,
    MessageNotFound,
    ProviderUnavailable,
)
from app.mailbox.types import (
    AttachmentPayload,
    AttachmentRef,
    MailboxAccountInfo,
    MessageDetail,
    MessageSummary,
)
import ssl

logger = logging.getLogger(__name__)

#: Prefill hints for the "other provider" form. Covers major webmail providers
#: and provides a generic cPanel (mail.<domain>:993) default guess.
KNOWN_IMAP_HOSTS: Dict[str, Tuple[str, int]] = {
    # Yahoo
    "yahoo.com": ("imap.mail.yahoo.com", 993),
    "yahoo.co.in": ("imap.mail.yahoo.com", 993),
    "yahoo.co.uk": ("imap.mail.yahoo.com", 993),
    "yahoo.ca": ("imap.mail.yahoo.com", 993),
    "ymail.com": ("imap.mail.yahoo.com", 993),
    # Zoho
    "zoho.com": ("imap.zoho.com", 993),
    "zohomail.com": ("imap.zoho.com", 993),
    "zoho.in": ("imap.zoho.com", 993),
    "zoho.eu": ("imap.zoho.com", 993),
    # Fastmail
    "fastmail.com": ("imap.fastmail.com", 993),
    "fastmail.fm": ("imap.fastmail.com", 993),
    "messagingengine.com": ("imap.fastmail.com", 993),
    # Apple iCloud
    "icloud.com": ("imap.mail.me.com", 993),
    "me.com": ("imap.mail.me.com", 993),
    "mac.com": ("imap.mail.me.com", 993),
    # GMX
    "gmx.com": ("imap.gmx.com", 993),
    "gmx.net": ("imap.gmx.com", 993),
    "gmx.de": ("imap.gmx.com", 993),
    # AOL
    "aol.com": ("imap.aol.com", 993),
    "aim.com": ("imap.aol.com", 993),
    # Mail.com
    "mail.com": ("imap.mail.com", 993),
    "email.com": ("imap.mail.com", 993),
    # Yandex & Rediff
    "yandex.com": ("imap.yandex.com", 993),
    "yandex.ru": ("imap.yandex.com", 993),
    "rediffmail.com": ("imap.rediffmail.com", 993),
    # Proton (requires local bridge)
    "protonmail.com": ("127.0.0.1", 1143),
    "proton.me": ("127.0.0.1", 1143),
}

_MAX_CACHED_MESSAGES = 32
_DEFAULT_IMAP_TIMEOUT = 20  # seconds


def suggest_imap_host(email_address: str) -> Optional[Tuple[str, int]]:
    """Suggest IMAP server settings for an address or domain.

    Returns the known provider host/port if cataloged in KNOWN_IMAP_HOSTS,
    or None if the domain is not known.
    """
    raw = (email_address or "").strip().lower()
    domain = raw.rsplit("@", 1)[-1].strip() if "@" in raw else raw
    if not domain:
        return None
    return KNOWN_IMAP_HOSTS.get(domain)


def _imap_date(value: _dt.datetime) -> str:
    return value.strftime("%d-%b-%Y")


def _quote(term: str) -> str:
    return '"' + term.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapProvider(EmailProvider):
    key = "imap"
    label = "Other email provider (IMAP)"

    def __init__(
        self,
        host: str,
        username: str,
        *,
        port: int = 993,
        use_ssl: bool = True,
        password: Optional[str] = None,
        access_token: Optional[str] = None,
        mailbox: str = "INBOX",
        email_address: Optional[str] = None,
    ):
        if not host:
            raise ConnectionConfigurationError("No IMAP host is stored for this connection.",
                                               provider="imap")
        if not username:
            raise ConnectionConfigurationError("No IMAP username is stored for this connection.",
                                               provider="imap")
        if not password and not access_token:
            raise ConnectionConfigurationError(
                "This IMAP connection has neither an application password nor an "
                "OAuth token stored; it must be reconnected.",
                provider="imap",
            )
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.username = username
        self.email_address = email_address or username
        self._password = password
        self.access_token = access_token
        self.mailbox = mailbox or "INBOX"
        self._conn: Optional[imaplib.IMAP4] = None
        self._cache: Dict[str, Message] = {}
        self._cache_order: List[str] = []

    @property
    def auth_type(self) -> str:  # type: ignore[override]
        return "oauth" if self.access_token else "app_password"

    # ---- transport -------------------------------------------------------

    def _connect_blocking(self) -> None:
        timeout = _DEFAULT_IMAP_TIMEOUT
        try:
            if self.use_ssl:
                conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(self.host, self.port, timeout=timeout)
            else:
                # STARTTLS, never plaintext: a mailbox credential must not cross
                # the network in the clear even when the server offers to.
                conn = imaplib.IMAP4(self.host, self.port, timeout=timeout)
                conn.starttls()
        except socket.gaierror as exc:
            raise ProviderUnavailable(
                f"Could not find mail server '{self.host}' — check the IMAP host name.",
                provider="imap",
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ProviderUnavailable(
                f"Connection to mail server '{self.host}:{self.port}' timed out — check the host and port.",
                provider="imap",
            ) from exc
        except (ssl.SSLError, ssl.CertificateError) as exc:
            raise ProviderUnavailable(
                f"SSL/TLS connection failed for '{self.host}:{self.port}' — verify whether your mail server requires SSL on this port.",
                provider="imap",
            ) from exc
        except (ConnectionRefusedError, ConnectionResetError) as exc:
            raise ProviderUnavailable(
                f"Could not reach mail server '{self.host}:{self.port}' — connection was refused. Check the host and port.",
                provider="imap",
            ) from exc
        except OSError as exc:
            raise ProviderUnavailable(
                f"Could not reach mail server '{self.host}:{self.port}' ({exc}).",
                provider="imap",
            ) from exc

        try:
            if self.access_token:
                auth_string = f"user={self.username}\x01auth=Bearer {self.access_token}\x01\x01"
                conn.authenticate("XOAUTH2", lambda _challenge: auth_string.encode())
            else:
                conn.login(self.username, self._password or "")
        except imaplib.IMAP4.error as exc:
            detail = str(exc)
            try:
                conn.logout()
            except Exception:
                pass
            lowered = detail.lower()
            if "auth" in lowered or "login" in lowered or "credential" in lowered or "password" in lowered or "denied" in lowered:
                raise AuthenticationRevoked(
                    "We couldn't sign in — check your email address and app password. "
                    "If you use an application-specific password, verify it is still valid.",
                    provider="imap",
                ) from exc
            raise ProviderUnavailable(f"IMAP login failed: {detail}", provider="imap") from exc

        status, _ = conn.select(self.mailbox, readonly=True)
        if status != "OK":
            # readonly=True is deliberate: EXAMINE rather than SELECT means the
            # server will not mark anything as \Seen while we scan.
            try:
                conn.logout()
            except Exception:
                pass
            raise ProviderUnavailable(f"IMAP mailbox '{self.mailbox}' could not be opened.",
                                      provider="imap")
        self._conn = conn

    async def connect(self) -> None:
        if self._conn is None:
            await asyncio.to_thread(self._connect_blocking)

    async def disconnect(self) -> None:
        conn, self._conn = self._conn, None
        self._cache.clear()
        self._cache_order.clear()
        if conn is None:
            return

        def _close() -> None:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn.logout()
            except Exception:
                pass

        await asyncio.to_thread(_close)

    # ---- auth ------------------------------------------------------------

    async def authenticate(self) -> None:
        await self.connect()

    async def refresh_authentication(self) -> Optional[dict]:
        """IMAP with an app password has nothing to refresh.

        With XOAUTH2 the *token* is refreshed by the owning OAuth client before
        the provider is constructed, so by the time we are here the credential is
        already current.
        """
        if self.access_token:
            return None
        return None

    async def get_account_info(self) -> MailboxAccountInfo:
        return MailboxAccountInfo(
            email_address=self.email_address,
            provider_account_id=f"{self.host}:{self.username}",
            display_name=self.username,
            provider="imap",
        )

    # ---- search ----------------------------------------------------------

    def _search_blocking(self, criteria: SearchCriteria) -> List[bytes]:
        assert self._conn is not None
        conn = self._conn
        base: List[str] = []
        if criteria.since:
            base += ["SINCE", _imap_date(criteria.since)]
        if criteria.until:
            base += ["BEFORE", _imap_date(criteria.until)]

        seen: List[bytes] = []
        seen_set = set()

        def run(terms: List[str]) -> None:
            args = base + terms if base or terms else ["ALL"]
            try:
                status, data = conn.search(None, *args)
            except imaplib.IMAP4.error as exc:
                logger.info("[IMAP] SEARCH %s rejected by server: %s", args, exc)
                return
            if status != "OK" or not data:
                return
            for uid in (data[0] or b"").split():
                if uid not in seen_set:
                    seen_set.add(uid)
                    seen.append(uid)

        if criteria.keywords:
            for keyword in criteria.keywords:
                run(["HEADER", "SUBJECT", _quote(keyword)])
            # One body sweep on the single most general term. TEXT searches are
            # expensive server-side, so this runs once rather than per keyword.
            run(["BODY", _quote("statement")])
        else:
            run([])

        # Newest first: IMAP returns sequence numbers ascending.
        seen.reverse()
        return seen[: criteria.limit]

    def _fetch_headers_blocking(self, uid: bytes) -> Optional[MessageSummary]:
        assert self._conn is not None
        try:
            status, data = self._conn.fetch(
                uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE CONTENT-TYPE)])"
            )
        except imaplib.IMAP4.error as exc:
            logger.info("[IMAP] header fetch failed for %s: %s", uid, exc)
            return None
        if status != "OK" or not data or not isinstance(data[0], tuple):
            return None

        raw = data[0][1] or b""
        parsed = email.message_from_bytes(raw)
        sender_raw = parsed.get("From", "") or ""
        name, _addr = email.utils.parseaddr(sender_raw)
        received = None
        if parsed.get("Date"):
            try:
                received = email.utils.parsedate_to_datetime(parsed["Date"])
                if received and received.tzinfo is not None:
                    received = received.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            except (TypeError, ValueError):
                received = None

        content_type = (parsed.get("Content-Type", "") or "").lower()
        return MessageSummary(
            id=uid.decode() if isinstance(uid, bytes) else str(uid),
            subject=_decode_header(parsed.get("Subject", "")),
            sender=sender_raw,
            sender_name=name,
            received_at=received,
            # multipart/mixed is the shape a message with a file attachment
            # takes; multipart/alternative is just text+html and carries none.
            has_attachments="multipart/mixed" in content_type or "multipart/related" in content_type,
        )

    async def search_messages(self, criteria: SearchCriteria) -> AsyncIterator[MessageSummary]:
        await self.connect()
        uids = await asyncio.to_thread(self._search_blocking, criteria)
        logger.info("[IMAP] %s candidate message(s) on %s", len(uids), self.host)
        for uid in uids:
            summary = await asyncio.to_thread(self._fetch_headers_blocking, uid)
            if summary is None:
                continue
            if not criteria.within_window(summary.received_at):
                continue
            if criteria.require_attachment and not summary.has_attachments:
                continue
            yield summary

    # ---- message + attachments ------------------------------------------

    def _fetch_full_blocking(self, message_id: str) -> Message:
        assert self._conn is not None
        status, data = self._conn.fetch(message_id.encode(), "(BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise MessageNotFound(f"IMAP message {message_id} could not be fetched.",
                                  provider="imap")
        return email.message_from_bytes(data[0][1] or b"")

    async def _message(self, message_id: str) -> Message:
        cached = self._cache.get(message_id)
        if cached is not None:
            return cached
        await self.connect()
        parsed = await asyncio.to_thread(self._fetch_full_blocking, message_id)
        self._cache[message_id] = parsed
        self._cache_order.append(message_id)
        while len(self._cache_order) > _MAX_CACHED_MESSAGES:
            self._cache.pop(self._cache_order.pop(0), None)
        return parsed

    async def get_message(self, message_id: str) -> MessageDetail:
        parsed = await self._message(message_id)
        return message_to_detail(message_id, parsed)

    async def download_attachment(self, message_id: str, attachment: AttachmentRef) -> AttachmentPayload:
        parsed = await self._message(message_id)
        part = _part_at_path(parsed, attachment.part_path or attachment.ref)
        if part is None:
            raise MessageNotFound(
                f"MIME part '{attachment.part_path or attachment.ref}' is not present in message {message_id}.",
                provider="imap",
            )
        data = part.get_payload(decode=True) or b""
        if not data:
            raise MessageNotFound(f"Attachment '{attachment.filename}' decoded to zero bytes.",
                                  provider="imap")
        return AttachmentPayload(data=data, filename=attachment.filename,
                                 mime_type=attachment.mime_type)


# ---------------------------------------------------------------------------
# MIME walking — pure functions so nested/forwarded structures are testable
# without an IMAP server.
# ---------------------------------------------------------------------------

def _decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
    except Exception:
        return str(value)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _walk_parts(message: Message, path: str = ""):
    """Yield ``(path, part)`` for every node, descending into attached messages.

    ``message/rfc822`` parts are the forwarded-email case: the statement is one
    level further down and a non-recursive walk misses it entirely.
    """
    yield path, message
    if message.is_multipart():
        for index, part in enumerate(message.get_payload(), start=1):
            child_path = f"{path}.{index}" if path else str(index)
            if not isinstance(part, Message):
                continue
            yield from _walk_parts(part, child_path)
    elif message.get_content_type() == "message/rfc822":
        payload = message.get_payload()
        if isinstance(payload, list) and payload and isinstance(payload[0], Message):
            yield from _walk_parts(payload[0], f"{path}.1" if path else "1")


def _part_at_path(message: Message, path: str) -> Optional[Message]:
    for candidate_path, part in _walk_parts(message):
        if candidate_path == path:
            return part
    return None


def message_to_detail(message_id: str, parsed: Message) -> MessageDetail:
    text_bodies: List[str] = []
    html_bodies: List[str] = []
    attachments: List[AttachmentRef] = []

    for path, part in _walk_parts(parsed):
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        disposition = (part.get("Content-Disposition") or "").lower()
        filename = _decode_header(part.get_filename() or "")

        is_attachment = bool(filename) or "attachment" in disposition
        if is_attachment:
            try:
                size = len(part.get_payload(decode=True) or b"")
            except Exception:
                size = 0
            attachments.append(AttachmentRef(
                ref=path,
                filename=filename,
                mime_type=content_type,
                size=size,
                is_inline="inline" in disposition,
                content_id=(part.get("Content-ID") or "").strip("<>") or None,
                part_path=path,
            ))
            continue

        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        decoded = payload.decode(charset, errors="replace")
        if content_type == "text/plain":
            text_bodies.append(decoded)
        elif content_type == "text/html":
            html_bodies.append(decoded)

    sender_raw = parsed.get("From", "") or ""
    name, _addr = email.utils.parseaddr(sender_raw)
    received = None
    if parsed.get("Date"):
        try:
            received = email.utils.parsedate_to_datetime(parsed["Date"])
            if received and received.tzinfo is not None:
                received = received.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            received = None

    return MessageDetail(
        id=message_id,
        subject=_decode_header(parsed.get("Subject", "")),
        sender=sender_raw,
        sender_name=name,
        to=parsed.get("To", "") or "",
        received_at=received,
        body_text="\n".join(text_bodies),
        body_html="\n".join(html_bodies),
        attachments=attachments,
        headers={k: v for k, v in parsed.items()},
    )
