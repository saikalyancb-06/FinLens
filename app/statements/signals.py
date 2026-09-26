"""Multi-signal scoring for "is this message about a financial statement?".

The rule this module exists to enforce: **no single signal decides anything**.
Not the sender, not the subject, not the filename. A statement from an
institution nobody has heard of, sent from a no-reply address on a shared
mail-service domain, with an attachment called ``document_2026_07.pdf``, still
has to be found. And ``statement.pdf`` from a bank's domain still has to be
rejected when it turns out to be a marketing brochure.

So each source of evidence contributes a weighted score, the score decides
whether a message is *worth opening*, and the actual verdict is made later by
``app.statements.classifier`` against the document's own content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Vocabulary
#
# Institution-agnostic by construction. Every phrase below describes the *shape*
# of a financial statement, not who sent it.
# ---------------------------------------------------------------------------

STRONG_STATEMENT_PHRASES: Tuple[str, ...] = (
    "account statement",
    "bank statement",
    "e-statement",
    "estatement",
    "transaction statement",
    "statement of account",
    "statement of transactions",
    "monthly statement",
    "quarterly statement",
    "credit card statement",
    "card statement",
    "loan statement",
    "portfolio statement",
    "holding statement",
    "contract note",
    "passbook",
    "account summary",
    "transaction history",
    "consolidated account statement",
    "wallet statement",
    "your statement",
)

WEAK_STATEMENT_PHRASES: Tuple[str, ...] = (
    "statement",
    "summary",
    "transactions",
    "e statement",
    "account activity",
    "statement period",
)

LEDGER_PHRASES: Tuple[str, ...] = (
    "opening balance",
    "closing balance",
    "available balance",
    "debit",
    "credit",
    "withdrawal",
    "deposit",
    "narration",
    "particulars",
    "value date",
    "transaction date",
    "cheque",
    "chq",
    "ref no",
    "reference no",
    "balance",
    "utr",
)

ACCOUNT_PHRASES: Tuple[str, ...] = (
    "account number",
    "account no",
    "a/c no",
    "a/c number",
    "acct no",
    "ending in",
    "card ending",
    "xxxx",
    "customer id",
    "ifsc",
    "iban",
    "sort code",
    "routing number",
)

PERIOD_PHRASES: Tuple[str, ...] = (
    "statement period",
    "period from",
    "for the period",
    "statement date",
    "from date",
    "to date",
    "billing period",
    "billing cycle",
)

#: Content that looks financial but is not a statement. Used to *lower* the
#: score, never to hard-reject on its own — a statement email can perfectly well
#: contain the word "offer" in a footer.
NEGATIVE_PHRASES: Tuple[str, ...] = (
    "tax invoice",
    "gst invoice",
    "order id",
    "order confirmation",
    "booking ref",
    "boarding pass",
    "e-ticket",
    "seat no",
    "convenience fee",
    "pre-approved",
    "apply now",
    "one time password",
    "otp",
    "verification code",
    "unsubscribe from marketing",
    "newsletter",
    "terms and conditions",
    "privacy policy",
    "job alert",
    "password reset",
)

#: Attachment extensions that can carry a statement.
STATEMENT_EXTENSIONS: Tuple[str, ...] = (".pdf", ".csv", ".xls", ".xlsx", ".xlsm", ".txt", ".ofx", ".qif")

#: MIME types that can carry a statement, checked *independently* of filename —
#: a bank that sends ``application/pdf`` named ``document`` with no extension is
#: still sending a PDF.
STATEMENT_MIME_TYPES: Tuple[str, ...] = (
    "application/pdf",
    "text/csv",
    "application/csv",
    "text/comma-separated-values",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/x-ofx",
    "text/plain",
    "application/octet-stream",  # generic: decided by content sniffing later
)

#: Attachments that never contain a statement table. Cheap early exit only.
NON_STATEMENT_EXTENSIONS: Tuple[str, ...] = (
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".ico", ".webp",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".exe", ".dll", ".msi", ".apk", ".dmg",
    ".mp3", ".mp4", ".mov", ".avi", ".wav",
    ".ics", ".vcf",
)

_AMOUNT_RE = re.compile(
    r"(?:(?:rs\.?|inr|usd|eur|gbp|aed|sgd|₹|\$|€|£)\s*)?"
    r"\d{1,3}(?:[, ]\d{2,3})*(?:\.\d{1,2})?",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(?:\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    r"|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-\s](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-\s]\d{2,4})\b",
    re.IGNORECASE,
)


@dataclass
class SignalScore:
    """Why a message was or was not treated as a statement candidate.

    Kept as a structured object rather than a bare float so the reason can be
    shown to the user and asserted on in tests — "score 0.62" tells nobody
    anything, "matched: subject phrase 'account statement', pdf attachment"
    does.
    """

    score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    matched: Dict[str, List[str]] = field(default_factory=dict)

    def add(self, weight: float, reason: str, group: str = "", terms: Optional[Iterable[str]] = None) -> None:
        self.score += weight
        self.reasons.append(reason)
        if group:
            self.matched.setdefault(group, []).extend(list(terms or []))

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons[:6])


def _found(haystack: str, phrases: Iterable[str]) -> List[str]:
    return [p for p in phrases if p in haystack]


def score_message_metadata(
    subject: str,
    sender: str,
    snippet: str = "",
    attachment_filenames: Optional[Iterable[str]] = None,
    has_attachments: bool = False,
) -> SignalScore:
    """Score a message from list-time metadata only — no body fetched yet.

    This is the sieve that decides which of several hundred summaries are worth
    a full download. It is intentionally generous: a false positive costs one
    message fetch, a false negative loses the user a statement.
    """
    result = SignalScore()
    subject_l = (subject or "").lower()
    snippet_l = (snippet or "").lower()
    combined = f"{subject_l}\n{snippet_l}"

    strong = _found(subject_l, STRONG_STATEMENT_PHRASES)
    if strong:
        result.add(0.55, f"subject names a statement ({strong[0]!r})", "subject", strong)
    else:
        weak = _found(subject_l, WEAK_STATEMENT_PHRASES)
        if weak:
            result.add(0.25, f"subject hints at a statement ({weak[0]!r})", "subject", weak)

    body_strong = _found(snippet_l, STRONG_STATEMENT_PHRASES)
    if body_strong and not strong:
        result.add(0.30, f"preview names a statement ({body_strong[0]!r})", "preview", body_strong)

    ledger = _found(combined, LEDGER_PHRASES)
    if ledger:
        result.add(min(0.20, 0.05 * len(ledger)), f"ledger vocabulary in preview ({', '.join(ledger[:3])})",
                   "ledger", ledger)

    account = _found(combined, ACCOUNT_PHRASES)
    if account:
        result.add(0.15, "references an account identifier", "account", account)

    period = _found(combined, PERIOD_PHRASES)
    if period:
        result.add(0.15, "references a statement period", "period", period)

    names = [str(n or "").lower() for n in (attachment_filenames or [])]
    if names:
        statement_like = [n for n in names if n.endswith(STATEMENT_EXTENSIONS)]
        if statement_like:
            result.add(0.25, f"carries a parsable attachment ({statement_like[0]})",
                       "attachment", statement_like)
        # Filename wording is a hint and nothing more; content decides.
        worded = [n for n in names if any(p in n for p in ("statement", "stmt", "passbook", "summary"))]
        if worded:
            result.add(0.10, "attachment filename mentions a statement", "attachment_name", worded)
    elif has_attachments:
        result.add(0.15, "message carries attachments")

    negatives = _found(combined, NEGATIVE_PHRASES)
    if negatives:
        result.add(-0.30, f"looks like a non-statement notice ({negatives[0]!r})",
                   "negative", negatives)

    sender_l = (sender or "").lower()
    # A financial-sounding sender is a *bonus*, never a requirement, and it is
    # matched on generic role words rather than a list of banks — the whole
    # point is that an unknown institution scores the same as a known one.
    if any(token in sender_l for token in
           ("statement", "estatement", "e-statement", "alerts", "no-reply", "noreply",
            "bank", "card", "finance", "wealth", "broker", "invest")):
        result.add(0.08, "sender address suggests an automated financial notice", "sender", [sender_l])

    return result


def score_message_content(text: str) -> SignalScore:
    """Score the full text of a message body once it has been fetched."""
    result = SignalScore()
    lowered = (text or "").lower()
    if not lowered.strip():
        return result

    strong = _found(lowered, STRONG_STATEMENT_PHRASES)
    if strong:
        result.add(0.40, f"body names a statement ({strong[0]!r})", "body", strong)

    ledger = _found(lowered, LEDGER_PHRASES)
    if len(ledger) >= 3:
        result.add(0.30, f"body carries ledger column vocabulary ({', '.join(ledger[:4])})",
                   "ledger", ledger)
    elif ledger:
        result.add(0.10, "body mentions ledger terms", "ledger", ledger)

    if _found(lowered, PERIOD_PHRASES):
        result.add(0.15, "body states a statement period")
    if _found(lowered, ACCOUNT_PHRASES):
        result.add(0.10, "body references an account identifier")

    dates = _DATE_RE.findall(text or "")
    amounts = _AMOUNT_RE.findall(text or "")
    if len(dates) >= 3 and len(amounts) >= 6:
        result.add(0.25, f"body contains {len(dates)} dates and {len(amounts)} amounts — table-like")

    negatives = _found(lowered, NEGATIVE_PHRASES)
    if negatives:
        result.add(-0.25, f"body reads as a non-statement notice ({negatives[0]!r})",
                   "negative", negatives)

    return result


def attachment_is_candidate(filename: str, mime_type: str, size: int = 0) -> Tuple[bool, str]:
    """Decide whether an attachment is worth downloading and inspecting.

    Both filename and MIME type are consulted, and either alone is enough. The
    old implementation trusted the filename extension exclusively, so a bank
    that sends ``application/pdf`` with the name ``document`` was invisible and
    a ``.pdf`` holding a cinema ticket was downloaded and treated as a statement.
    """
    name = (filename or "").strip().lower()
    mime = (mime_type or "").strip().lower().split(";")[0]

    if name.endswith(NON_STATEMENT_EXTENSIONS):
        return False, f"'{filename}' is a {name.rsplit('.', 1)[-1]} file, which cannot hold a statement table"
    if mime.startswith(("image/", "video/", "audio/")):
        return False, f"'{filename or mime}' is {mime}, which cannot hold a statement table"

    if name.endswith(STATEMENT_EXTENSIONS):
        return True, f"'{filename}' has a parsable document extension"
    if mime in STATEMENT_MIME_TYPES:
        return True, f"'{filename or 'attachment'}' is {mime}, which may hold a statement"

    # Unknown type with plausible size: let the content sniffer decide rather
    # than discarding it here.
    if 0 < size <= 25 * 1024 * 1024 and not name.endswith(NON_STATEMENT_EXTENSIONS):
        return True, f"'{filename or 'attachment'}' has an unrecognised type; content will decide"
    return False, f"'{filename or mime}' is not a document this system can read"


#: A message scoring at or above this is fetched in full and inspected.
CANDIDATE_THRESHOLD = 0.30

#: Below this, a fully-read message is not worth downloading attachments for
#: *unless* it carries a document-shaped attachment, which is checked separately.
CONTENT_THRESHOLD = 0.25
