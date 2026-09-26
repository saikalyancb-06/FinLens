"""Provider-neutral search criteria.

The scan engine describes *what* it is looking for; each connector decides how
to say that in its own dialect (Gmail query string, Graph ``$search``/``$filter``,
IMAP ``SEARCH`` keys). Keeping the vocabulary small is deliberate — anything a
provider cannot express server-side is re-applied locally by the engine, so the
result set is identical everywhere and only the amount of network traffic
differs.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import List, Optional


#: Keywords that make a message worth *looking at*. This is the coarse
#: server-side sieve only — the fine-grained multi-signal scoring lives in
#: app/statements/signals.py and runs over the full message. Deliberately
#: institution-agnostic: no bank name appears here.
DEFAULT_STATEMENT_KEYWORDS: List[str] = [
    "statement",
    "estatement",
    "e-statement",
    "account statement",
    "bank statement",
    "account summary",
    "transaction statement",
    "transaction history",
    "passbook",
    "monthly statement",
    "credit card statement",
    "loan statement",
    "portfolio statement",
    "contract note",
    "holding statement",
    "wallet statement",
]


@dataclass
class SearchCriteria:
    """What the scan engine wants from a mailbox.

    Attributes
    ----------
    keywords:
        Any-of match against subject/body. Empty means "no keyword restriction".
    require_attachment:
        Ask the provider for messages carrying attachments. The engine runs a
        second, keyword-only pass with this ``False`` so body-only statements
        (HTML tables, plain-text summaries) are not missed.
    since / until:
        Received-date window.
    limit:
        Hard cap on summaries returned, so a scan of a 200 000-message mailbox
        cannot run unbounded.
    page_size:
        Per-request page size hint.
    folders:
        Provider folder/label names to restrict to. Empty means "all mail".
    """

    keywords: List[str] = field(default_factory=lambda: list(DEFAULT_STATEMENT_KEYWORDS))
    require_attachment: bool = False
    since: Optional[_dt.datetime] = None
    until: Optional[_dt.datetime] = None
    limit: int = 200
    page_size: int = 50
    folders: List[str] = field(default_factory=list)

    def within_window(self, received_at: Optional[_dt.datetime]) -> bool:
        """Local re-check of the date window for providers that cannot filter."""
        if received_at is None:
            return True
        received = received_at
        if received.tzinfo is not None:
            received = received.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        if self.since is not None:
            since = self.since
            if since.tzinfo is not None:
                since = since.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            if received < since:
                return False
        if self.until is not None:
            until = self.until
            if until.tzinfo is not None:
                until = until.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            if received > until:
                return False
        return True

    @classmethod
    def default_scan(cls, days: int = 365, limit: int = 200) -> "SearchCriteria":
        return cls(
            since=_dt.datetime.utcnow() - _dt.timedelta(days=days),
            limit=limit,
        )
