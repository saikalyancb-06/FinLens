"""Gmail connector — Gmail REST API v1.

Reading only: ``users.messages.list`` / ``.get`` / ``.attachments.get``. Gmail's
own search syntax does the coarse filtering server-side so a mailbox with
100 000 messages costs one list call, not 100 000 fetches.
"""
from __future__ import annotations

import base64
import datetime as _dt
import email.utils
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from app.mailbox.base import EmailProvider
from app.mailbox.criteria import SearchCriteria
from app.mailbox.errors import (
    ConnectionConfigurationError,
    AuthenticationExpired,
    AuthenticationRevoked,
    InsufficientPermissions,
    MessageNotFound,
    ProviderUnavailable,
    RateLimited,
)
from app.mailbox.types import (
    AttachmentPayload,
    AttachmentRef,
    MailboxAccountInfo,
    MessageDetail,
    MessageSummary,
)

logger = logging.getLogger(__name__)

API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def decode_b64url(data: str) -> bytes:
    """Decode Gmail's URL-safe base64, tolerating missing padding.

    Gmail strips ``=`` padding. ``base64.urlsafe_b64decode`` does not, and fails
    with a binascii error that reads like a corrupt attachment when the payload
    is perfectly fine.
    """
    if not data:
        return b""
    cleaned = data.replace("-", "+").replace("_", "/")
    pad = len(cleaned) % 4
    if pad:
        cleaned += "=" * (4 - pad)
    return base64.b64decode(cleaned)


def _parse_date(raw: Optional[str]) -> Optional[_dt.datetime]:
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return parsed


def build_query(criteria: SearchCriteria) -> str:
    """Translate provider-neutral criteria into a Gmail search string.

    Note what is *not* here: no ``from:`` clause naming a bank. Filtering by
    sender was the old implementation's central assumption and it silently
    dropped every statement from an institution nobody had listed.
    """
    clauses: List[str] = []

    if criteria.keywords:
        # Gmail matches subject and body for a bare term; quoting keeps
        # multi-word phrases together.
        terms = " OR ".join(f'"{k}"' if " " in k else k for k in criteria.keywords)
        clauses.append(f"({terms})")

    if criteria.require_attachment:
        clauses.append("has:attachment")

    if criteria.since:
        clauses.append(f"after:{criteria.since.strftime('%Y/%m/%d')}")
    if criteria.until:
        clauses.append(f"before:{criteria.until.strftime('%Y/%m/%d')}")

    for folder in criteria.folders:
        clauses.append(f"label:{folder}")

    # Chat and Drafts hold nothing a bank sent.
    clauses.append("-in:chats")
    return " ".join(clauses)


class GmailProvider(EmailProvider):
    key = "gmail"
    label = "Gmail"
    auth_type = "oauth"

    def __init__(self, access_token: str, refresh_token: Optional[str] = None,
                 *, on_token_refresh=None):
        self.access_token = access_token
        self.refresh_token = refresh_token
        #: called with the new token bundle so the caller can persist it
        self._on_token_refresh = on_token_refresh
        self._client: Optional[httpx.AsyncClient] = None

    # ---- lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60)

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def _get(self, url: str, params: Optional[dict] = None) -> dict:
        """One authenticated GET, with the provider's error vocabulary mapped."""
        await self.connect()
        assert self._client is not None
        try:
            res = await self._client.get(url, headers=self._headers, params=params)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Gmail request failed: {exc}", provider="gmail") from exc

        if res.status_code == 200:
            return res.json()
        if res.status_code == 401:
            raise AuthenticationExpired(
                "The Gmail access token was rejected. Reconnect the mailbox if this persists.",
                provider="gmail",
            )
        if res.status_code == 403:
            body = res.text.lower()
            if "ratelimit" in body or "userratelimitexceeded" in body or "quota" in body:
                raise RateLimited("Gmail rate limit reached.", provider="gmail",
                                  retry_after=float(res.headers.get("retry-after", 30) or 30))
            if "insufficient" in body or "scope" in body:
                raise InsufficientPermissions(
                    "This connection does not have permission to read Gmail messages. "
                    "Reconnect and grant read access.",
                    provider="gmail",
                )
            raise AuthenticationRevoked("Gmail refused the request (403).", provider="gmail")
        if res.status_code == 400 and ("failedprecondition" in res.text.lower().replace("_", "")
                                       or "mail service not enabled" in res.text.lower()):
            # A Google account with no Gmail behind it: e.g. an address at a
            # company whose mail is elsewhere, registered as a Google account.
            raise ConnectionConfigurationError(
                "This Google account has no Gmail mailbox. If your company's mail "
                "is on Microsoft 365, connect it with Microsoft; otherwise use "
                "'Other email provider'.",
                provider="gmail",
            )
        if res.status_code == 404:
            raise MessageNotFound("The Gmail message or attachment no longer exists.",
                                  provider="gmail")
        if res.status_code == 429:
            raise RateLimited("Gmail rate limit reached.", provider="gmail",
                              retry_after=float(res.headers.get("retry-after", 30) or 30))
        raise ProviderUnavailable(f"Gmail returned HTTP {res.status_code}.", provider="gmail")

    # ---- auth ------------------------------------------------------------

    async def authenticate(self) -> None:
        await self._get(f"{API_BASE}/profile")

    async def check_mailbox(self) -> None:
        """Prove there is a Gmail mailbox this token can read."""
        await self._get(f"{API_BASE}/profile")
        await self._get(f"{API_BASE}/messages", params={"maxResults": 1})

    async def refresh_authentication(self) -> Optional[dict]:
        from app.mailbox.oauth.google import google_oauth_client

        if not self.refresh_token:
            raise AuthenticationExpired(
                "No Gmail refresh token is stored for this connection; it must be reconnected.",
                provider="gmail",
            )
        bundle = await google_oauth_client.refresh(self.refresh_token)
        if bundle.get("access_token"):
            self.access_token = bundle["access_token"]
        if callable(self._on_token_refresh):
            self._on_token_refresh(bundle)
        return bundle

    async def get_account_info(self) -> MailboxAccountInfo:
        from app.config import settings

        if settings.DEMO_MODE or (self.access_token or "").startswith("demo_"):
            return MailboxAccountInfo(
                email_address="user.bank.statements@gmail.com",
                provider_account_id="demo-google-account",
                provider="gmail",
            )
        profile = await self._get(f"{API_BASE}/profile")
        address = profile.get("emailAddress", "")
        return MailboxAccountInfo(
            email_address=address,
            provider_account_id=address,
            provider="gmail",
            raw=profile,
        )

    # ---- search ----------------------------------------------------------

    async def search_messages(self, criteria: SearchCriteria) -> AsyncIterator[MessageSummary]:
        query = build_query(criteria)
        logger.info("[Gmail] search q=%r limit=%s", query, criteria.limit)

        page_token: Optional[str] = None
        yielded = 0
        while yielded < criteria.limit:
            params: Dict[str, Any] = {
                "q": query,
                "maxResults": min(criteria.page_size, criteria.limit - yielded),
            }
            if page_token:
                params["pageToken"] = page_token

            body = await self._get(f"{API_BASE}/messages", params=params)
            refs = body.get("messages") or []
            if not refs:
                return

            for ref in refs:
                # metadata format: headers only, no body and no attachment
                # payloads. Roughly two orders of magnitude cheaper than `full`,
                # which matters when the first pass looks at 200 messages and
                # keeps 6.
                meta = await self._get(
                    f"{API_BASE}/messages/{ref['id']}",
                    params={
                        "format": "metadata",
                        "metadataHeaders": ["Subject", "From", "Date"],
                    },
                )
                yield _summary_from_metadata(meta)
                yielded += 1
                if yielded >= criteria.limit:
                    return

            page_token = body.get("nextPageToken")
            if not page_token:
                return

    async def get_message(self, message_id: str) -> MessageDetail:
        msg = await self._get(f"{API_BASE}/messages/{message_id}", params={"format": "full"})
        return _detail_from_full(msg)

    async def download_attachment(self, message_id: str, attachment: AttachmentRef) -> AttachmentPayload:
        if attachment.inline_b64:
            data = decode_b64url(attachment.inline_b64)
        else:
            body = await self._get(f"{API_BASE}/messages/{message_id}/attachments/{attachment.ref}")
            data = decode_b64url(body.get("data") or "")

        if not data:
            raise MessageNotFound(
                f"Gmail returned an empty payload for '{attachment.filename or attachment.ref}'.",
                provider="gmail",
            )
        return AttachmentPayload(data=data, filename=attachment.filename,
                                 mime_type=attachment.mime_type)

    async def revoke_access(self) -> None:
        from app.mailbox.oauth.google import google_oauth_client

        await google_oauth_client.revoke(self.refresh_token or self.access_token)


# ---------------------------------------------------------------------------
# Payload shaping — module-level so they can be unit-tested without a network.
# ---------------------------------------------------------------------------

def _header(headers: List[dict], name: str) -> str:
    lowered = name.lower()
    for h in headers or []:
        if (h.get("name") or "").lower() == lowered:
            return h.get("value") or ""
    return ""


def _summary_from_metadata(meta: dict) -> MessageSummary:
    payload = meta.get("payload") or {}
    headers = payload.get("headers") or []
    sender_raw = _header(headers, "From")
    name, _addr = email.utils.parseaddr(sender_raw)

    # A message with any sub-part carrying a filename has an attachment. The
    # metadata format still reports the part tree's shape, just not its bytes.
    has_attachments = _payload_has_filename(payload)

    received = _parse_date(_header(headers, "Date"))
    if received is None and meta.get("internalDate"):
        try:
            received = _dt.datetime.utcfromtimestamp(int(meta["internalDate"]) / 1000)
        except (TypeError, ValueError):
            received = None

    return MessageSummary(
        id=meta.get("id", ""),
        subject=_header(headers, "Subject"),
        sender=sender_raw,
        sender_name=name,
        received_at=received,
        snippet=meta.get("snippet", "") or "",
        has_attachments=has_attachments,
        attachment_filenames=_payload_filenames(payload),
        raw={"labelIds": meta.get("labelIds", [])},
    )


def _payload_filenames(payload: dict) -> List[str]:
    names: List[str] = []

    def walk(part: dict) -> None:
        if part.get("filename"):
            names.append(part["filename"])
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    return names


def _payload_has_filename(payload: dict) -> bool:
    return bool(_payload_filenames(payload))


def _detail_from_full(msg: dict) -> MessageDetail:
    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []
    sender_raw = _header(headers, "From")
    name, _addr = email.utils.parseaddr(sender_raw)

    bodies = {"text": [], "html": []}
    attachments: List[AttachmentRef] = []

    def walk(part: dict, path: str = "") -> None:
        mime = (part.get("mimeType") or "").lower()
        filename = part.get("filename") or ""
        body = part.get("body") or {}
        att_id = body.get("attachmentId")
        data = body.get("data")
        headers_l = {(h.get("name") or "").lower(): h.get("value") or ""
                     for h in part.get("headers") or []}
        disposition = headers_l.get("content-disposition", "").lower()

        children = part.get("parts") or []

        # A part is an attachment candidate when it carries a filename or an
        # attachment id, regardless of where it sits in the tree. That covers
        # forwarded mail (message/rfc822 wrappers) and inline parts that a
        # filename-only check would miss.
        if (filename or att_id) and not children:
            attachments.append(AttachmentRef(
                ref=att_id or path,
                filename=filename,
                mime_type=mime,
                size=int(body.get("size") or 0),
                is_inline="inline" in disposition or bool(headers_l.get("content-id")),
                content_id=(headers_l.get("content-id") or "").strip("<>") or None,
                inline_b64=data if (data and not att_id) else None,
                part_path=path,
            ))
        elif not children and data:
            decoded = decode_b64url(data).decode("utf-8", errors="replace")
            if mime == "text/plain":
                bodies["text"].append(decoded)
            elif mime == "text/html":
                bodies["html"].append(decoded)

        for index, child in enumerate(children, start=1):
            walk(child, f"{path}.{index}" if path else str(index))

    walk(payload)

    received = _parse_date(_header(headers, "Date"))
    if received is None and msg.get("internalDate"):
        try:
            received = _dt.datetime.utcfromtimestamp(int(msg["internalDate"]) / 1000)
        except (TypeError, ValueError):
            received = None

    return MessageDetail(
        id=msg.get("id", ""),
        subject=_header(headers, "Subject"),
        sender=sender_raw,
        sender_name=name,
        to=_header(headers, "To"),
        received_at=received,
        body_text="\n".join(bodies["text"]),
        body_html="\n".join(bodies["html"]),
        attachments=attachments,
        headers={(h.get("name") or ""): (h.get("value") or "") for h in headers},
    )
