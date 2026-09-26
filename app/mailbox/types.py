"""Provider-neutral value objects.

Everything above ``app.mailbox`` — the discovery engine, the classifier, the
routes, the UI — speaks only in these types. That is the whole point of the
package: adding a fourth provider must not require touching a single line of
statement-detection code.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MailboxAccountInfo:
    """Who the mailbox belongs to, as the provider reports it."""

    email_address: str
    #: provider-side stable identifier (Google ``sub``, Graph ``id``, or the
    #: address itself for IMAP where no such identifier exists)
    provider_account_id: str
    display_name: Optional[str] = None
    provider: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MessageSummary:
    """Cheap metadata for one message, obtained without downloading its body.

    A summary is deliberately thin: the scan engine scores hundreds of these
    before deciding which handful deserve a full fetch.
    """

    id: str
    subject: str = ""
    sender: str = ""
    sender_name: str = ""
    received_at: Optional[_dt.datetime] = None
    snippet: str = ""
    has_attachments: bool = False
    #: filenames if the provider volunteers them at list time (Graph and IMAP
    #: ENVELOPE/BODYSTRUCTURE do; Gmail's list endpoint does not)
    attachment_filenames: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def sender_domain(self) -> str:
        addr = self.sender or ""
        if "<" in addr and ">" in addr:
            addr = addr[addr.rfind("<") + 1: addr.rfind(">")]
        return addr.rsplit("@", 1)[-1].strip().lower() if "@" in addr else ""

    @property
    def sender_address(self) -> str:
        addr = (self.sender or "").strip()
        if "<" in addr and ">" in addr:
            addr = addr[addr.rfind("<") + 1: addr.rfind(">")]
        return addr.strip().lower()


@dataclass
class AttachmentRef:
    """A candidate attachment discovered while walking a message's MIME tree.

    ``ref`` is whatever the provider needs to fetch the bytes later. It is opaque
    to every caller: Gmail stores an ``attachmentId``, Graph an attachment ``id``,
    IMAP the MIME part path (``"2.1"``). ``inline_b64`` is set when the provider
    already handed us the payload during the detail fetch, which lets the scan
    skip a network round-trip.
    """

    ref: str
    filename: str = ""
    mime_type: str = ""
    size: int = 0
    is_inline: bool = False
    content_id: Optional[str] = None
    #: base64 payload if it arrived with the message detail
    inline_b64: Optional[str] = None
    #: dotted path through the MIME tree, useful for nested/forwarded messages
    part_path: str = ""


@dataclass
class MessageDetail:
    """A fully fetched message: headers, decoded bodies, and attachment refs."""

    id: str
    subject: str = ""
    sender: str = ""
    sender_name: str = ""
    to: str = ""
    received_at: Optional[_dt.datetime] = None
    body_text: str = ""
    body_html: str = ""
    attachments: List[AttachmentRef] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def sender_domain(self) -> str:
        return MessageSummary(id=self.id, sender=self.sender).sender_domain

    @property
    def sender_address(self) -> str:
        return MessageSummary(id=self.id, sender=self.sender).sender_address

    def combined_text(self, limit: int = 40_000) -> str:
        """Subject + plain body + de-tagged HTML, for keyword scoring."""
        from app.statements.html_text import html_to_text

        parts = [self.subject or "", self.body_text or ""]
        if self.body_html:
            parts.append(html_to_text(self.body_html))
        return "\n".join(p for p in parts if p)[:limit]


@dataclass
class AttachmentPayload:
    """Downloaded attachment bytes plus the metadata needed to store them."""

    data: bytes
    filename: str = ""
    mime_type: str = ""

    def __len__(self) -> int:  # pragma: no cover - convenience only
        return len(self.data)
