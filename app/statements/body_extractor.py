"""Recover statements that arrived as the email body rather than as a file.

Not every statement is a PDF attachment. Institutions routinely send the
transaction grid as an HTML table inside the message, or as a fixed-width
plain-text block. Those are statements by every meaningful definition and the
transactions in them are exactly what the user wants, so they are extracted into
a CSV and handed to the same parsing pipeline an attachment would go through.

External links are deliberately *not* followed. An email saying "log in to
download your statement" is recorded as such and left alone: fetching arbitrary
URLs found in mail, or submitting credentials to them, is not something this
system does.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.mailbox.types import MessageDetail
from app.statements.classifier import TableShape, analyse_rows
from app.statements.html_text import html_tables, html_to_text, text_tables

logger = logging.getLogger(__name__)

#: A body table smaller than this is a summary box ("Closing balance: X"), not a
#: transaction ledger.
MIN_LEDGER_ROWS = 2

_STATEMENT_LINK_HINTS = (
    "download your statement", "view your statement", "click here to download",
    "download statement", "access your statement", "log in to view",
    "login to view", "view statement", "netbanking to download",
)


@dataclass
class BodyStatement:
    """A transaction grid found in a message body."""

    rows: List[List[str]] = field(default_factory=list)
    shape: TableShape = field(default_factory=TableShape)
    source: str = ""          # "html" or "text"

    @property
    def is_ledger(self) -> bool:
        return self.shape.is_ledger and len(self.rows) >= MIN_LEDGER_ROWS

    def to_csv_bytes(self) -> bytes:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerows(self.rows)
        return buffer.getvalue().encode("utf-8")


def extract_body_statement(detail: MessageDetail) -> Optional[BodyStatement]:
    """Find the transaction grid in a message body, if there is one.

    HTML is tried first because it carries explicit cell boundaries; the
    plain-text alternative part of the same message is a fallback for senders
    that only ship text.
    """
    candidates: List[BodyStatement] = []

    for table in html_tables(detail.body_html or ""):
        cleaned = _clean(table)
        if len(cleaned) < MIN_LEDGER_ROWS:
            continue
        shape = analyse_rows(cleaned)
        if shape.is_ledger:
            candidates.append(BodyStatement(rows=cleaned, shape=shape, source="html"))

    if not candidates:
        text = detail.body_text or html_to_text(detail.body_html or "")
        for table in text_tables(text):
            cleaned = _clean(table)
            if len(cleaned) < MIN_LEDGER_ROWS:
                continue
            shape = analyse_rows(cleaned)
            if shape.is_ledger:
                candidates.append(BodyStatement(rows=cleaned, shape=shape, source="text"))

    if not candidates:
        return None
    # The grid with the most transaction rows: layout tables and header/footer
    # blocks are small, the ledger is not.
    return max(candidates, key=lambda c: (c.shape.data_rows, len(c.rows)))


def _clean(table: List[List[str]]) -> List[List[str]]:
    rows: List[List[str]] = []
    for row in table:
        cells = [re.sub(r"\s+", " ", (c or "")).strip() for c in row]
        if any(cells):
            rows.append(cells)
    return rows


def mentions_external_statement_link(detail: MessageDetail) -> bool:
    """True when the message points at a statement hosted somewhere else.

    Reported to the user so they know why a message that clearly concerns a
    statement produced no document, rather than silently dropping it.
    """
    haystack = f"{detail.subject}\n{detail.body_text}\n{html_to_text(detail.body_html or '')}".lower()
    return any(hint in haystack for hint in _STATEMENT_LINK_HINTS)
