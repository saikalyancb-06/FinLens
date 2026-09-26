"""Compatibility shim for the original Gmail-only client.

Gmail access now goes through ``app.mailbox.gmail.GmailProvider``, which
implements the same ``EmailProvider`` interface as the Microsoft Graph and IMAP
connectors. This module forwards the two methods the old client exposed so any
remaining caller keeps working; the discovery engine does not use it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.mailbox.criteria import SearchCriteria
from app.mailbox.gmail import GmailProvider, decode_b64url  # noqa: F401  (re-exported)
from app.mailbox.types import AttachmentRef

logger = logging.getLogger(__name__)


class GmailService:
    """Synchronous facade over :class:`~app.mailbox.gmail.GmailProvider`."""

    def __init__(self, access_token: str, refresh_token: Optional[str] = None):
        self.access_token = access_token
        self.provider = GmailProvider(access_token=access_token, refresh_token=refresh_token)

    async def list_messages(self, query: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """Return message summaries as plain dictionaries.

        ``query`` is accepted for signature compatibility and treated as a
        keyword list; provider-specific query syntax is no longer part of the
        interface, because the same call has to work against Graph and IMAP.
        """
        criteria = SearchCriteria(limit=limit)
        if query:
            criteria.keywords = [term for term in query.split() if term]
        out: List[Dict[str, Any]] = []
        async for summary in self.provider.search_messages(criteria):
            out.append({
                "id": summary.id,
                "subject": summary.subject,
                "sender": summary.sender,
                "received_at": summary.received_at.isoformat() if summary.received_at else None,
                "has_attachments": summary.has_attachments,
                "attachment_filenames": summary.attachment_filenames,
            })
        return out

    async def download_attachment(
        self,
        message_id: str,
        attachment_id: Optional[str] = None,
        body_data: Optional[str] = None,
        expected_filename: str = "",
        reported_size: int = 0,
    ) -> bytes:
        payload = await self.provider.download_attachment(
            message_id,
            AttachmentRef(ref=attachment_id or "", filename=expected_filename,
                          size=reported_size, inline_b64=body_data),
        )
        return payload.data

    def close(self) -> None:
        try:
            asyncio.get_event_loop().run_until_complete(self.provider.disconnect())
        except Exception:
            pass


__all__ = ["GmailService", "GmailProvider", "decode_b64url"]
