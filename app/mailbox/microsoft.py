"""Microsoft connector — Microsoft Graph v1.0 ``/me/messages``.

Works for Outlook.com, Hotmail, Live and Microsoft 365 mailboxes. The Gmail API
is never used for these accounts and vice versa: each connector speaks only its
own provider's protocol, and the layer above neither knows nor cares.
"""
from __future__ import annotations

import base64
import datetime as _dt
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from app.mailbox.base import EmailProvider
from app.mailbox.criteria import SearchCriteria
from app.mailbox.errors import (
    AuthenticationExpired,
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

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"
_ITEM_ATTACHMENT = "#microsoft.graph.itemAttachment"


def _parse_graph_datetime(raw: Optional[str]) -> Optional[_dt.datetime]:
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return parsed


def build_filter(criteria: SearchCriteria) -> Dict[str, Any]:
    """Translate provider-neutral criteria into Graph query parameters.

    Graph will not combine ``$search`` with ``$filter`` in one request, so the
    date window and attachment flag go into ``$filter`` and the keyword sieve is
    applied locally against subject/preview. That costs one extra pass over
    already-fetched metadata and keeps the result set identical to Gmail's.
    """
    filters: List[str] = []
    if criteria.require_attachment:
        filters.append("hasAttachments eq true")
    if criteria.since:
        filters.append(f"receivedDateTime ge {criteria.since.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    if criteria.until:
        filters.append(f"receivedDateTime le {criteria.until.strftime('%Y-%m-%dT%H:%M:%SZ')}")

    params: Dict[str, Any] = {
        "$select": "id,subject,from,receivedDateTime,hasAttachments,bodyPreview,internetMessageId",
        "$top": min(criteria.page_size, 100),
        "$orderby": "receivedDateTime desc",
    }
    if filters:
        params["$filter"] = " and ".join(filters)
    return params


class MicrosoftGraphProvider(EmailProvider):
    key = "microsoft"
    label = "Microsoft / Outlook"
    auth_type = "oauth"

    def __init__(self, access_token: str, refresh_token: Optional[str] = None,
                 *, on_token_refresh=None):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self._on_token_refresh = on_token_refresh
        self._client: Optional[httpx.AsyncClient] = None

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
        await self.connect()
        assert self._client is not None
        try:
            res = await self._client.get(url, headers=self._headers, params=params)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Microsoft Graph request failed: {exc}",
                                      provider="microsoft") from exc

        if res.status_code == 200:
            return res.json()
        if res.status_code == 401:
            raise AuthenticationExpired(
                "The Microsoft access token was rejected. Reconnect the mailbox if this persists.",
                provider="microsoft",
            )
        if res.status_code == 403:
            raise InsufficientPermissions(
                "This connection does not have permission to read the Microsoft mailbox. "
                "Reconnect and grant Mail.Read.",
                provider="microsoft",
            )
        if res.status_code == 404:
            raise MessageNotFound("The Microsoft message or attachment no longer exists.",
                                  provider="microsoft")
        if res.status_code == 429:
            raise RateLimited("Microsoft Graph is throttling this application.",
                              provider="microsoft",
                              retry_after=float(res.headers.get("retry-after", 30) or 30))
        if res.status_code >= 500:
            raise ProviderUnavailable(f"Microsoft Graph returned HTTP {res.status_code}.",
                                      provider="microsoft")
        raise ProviderUnavailable(f"Microsoft Graph returned HTTP {res.status_code}.",
                                  provider="microsoft")

    # ---- auth ------------------------------------------------------------

    async def authenticate(self) -> None:
        await self._get(f"{GRAPH_BASE}/me")

    async def refresh_authentication(self) -> Optional[dict]:
        from app.mailbox.oauth.microsoft import microsoft_oauth_client

        if not self.refresh_token:
            raise AuthenticationExpired(
                "No Microsoft refresh token is stored for this connection; it must be reconnected.",
                provider="microsoft",
            )
        bundle = await microsoft_oauth_client.refresh(self.refresh_token)
        if bundle.get("access_token"):
            self.access_token = bundle["access_token"]
        # Microsoft rotates refresh tokens: the old one stops working once the
        # new one is issued, so the caller must persist it or the next scan is
        # a forced reconnect.
        if bundle.get("refresh_token"):
            self.refresh_token = bundle["refresh_token"]
        if callable(self._on_token_refresh):
            self._on_token_refresh(bundle)
        return bundle

    async def get_account_info(self) -> MailboxAccountInfo:
        from app.config import settings

        if settings.DEMO_MODE or (self.access_token or "").startswith("demo_"):
            return MailboxAccountInfo(
                email_address="user.bank.statements@outlook.com",
                provider_account_id="demo-microsoft-account",
                provider="microsoft",
            )
        body = await self._get(f"{GRAPH_BASE}/me")
        address = body.get("mail") or body.get("userPrincipalName") or ""
        return MailboxAccountInfo(
            email_address=address,
            provider_account_id=str(body.get("id") or address),
            display_name=body.get("displayName"),
            provider="microsoft",
            raw=body,
        )

    # ---- search ----------------------------------------------------------

    async def search_messages(self, criteria: SearchCriteria) -> AsyncIterator[MessageSummary]:
        params = build_filter(criteria)
        url = f"{GRAPH_BASE}/me/messages"
        keywords = [k.lower() for k in criteria.keywords]
        yielded = 0
        examined = 0
        # Bound on messages *inspected*, not matched: without it a mailbox where
        # nothing matches would page to the beginning of time.
        examine_budget = max(criteria.limit * 10, 500)

        while yielded < criteria.limit and examined < examine_budget:
            body = await self._get(url, params=params)
            items = body.get("value") or []
            if not items:
                return

            for item in items:
                examined += 1
                summary = _summary_from_graph(item)
                if keywords:
                    haystack = f"{summary.subject} {summary.snippet}".lower()
                    if not any(k in haystack for k in keywords):
                        continue
                if not criteria.within_window(summary.received_at):
                    continue
                yield summary
                yielded += 1
                if yielded >= criteria.limit:
                    return

            next_link = body.get("@odata.nextLink")
            if not next_link:
                return
            # nextLink already carries every query parameter.
            url, params = next_link, None

    async def get_message(self, message_id: str) -> MessageDetail:
        msg = await self._get(
            f"{GRAPH_BASE}/me/messages/{message_id}",
            params={"$select": "id,subject,from,toRecipients,receivedDateTime,body,"
                               "bodyPreview,hasAttachments,internetMessageHeaders"},
        )
        detail = _detail_from_graph(msg)
        if msg.get("hasAttachments"):
            detail.attachments = await self.get_attachments(message_id)
        return detail

    async def get_attachments(self, message_id: str) -> List[AttachmentRef]:
        body = await self._get(
            f"{GRAPH_BASE}/me/messages/{message_id}/attachments",
            params={"$select": "id,name,contentType,size,isInline,contentId"},
        )
        refs: List[AttachmentRef] = []
        for item in body.get("value") or []:
            odata_type = item.get("@odata.type", "")
            if odata_type == _ITEM_ATTACHMENT:
                # An attached message (a forward). Graph exposes its own
                # attachments one level down; expanding it here keeps forwarded
                # statements reachable.
                refs.extend(await self._expand_item_attachment(message_id, item))
                continue
            if odata_type and odata_type != _FILE_ATTACHMENT:
                continue
            refs.append(AttachmentRef(
                ref=item.get("id", ""),
                filename=item.get("name", "") or "",
                mime_type=(item.get("contentType") or "").lower(),
                size=int(item.get("size") or 0),
                is_inline=bool(item.get("isInline")),
                content_id=item.get("contentId"),
            ))
        return refs

    async def _expand_item_attachment(self, message_id: str, item: dict) -> List[AttachmentRef]:
        item_id = item.get("id", "")
        try:
            nested = await self._get(
                f"{GRAPH_BASE}/me/messages/{message_id}/attachments/{item_id}",
                params={"$expand": "microsoft.graph.itemAttachment/item"},
            )
        except (MessageNotFound, ProviderUnavailable) as exc:
            logger.info("[Graph] could not expand item attachment %s: %s", item_id, exc.reason)
            return []

        inner = ((nested.get("item") or {}).get("attachments")) or []
        refs: List[AttachmentRef] = []
        for att in inner:
            if att.get("@odata.type") not in (None, _FILE_ATTACHMENT):
                continue
            refs.append(AttachmentRef(
                ref=f"{item_id}/{att.get('id', '')}",
                filename=att.get("name", "") or "",
                mime_type=(att.get("contentType") or "").lower(),
                size=int(att.get("size") or 0),
                inline_b64=att.get("contentBytes"),
                part_path=item_id,
            ))
        return refs

    async def download_attachment(self, message_id: str, attachment: AttachmentRef) -> AttachmentPayload:
        if attachment.inline_b64:
            data = base64.b64decode(attachment.inline_b64)
        else:
            body = await self._get(f"{GRAPH_BASE}/me/messages/{message_id}/attachments/{attachment.ref}")
            content = body.get("contentBytes")
            if not content:
                raise MessageNotFound(
                    f"Microsoft Graph returned no bytes for '{attachment.filename or attachment.ref}'.",
                    provider="microsoft",
                )
            data = base64.b64decode(content)

        if not data:
            raise MessageNotFound(
                f"Microsoft Graph returned an empty payload for '{attachment.filename}'.",
                provider="microsoft",
            )
        return AttachmentPayload(data=data, filename=attachment.filename,
                                 mime_type=attachment.mime_type)

    async def revoke_access(self) -> None:
        # No confidential-client revocation endpoint exists; the stored
        # credential is deleted by the caller, which is what stops access.
        return None


def _summary_from_graph(item: dict) -> MessageSummary:
    sender = ((item.get("from") or {}).get("emailAddress") or {})
    return MessageSummary(
        id=item.get("id", ""),
        subject=item.get("subject") or "",
        sender=sender.get("address", "") or "",
        sender_name=sender.get("name", "") or "",
        received_at=_parse_graph_datetime(item.get("receivedDateTime")),
        snippet=item.get("bodyPreview") or "",
        has_attachments=bool(item.get("hasAttachments")),
        raw={"internetMessageId": item.get("internetMessageId")},
    )


def _detail_from_graph(msg: dict) -> MessageDetail:
    sender = ((msg.get("from") or {}).get("emailAddress") or {})
    body = msg.get("body") or {}
    content = body.get("content") or ""
    content_type = (body.get("contentType") or "").lower()
    to_addresses = ", ".join(
        (r.get("emailAddress") or {}).get("address", "")
        for r in (msg.get("toRecipients") or [])
    )
    headers = {
        h.get("name", ""): h.get("value", "")
        for h in (msg.get("internetMessageHeaders") or [])
    }
    return MessageDetail(
        id=msg.get("id", ""),
        subject=msg.get("subject") or "",
        sender=sender.get("address", "") or "",
        sender_name=sender.get("name", "") or "",
        to=to_addresses,
        received_at=_parse_graph_datetime(msg.get("receivedDateTime")),
        body_text=content if content_type != "html" else (msg.get("bodyPreview") or ""),
        body_html=content if content_type == "html" else "",
        headers=headers,
    )
