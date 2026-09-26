"""A fixture mailbox, used when ``DEMO_MODE`` is on or a demo token is stored.

Demo mode exists so the product can be walked through — and the test suite run —
without live Google or Microsoft credentials. Implementing it as a *provider*
rather than as ``if demo:`` branches scattered through the Gmail client means the
demo path exercises the same discovery, classification and ingestion code as a
real mailbox; only the transport is substituted.

The fixture mailbox is deliberately awkward on purpose:

* one statement from an institution that appears in no lookup table;
* one statement whose filename gives nothing away (``monthly_document.pdf``);
* one statement delivered as an HTML table in the body, with no attachment;
* one invoice that must be rejected despite arriving from a bank-ish sender;
* the same statement twice, in two different messages, to prove deduplication.
"""
from __future__ import annotations

import base64
import datetime as _dt
from typing import AsyncIterator, Dict, List, Optional

from app.mailbox.base import EmailProvider
from app.mailbox.criteria import SearchCriteria
from app.mailbox.errors import MessageNotFound
from app.mailbox.types import (
    AttachmentPayload,
    AttachmentRef,
    MailboxAccountInfo,
    MessageDetail,
    MessageSummary,
)

_NOW = _dt.datetime(2026, 8, 1, 9, 30)

UNKNOWN_BANK_CSV = (
    "Sahyadri Sahakari Bank Ltd - Account Statement\n"
    "Account Statement for A/C No: 001234567890\n"
    "Statement Period: 01/07/2026 to 31/07/2026\n"
    "Date,Narration,Withdrawal,Deposit,Balance\n"
    "01/07/2026,SALARY CREDIT TECH CORP,0.00,75000.00,125000.00\n"
    "02/07/2026,SWIGGY BANGALORE ORDER,450.00,0.00,124550.00\n"
    "03/07/2026,ELECTRICITY UTILITY BILL,1200.00,0.00,123350.00\n"
    "05/07/2026,UPI PAYMENT TO LANDLORD,25000.00,0.00,98350.00\n"
)

KNOWN_BANK_CSV = (
    "HDFC Bank Account Statement\n"
    "Account Number: 50200011112222\n"
    "Statement Period: 01/07/2026 to 31/07/2026\n"
    "Date,Particulars,Debit,Credit,Balance\n"
    "05/07/2026,BHARATPE PAYOUT RECEIVED,0.00,12500.00,135850.00\n"
    "06/07/2026,STARBUCKS COFFEE STORE,350.00,0.00,135500.00\n"
    "08/07/2026,NEFT TRANSFER TO VENDOR,8000.00,0.00,127500.00\n"
)

INVOICE_TEXT = (
    "EatSure Order CRN-221321644\n"
    "Tax Invoice  GSTIN: 27AAAAA0000A1Z5\n"
    "Food Items and Delivery Charges\n"
    "Order Date: 11/08/2026   Total: 564.00\n"
)

BODY_STATEMENT_HTML = """
<html><body>
  <p>Dear Customer,</p>
  <p>Here is your account summary for the statement period 01/07/2026 to 31/07/2026
     for account ending in 8891.</p>
  <table>
    <tr><th>Date</th><th>Description</th><th>Debit</th><th>Credit</th><th>Balance</th></tr>
    <tr><td>04/07/2026</td><td>CARD PAYMENT AMAZON</td><td>1899.00</td><td>0.00</td><td>44101.00</td></tr>
    <tr><td>07/07/2026</td><td>INTEREST CREDITED</td><td>0.00</td><td>312.00</td><td>44413.00</td></tr>
    <tr><td>12/07/2026</td><td>ATM CASH WITHDRAWAL</td><td>3000.00</td><td>0.00</td><td>41413.00</td></tr>
  </table>
  <p>Opening balance 46000.00, closing balance 41413.00.</p>
</body></html>
"""


def _pdf_bytes(text: str) -> bytes:
    """A minimal but genuinely valid single-page PDF carrying ``text``.

    Built by hand rather than with a PDF library so the fixture has no extra
    dependency and its byte layout is stable across runs.
    """
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    lines = escaped.splitlines() or [""]
    content_lines = ["BT", "/F1 10 Tf", "50 750 Td", "12 TL"]
    for line in lines:
        content_lines.append(f"({line}) Tj")
        content_lines.append("T*")
    content_lines.append("ET")
    stream = "\n".join(content_lines).encode("latin-1", errors="replace")

    objects: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF".encode()
    return bytes(out)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


#: The fixture mailbox. Ordered oldest first; the engine sees them newest first
#: after scoring, and the ordering below decides the order rows are written.
DEMO_MESSAGES: List[Dict] = [
    {
        "id": "demo_msg_invoice",
        "subject": "Your EatSure order invoice",
        "sender": "Bank of Snacks <billing@eatsure-notifications.com>",
        "received_at": _NOW - _dt.timedelta(days=9),
        "body_text": "Thanks for your order. Your tax invoice is attached.",
        "attachments": [
            {"filename": "EatSure_Invoice.pdf", "mime_type": "application/pdf",
             "data": _pdf_bytes(INVOICE_TEXT)},
        ],
    },
    {
        "id": "demo_msg_body_only",
        "subject": "Your monthly account summary",
        "sender": "statements@some-credit-union.example",
        "received_at": _NOW - _dt.timedelta(days=6),
        "body_html": BODY_STATEMENT_HTML,
        "attachments": [],
    },
    {
        "id": "demo_msg_unknown_bank",
        "subject": "Your monthly document is ready",
        "sender": "no-reply@sahyadri-sahakari.example",
        "received_at": _NOW - _dt.timedelta(days=4),
        "body_text": "Please find your account statement for the period attached.",
        "attachments": [
            # Filename says nothing; the content is what identifies it.
            {"filename": "monthly_document.csv", "mime_type": "application/octet-stream",
             "data": UNKNOWN_BANK_CSV.encode()},
        ],
    },
    {
        "id": "demo_msg_forwarded_duplicate",
        "subject": "Fwd: Your monthly document is ready",
        "sender": "Someone Else <colleague@example.com>",
        "received_at": _NOW - _dt.timedelta(days=3),
        "body_text": "Forwarding the account statement you asked for.",
        "attachments": [
            # Identical bytes under a different name in a different message:
            # must be recognised as the same document.
            {"filename": "statement_copy.csv", "mime_type": "text/csv",
             "data": UNKNOWN_BANK_CSV.encode()},
        ],
    },
    {
        "id": "demo_msg_known_bank",
        "subject": "HDFC Bank E-Statement for Account ending 2222",
        "sender": "estatement@hdfcbank.net",
        "received_at": _NOW - _dt.timedelta(days=1),
        "body_text": "Your e-statement is attached.",
        "attachments": [
            {"filename": "HDFC_Statement_Jul2026.csv", "mime_type": "text/csv",
             "data": KNOWN_BANK_CSV.encode()},
        ],
    },
]


class DemoProvider(EmailProvider):
    """Serves ``DEMO_MESSAGES`` through the ordinary provider interface."""

    key = "demo"
    label = "Demo mailbox"
    auth_type = "oauth"

    def __init__(self, email_address: str = "user.bank.statements@gmail.com",
                 provider: str = "gmail", **_ignored):
        self.email_address = email_address
        self.provider = provider

    async def authenticate(self) -> None:
        return None

    async def refresh_authentication(self) -> Optional[dict]:
        return {"access_token": "demo_refreshed_access_token", "expires_in": 3600}

    async def get_account_info(self) -> MailboxAccountInfo:
        return MailboxAccountInfo(
            email_address=self.email_address,
            provider_account_id=f"demo-{self.provider}",
            display_name="Demo mailbox",
            provider=self.provider,
        )

    async def search_messages(self, criteria: SearchCriteria) -> AsyncIterator[MessageSummary]:
        keywords = [k.lower() for k in criteria.keywords]
        count = 0
        for message in reversed(DEMO_MESSAGES):
            if count >= criteria.limit:
                return
            attachments = message.get("attachments") or []
            if criteria.require_attachment and not attachments:
                continue
            if not criteria.within_window(message["received_at"]):
                continue
            haystack = " ".join([
                message.get("subject", ""),
                message.get("body_text", ""),
                message.get("body_html", ""),
            ]).lower()
            if keywords and not any(k in haystack for k in keywords):
                continue
            yield MessageSummary(
                id=message["id"],
                subject=message.get("subject", ""),
                sender=message.get("sender", ""),
                received_at=message["received_at"],
                snippet=(message.get("body_text") or "")[:200],
                has_attachments=bool(attachments),
                attachment_filenames=[a["filename"] for a in attachments],
            )
            count += 1

    def _find(self, message_id: str) -> Dict:
        for message in DEMO_MESSAGES:
            if message["id"] == message_id:
                return message
        raise MessageNotFound(f"No demo message with id '{message_id}'.", provider="demo")

    async def get_message(self, message_id: str) -> MessageDetail:
        message = self._find(message_id)
        return MessageDetail(
            id=message["id"],
            subject=message.get("subject", ""),
            sender=message.get("sender", ""),
            sender_name=(message.get("sender", "").split("<")[0].strip()),
            received_at=message["received_at"],
            body_text=message.get("body_text", ""),
            body_html=message.get("body_html", ""),
            attachments=[
                AttachmentRef(
                    ref=f"{message['id']}::{index}",
                    filename=attachment["filename"],
                    mime_type=attachment["mime_type"],
                    size=len(attachment["data"]),
                    part_path=str(index),
                )
                for index, attachment in enumerate(message.get("attachments") or [])
            ],
        )

    async def download_attachment(self, message_id: str, attachment: AttachmentRef) -> AttachmentPayload:
        message = self._find(message_id)
        try:
            index = int((attachment.ref or "").rsplit("::", 1)[-1])
            data = (message.get("attachments") or [])[index]["data"]
        except (ValueError, IndexError, KeyError) as exc:
            raise MessageNotFound(
                f"Demo attachment '{attachment.ref}' does not exist.", provider="demo"
            ) from exc
        return AttachmentPayload(data=data, filename=attachment.filename,
                                 mime_type=attachment.mime_type)
