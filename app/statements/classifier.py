"""Decide what a downloaded document actually is, from its own content.

The question is answered in two parts:

1. *Is this financial at all?* — answered by structure: does the document
   contain a ledger, i.e. repeated rows of (date, description, money movement)?
   A brochure that says "statement" forty times has no ledger; a statement from
   an institution nobody has heard of has one.
2. *What kind of financial document?* — answered by vocabulary specific to each
   kind (card statements talk about minimum amount due, loan statements about
   EMI and principal outstanding, broker statements about ISIN and quantity).

The institution's name is deliberately *not* an input to either question. A
statement is a statement whether or not we can name the bank that sent it, and
step 2 of the previous implementation — reject anything whose sender domain was
not in a hardcoded list — is precisely the behaviour being removed.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.statements.document_text import DocumentContent, read_document
from app.statements.institutions import UNKNOWN, InstitutionMatch, identify_institution

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Document types
# ---------------------------------------------------------------------------
BANK_STATEMENT = "BANK_STATEMENT"
CREDIT_CARD_STATEMENT = "CREDIT_CARD_STATEMENT"
LOAN_STATEMENT = "LOAN_STATEMENT"
BROKER_STATEMENT = "BROKER_STATEMENT"
INVESTMENT_STATEMENT = "INVESTMENT_STATEMENT"
WALLET_STATEMENT = "WALLET_STATEMENT"
OTHER_FINANCIAL_DOCUMENT = "OTHER_FINANCIAL_DOCUMENT"
NOT_FINANCIAL = "NOT_FINANCIAL"

#: Types whose rows are transactions the ingestion pipeline can consume.
TRANSACTIONAL_TYPES = {
    BANK_STATEMENT,
    CREDIT_CARD_STATEMENT,
    LOAN_STATEMENT,
    BROKER_STATEMENT,
    INVESTMENT_STATEMENT,
    WALLET_STATEMENT,
}

# ---- legacy verdict codes, kept because the existing API and UI key off them --
LEGACY_CONFIRMED = "BANK_STATEMENT_CONFIRMED"
LEGACY_NOT_STATEMENT = "NOT_BANK_STATEMENT"
LEGACY_UNSUPPORTED = "UNSUPPORTED_ATTACHMENT"
LEGACY_PASSWORD_REQUIRED = "PDF_PASSWORD_REQUIRED"
LEGACY_PASSWORD_INVALID = "PASSWORD_INVALID"


# ---------------------------------------------------------------------------
# Column vocabulary. Deliberately broad: no two institutions name their columns
# the same way, and the normaliser downstream is what maps them onto the
# application's schema — this only has to recognise that the columns exist.
# ---------------------------------------------------------------------------
DATE_HEADERS = (
    "date", "txn date", "transaction date", "value date", "posting date",
    "tran date", "trans date", "booking date", "entry date", "dated", "day",
)
DESCRIPTION_HEADERS = (
    "description", "narration", "particulars", "details", "transaction details",
    "remarks", "reference", "narrative", "transaction remarks", "merchant",
    "payee", "counterparty", "transaction", "chq/ref no",
)
DEBIT_HEADERS = (
    "debit", "withdrawal", "withdrawals", "dr", "dr amount", "debit amount",
    "paid out", "money out", "withdrawal amt", "withdrawal (dr)", "spent",
    "purchase", "outflow",
)
CREDIT_HEADERS = (
    "credit", "deposit", "deposits", "cr", "cr amount", "credit amount",
    "paid in", "money in", "deposit amt", "deposit (cr)", "received", "inflow",
)
AMOUNT_HEADERS = (
    "amount", "amount (inr)", "transaction amount", "amt", "value", "txn amount",
    "amount in inr", "gross amount", "net amount",
)
BALANCE_HEADERS = (
    "balance", "closing balance", "running balance", "available balance",
    "balance (inr)", "bal", "balance amount", "ledger balance",
)
TYPE_HEADERS = ("type", "dr/cr", "cr/dr", "debit/credit", "transaction type", "dr cr")

# ---- kind-specific vocabulary ------------------------------------------------
CARD_TERMS = (
    "credit card statement", "card statement", "minimum amount due", "total amount due",
    "payment due date", "statement of your credit card", "credit limit",
    "available credit limit", "reward points", "card number ending",
    "billing cycle", "finance charges", "cash limit",
)
LOAN_TERMS = (
    "loan account", "loan statement", "emi", "equated monthly", "principal outstanding",
    "interest outstanding", "loan amount", "repayment schedule", "tenure",
    "disbursement", "outstanding principal", "amortisation", "amortization",
)
BROKER_TERMS = (
    "contract note", "isin", "trade date", "settlement", "brokerage", "exchange order",
    "nse", "bse", "securities transaction tax", "stt", "demat", "quantity", "scrip",
    "buy/sell", "trade id",
)
INVESTMENT_TERMS = (
    "portfolio statement", "folio", "folio no", "nav", "units", "mutual fund",
    "consolidated account statement", "sip", "redemption", "dividend reinvest",
    "scheme name", "holding statement", "market value",
)
WALLET_TERMS = (
    "wallet statement", "wallet balance", "wallet transactions", "paytm wallet",
    "prepaid instrument", "load money", "wallet id",
)
BANK_TERMS = (
    "account statement", "bank statement", "statement of account", "savings account",
    "current account", "ifsc", "micr", "account branch", "passbook",
    "opening balance", "closing balance", "cheque", "neft", "imps", "rtgs", "upi",
)

NON_FINANCIAL_TERMS = (
    "tax invoice", "gst invoice", "order id", "order crn", "booking ref",
    "boarding pass", "e-ticket", "seat no", "convenience fee", "cinema",
    "restaurant", "food items", "delivery charges", "one time password",
    "verification code", "job alert", "newsletter", "unsubscribe",
    "terms and conditions", "privacy policy", "policy schedule",
)

CURRENCY_SIGNS: Dict[str, str] = {
    "₹": "INR", "rs.": "INR", "rs ": "INR", "inr": "INR",
    "$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
    "aed": "AED", "sgd": "SGD", "aud": "AUD", "cad": "CAD", "jpy": "JPY",
    "chf": "CHF", "hkd": "HKD", "zar": "ZAR",
}

_DATE_TOKEN = re.compile(
    r"\b(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    r"|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-\s](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-\s,]*\d{2,4})\b",
    re.IGNORECASE,
)
_MONEY_TOKEN = re.compile(r"-?\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|-?\d+\.\d{2}")

_ACCOUNT_PATTERNS = (
    re.compile(r"(?:account|a\s*/\s*c|acct|acc)\.?\s*(?:no\.?|number|num|#)?\s*[:\-]?\s*"
               r"([xX\*]{0,12}\d{4,20})", re.IGNORECASE),
    re.compile(r"(?:card|account)\s*(?:number\s*)?ending(?:\s+in|\s+with)?\s*[:\-]?\s*"
               r"([xX\*]{0,12}\d{4,8})", re.IGNORECASE),
    re.compile(r"\b((?:[xX\*]{4,}[\s-]?){1,4}\d{4})\b"),
    re.compile(r"(?:iban)\s*[:\-]?\s*([A-Z]{2}\d{2}[A-Z0-9]{10,30})", re.IGNORECASE),
)

_PERIOD_PATTERNS = (
    re.compile(r"statement\s+period\s*[:\-]?\s*(.{0,60})", re.IGNORECASE),
    re.compile(r"(?:for\s+the\s+)?period\s*[:\-]?\s*(?:from\s*)?(.{0,60})", re.IGNORECASE),
    re.compile(r"from\s+(.{0,25}?)\s+to\s+(.{0,25})", re.IGNORECASE),
    re.compile(r"billing\s+(?:period|cycle)\s*[:\-]?\s*(.{0,60})", re.IGNORECASE),
)

_DATE_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d",
    "%d/%m/%y", "%d-%m-%y", "%m/%d/%Y", "%d %b %Y", "%d-%b-%Y",
    "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d %b %y", "%d-%b-%y",
)


@dataclass
class TableShape:
    """What a document's grid looks like, if it has one."""

    has_date: bool = False
    has_description: bool = False
    has_debit_credit: bool = False
    has_amount: bool = False
    has_balance: bool = False
    header_row: Optional[List[str]] = None
    data_rows: int = 0
    columns: Dict[str, str] = field(default_factory=dict)

    @property
    def is_ledger(self) -> bool:
        """Does this document contain a transaction ledger?

        Dates plus a money column are mandatory. How many rows are then required
        depends on how much else was recognised:

        * with a recognisable header row, or ledger vocabulary such as a
          narration or balance column, **one** transaction is enough. A statement
          for a quiet month legitimately holds a single line, and demanding two
          silently rejected it.
        * with neither — an unlabelled grid we are reading purely by shape —
          three rows are required, so that an invoice's "date / item / total"
          block is not mistaken for a ledger.

        Description is not itself mandatory: several institutions ship a
        date/amount/balance grid with the narration in a merged cell.
        """
        money = self.has_debit_credit or self.has_amount
        if not (self.has_date and money):
            return False
        recognised = (self.header_row is not None or self.has_description
                      or self.has_balance or self.has_debit_credit)
        return self.data_rows >= (1 if recognised else 3)


@dataclass
class DocumentClassification:
    """The verdict, everything that supports it, and everything derived from it."""

    document_type: str = NOT_FINANCIAL
    is_financial: bool = False
    confidence: float = 0.0
    reason: str = ""
    institution: InstitutionMatch = field(default_factory=InstitutionMatch)
    account_identifier: Optional[str] = None
    period_start: Optional[_dt.date] = None
    period_end: Optional[_dt.date] = None
    currency: Optional[str] = None
    table: TableShape = field(default_factory=TableShape)
    content_kind: str = ""
    password_required: bool = False
    password_invalid: bool = False
    error: Optional[str] = None

    @property
    def is_transactional(self) -> bool:
        return self.document_type in TRANSACTIONAL_TYPES

    @property
    def legacy_classification(self) -> str:
        """Map onto the older verdict vocabulary the API and UI still speak."""
        if self.password_required:
            return LEGACY_PASSWORD_REQUIRED
        if self.password_invalid:
            return LEGACY_PASSWORD_INVALID
        if self.document_type == NOT_FINANCIAL and self.content_kind in ("binary", "unknown", ""):
            return LEGACY_UNSUPPORTED
        if self.is_transactional:
            return LEGACY_CONFIRMED
        return LEGACY_NOT_STATEMENT

    @property
    def institution_name(self) -> str:
        return self.institution.name or UNKNOWN


# ---------------------------------------------------------------------------
# Table analysis
# ---------------------------------------------------------------------------

def _normalise_header(cell: str) -> str:
    return re.sub(r"[^a-z0-9/ ]+", " ", (cell or "").lower()).strip()


def _header_matches(cell: str, vocabulary: Tuple[str, ...]) -> bool:
    normalised = _normalise_header(cell)
    if not normalised:
        return False
    return any(normalised == term or normalised.startswith(term + " ") or term == normalised
               for term in vocabulary) or any(
        term in normalised for term in vocabulary if len(term) > 4
    )


def analyse_rows(rows: List[List[str]]) -> TableShape:
    """Find the header row and work out which columns a grid carries."""
    shape = TableShape()
    if not rows:
        return shape

    best_index = -1
    best_hits = 0
    # Scan the first 25 rows: statements routinely carry a bank address block,
    # a customer block and a summary before the transaction grid begins.
    for index, row in enumerate(rows[:25]):
        hits = 0
        if any(_header_matches(c, DATE_HEADERS) for c in row):
            hits += 1
        if any(_header_matches(c, DESCRIPTION_HEADERS) for c in row):
            hits += 1
        if any(_header_matches(c, DEBIT_HEADERS) for c in row) or \
           any(_header_matches(c, CREDIT_HEADERS) for c in row):
            hits += 1
        if any(_header_matches(c, AMOUNT_HEADERS) for c in row):
            hits += 1
        if any(_header_matches(c, BALANCE_HEADERS) for c in row):
            hits += 1
        if hits > best_hits:
            best_hits, best_index = hits, index

    if best_index >= 0 and best_hits >= 2:
        header = rows[best_index]
        shape.header_row = header
        shape.has_date = any(_header_matches(c, DATE_HEADERS) for c in header)
        shape.has_description = any(_header_matches(c, DESCRIPTION_HEADERS) for c in header)
        shape.has_debit_credit = (
            any(_header_matches(c, DEBIT_HEADERS) for c in header)
            and any(_header_matches(c, CREDIT_HEADERS) for c in header)
        ) or any(_header_matches(c, TYPE_HEADERS) for c in header)
        shape.has_amount = any(_header_matches(c, AMOUNT_HEADERS) for c in header)
        shape.has_balance = any(_header_matches(c, BALANCE_HEADERS) for c in header)
        for position, cell in enumerate(header):
            for role, vocabulary in (
                ("date", DATE_HEADERS), ("description", DESCRIPTION_HEADERS),
                ("debit", DEBIT_HEADERS), ("credit", CREDIT_HEADERS),
                ("amount", AMOUNT_HEADERS), ("balance", BALANCE_HEADERS),
                ("type", TYPE_HEADERS),
            ):
                if _header_matches(cell, vocabulary):
                    shape.columns.setdefault(role, str(position))
                    break
        shape.data_rows = sum(
            1 for row in rows[best_index + 1:]
            if any(_DATE_TOKEN.search(c or "") for c in row)
            and any(_MONEY_TOKEN.search(c or "") for c in row)
        )
        return shape

    # No recognisable header. Fall back to shape alone: a grid where most rows
    # start with a date and contain a money figure is a ledger even when its
    # column titles are in a language or wording we do not know.
    ledger_like = [
        row for row in rows
        if row and _DATE_TOKEN.search(" ".join(row[:2]))
        and any(_MONEY_TOKEN.search(c or "") for c in row[1:])
    ]
    if len(ledger_like) >= 3:
        shape.has_date = True
        shape.has_amount = True
        shape.has_description = any(len(" ".join(r)) > 25 for r in ledger_like)
        shape.data_rows = len(ledger_like)
    return shape


def analyse_text(text: str) -> TableShape:
    """Detect a ledger in free text (a PDF's extracted layer, or a mail body)."""
    shape = TableShape()
    if not text:
        return shape

    lowered = text.lower()
    shape.has_date = bool(_DATE_TOKEN.search(text))
    shape.has_description = any(term in lowered for term in DESCRIPTION_HEADERS)
    shape.has_debit_credit = (
        any(term in lowered for term in DEBIT_HEADERS[:6])
        and any(term in lowered for term in CREDIT_HEADERS[:6])
    )
    shape.has_amount = any(term in lowered for term in AMOUNT_HEADERS[:4])
    shape.has_balance = any(term in lowered for term in BALANCE_HEADERS[:4])

    # Count lines that look like transaction rows: a date plus a money figure.
    rows = 0
    for line in text.splitlines():
        if _DATE_TOKEN.search(line) and len(_MONEY_TOKEN.findall(line)) >= 1:
            rows += 1
    shape.data_rows = rows
    return shape


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def extract_account_identifier(text: str) -> Optional[str]:
    for pattern in _ACCOUNT_PATTERNS:
        match = pattern.search(text or "")
        if match:
            value = re.sub(r"\s+", "", match.group(1)).strip()
            digits = re.sub(r"[^0-9]", "", value)
            # Four digits is the shortest useful identifier ("ending in 4589").
            # Anything longer than 20 is a reference number, not an account.
            if 4 <= len(digits) <= 20:
                return value
    return None


def _parse_date(token: str) -> Optional[_dt.date]:
    cleaned = (token or "").strip().strip(",.;:")
    if not cleaned:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def extract_period(text: str) -> Tuple[Optional[_dt.date], Optional[_dt.date]]:
    """Find the statement period, preferring an explicit label over inference."""
    for pattern in _PERIOD_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        window = " ".join(g for g in match.groups() if g)
        tokens = _DATE_TOKEN.findall(window)
        dates = [d for d in (_parse_date(t) for t in tokens) if d]
        if len(dates) >= 2:
            return min(dates), max(dates)
        if len(dates) == 1:
            return dates[0], None

    # No labelled period: derive it from the transaction dates present. Bounded
    # to a plausible window so a "©2004" in a footer cannot become the start.
    tokens = _DATE_TOKEN.findall(text or "")[:400]
    dates = [d for d in (_parse_date(t) for t in tokens) if d]
    today = _dt.date.today()
    dates = [d for d in dates if _dt.date(1990, 1, 1) <= d <= today + _dt.timedelta(days=400)]
    if len(dates) >= 2:
        return min(dates), max(dates)
    return None, None


def detect_currency(text: str) -> Optional[str]:
    lowered = (text or "")[:20_000].lower()
    counts: Dict[str, int] = {}
    for token, code in CURRENCY_SIGNS.items():
        occurrences = lowered.count(token)
        if occurrences:
            counts[code] = counts.get(code, 0) + occurrences
    if not counts:
        return None
    return max(counts, key=lambda code: counts[code])


def _kind_scores(lowered: str) -> Dict[str, int]:
    return {
        CREDIT_CARD_STATEMENT: sum(1 for t in CARD_TERMS if t in lowered),
        LOAN_STATEMENT: sum(1 for t in LOAN_TERMS if t in lowered),
        BROKER_STATEMENT: sum(1 for t in BROKER_TERMS if t in lowered),
        INVESTMENT_STATEMENT: sum(1 for t in INVESTMENT_TERMS if t in lowered),
        WALLET_STATEMENT: sum(1 for t in WALLET_TERMS if t in lowered),
        BANK_STATEMENT: sum(1 for t in BANK_TERMS if t in lowered),
    }


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def classify_content(
    content: DocumentContent,
    *,
    filename: str = "",
    sender_domain: str = "",
    sender_name: str = "",
    subject: str = "",
) -> DocumentClassification:
    """Classify already-extracted document content."""
    verdict = DocumentClassification(content_kind=content.kind)

    if content.password_required:
        verdict.password_required = True
        verdict.reason = content.error or "The document is password-protected."
        verdict.error = verdict.reason
        return verdict
    if content.password_invalid:
        verdict.password_invalid = True
        verdict.reason = content.error or "The document password is invalid."
        verdict.error = verdict.reason
        return verdict
    if not content.has_content:
        verdict.reason = content.error or f"No readable content could be extracted from '{filename}'."
        verdict.error = content.error
        return verdict

    text = content.text or ""
    lowered = text.lower()

    table = analyse_rows(content.rows) if content.rows else TableShape()
    if not table.is_ledger:
        text_shape = analyse_text(text)
        if text_shape.data_rows > table.data_rows:
            table = text_shape
    verdict.table = table

    verdict.institution = identify_institution(
        text, sender_domain=sender_domain, sender_name=sender_name,
        subject=subject, filename=filename,
    )
    verdict.account_identifier = extract_account_identifier(text)
    verdict.period_start, verdict.period_end = extract_period(text)
    verdict.currency = detect_currency(text)

    scores = _kind_scores(lowered)
    negatives = [t for t in NON_FINANCIAL_TERMS if t in lowered]
    best_kind = max(scores, key=lambda k: scores[k])
    best_score = scores[best_kind]

    if not table.is_ledger:
        # No ledger. It may still be a financial document worth recording (a
        # balance certificate, an interest certificate), but it carries no
        # transactions to extract.
        financial_signal = best_score >= 2 and not negatives
        if financial_signal:
            verdict.document_type = OTHER_FINANCIAL_DOCUMENT
            verdict.is_financial = True
            verdict.confidence = 0.45
            verdict.reason = (
                f"'{filename}' reads as a financial document but contains no transaction "
                f"table (no dated rows with amounts were found)."
            )
        else:
            verdict.document_type = NOT_FINANCIAL
            verdict.confidence = 0.2
            if negatives:
                verdict.reason = (
                    f"'{filename}' reads as {negatives[0]!r} rather than a statement, and "
                    "contains no transaction table."
                )
            else:
                verdict.reason = (
                    f"'{filename}' contains no transaction table "
                    "(rows of date + description + amount)."
                )
        return verdict

    # A ledger is present. Negative vocabulary now only lowers confidence: a
    # statement footer that mentions "terms and conditions" is still a statement.
    verdict.is_financial = True
    if best_score == 0:
        # Structure without vocabulary — an unlabelled export from an
        # institution we cannot characterise. Kept, and typed conservatively.
        verdict.document_type = BANK_STATEMENT
        verdict.confidence = 0.55
        verdict.reason = (
            f"'{filename}' contains a transaction ledger ({table.data_rows} dated rows with "
            "amounts) but no vocabulary identifying the statement kind; treated as a bank "
            "statement."
        )
    else:
        verdict.document_type = best_kind
        base = 0.6 + min(0.3, 0.05 * best_score)
        if negatives:
            base -= 0.1
        if table.has_balance:
            base += 0.05
        verdict.confidence = round(min(0.98, base), 2)
        verdict.reason = (
            f"'{filename}' contains a transaction ledger ({table.data_rows} dated rows with "
            f"amounts) and {best_score} term(s) characteristic of a "
            f"{best_kind.replace('_', ' ').lower()}."
        )

    if negatives:
        verdict.reason += f" Note: also contains {negatives[0]!r}."
    return verdict


def classify_file(
    path: str,
    filename: str = "",
    *,
    password: Optional[str] = None,
    sender_domain: str = "",
    sender_name: str = "",
    subject: str = "",
) -> DocumentClassification:
    """Read a document from disk and classify it."""
    content = read_document(path, filename=filename or path, password=password)
    return classify_content(
        content, filename=filename or path, sender_domain=sender_domain,
        sender_name=sender_name, subject=subject,
    )
