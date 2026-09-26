"""
bank_alerts.py — Single-file offline rule-based parser for Indian bank transaction-alert emails.
No third-party dependencies — pure Python standard library.
Handles body-only alert emails (no attachments required).
"""

import base64
import datetime
import hashlib
import html.parser
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any

logger = logging.getLogger(__name__)

# ── 1. REGISTRY & CONSTANTS ──────────────────────────────────────────────────

BANK_DOMAINS: Dict[str, str] = {
    # HDFC Bank
    "hdfcbank.net": "HDFC Bank",
    "hdfcbank.com": "HDFC Bank",
    # ICICI Bank
    "icicibank.com": "ICICI Bank",
    "icicibank.co.in": "ICICI Bank",
    # State Bank of India
    "sbi.co.in": "State Bank of India",
    "sbi.com": "State Bank of India",
    # Axis Bank
    "axisbank.com": "Axis Bank",
    "axisbank.co.in": "Axis Bank",
    # Kotak Mahindra Bank
    "kotak.com": "Kotak Mahindra Bank",
    # IndusInd Bank
    "indusind.com": "IndusInd Bank",
    # Punjab National Bank
    "pnb.co.in": "Punjab National Bank",
    # Canara Bank
    "canarabank.com": "Canara Bank",
    "canarabank.in": "Canara Bank",
    # Bank of Baroda
    "bankofbaroda.com": "Bank of Baroda",
    "bankofbaroda.co.in": "Bank of Baroda",
    # Federal Bank
    "federalbank.co.in": "Federal Bank",
    # IDFC FIRST Bank
    "idfcfirstbank.com": "IDFC FIRST Bank",
    # Yes Bank
    "yesbank.in": "Yes Bank",
}

# Transaction indicators vs Non-transaction (OTP / Promo / Statement) indicators
_TXN_KEYWORDS = re.compile(
    r'\b(debited|credited|spent|withdrawn|transferred|deposited|received|paid|sent|payment|txn|transaction|purchase|charge|auto-debited)\b',
    re.IGNORECASE
)

_NON_TXN_KEYWORDS = re.compile(
    r'\b(otp|one time password|verification code|security code|e-statement|account statement|monthly statement|special offer|loan offer|credit card offer|congratulations|pre-approved|apply now)\b',
    re.IGNORECASE
)


# ── 2. DATA MODELS ───────────────────────────────────────────────────────────

@dataclass
class ParsedTransaction:
    amount: float
    direction: str  # "debit" | "credit"
    txn_datetime: Optional[str] = None  # ISO format string or YYYY-MM-DD
    account_last4: Optional[str] = None
    counterparty: Optional[str] = None
    mode: Optional[str] = None  # UPI, IMPS, NEFT, POS, ATM, NetBanking, Card, etc.
    balance: Optional[float] = None
    reference_no: Optional[str] = None
    bank_name: str = "Unknown Bank"
    source: str = "email_alert"
    needs_review: bool = False
    fingerprint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "amount": self.amount,
            "direction": self.direction,
            "txn_datetime": self.txn_datetime,
            "account_last4": self.account_last4,
            "counterparty": self.counterparty,
            "mode": self.mode,
            "balance": self.balance,
            "reference_no": self.reference_no,
            "bank_name": self.bank_name,
            "source": self.source,
            "needs_review": self.needs_review,
            "fingerprint": self.fingerprint,
        }


@dataclass
class AlertResult:
    status: str  # "parsed" | "duplicate" | "skipped"
    reason: Optional[str] = None
    bank_name: Optional[str] = None
    transaction: Optional[ParsedTransaction] = None

    def log_line(self) -> str:
        return f"[{self.status.upper()}] Bank: {self.bank_name or 'N/A'} | Reason: {self.reason or 'N/A'}"


# ── 3. HTML TO TEXT PARSER (STDLIB) ─────────────────────────────────────────

class _HTMLTextExtractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self._chunks: List[str] = []

    def handle_data(self, data: str):
        text = data.strip()
        if text:
            self._chunks.append(text)

    def get_text(self) -> str:
        return " ".join(self._chunks)


def extract_text_from_html(html_str: str) -> str:
    """Converts HTML string to clean plain text using standard library html.parser."""
    if not html_str:
        return ""
    try:
        parser = _HTMLTextExtractor()
        parser.feed(html_str)
        return parser.get_text()
    except Exception as e:
        logger.debug(f"HTML parsing fallback: {e}")
        return re.sub(r'<[^>]+>', ' ', html_str)


# ── 4. GMAIL MESSAGE ADAPTER ─────────────────────────────────────────────────

def extract_email_fields(message_dict: Dict[str, Any]) -> Dict[str, str]:
    """
    Normalizes a standard Gmail API message resource dict into:
    { "sender", "subject", "date", "body", "domain" }
    """
    payload = message_dict.get("payload", {})
    headers = payload.get("headers", [])

    header_map = {h.get("name", "").lower(): h.get("value", "") for h in headers}

    sender = header_map.get("from", "")
    subject = header_map.get("subject", "")
    date_hdr = header_map.get("date", "")

    # Extract sender domain
    domain = ""
    domain_match = re.search(r'@([a-zA-Z0-9.\-]+)', sender)
    if domain_match:
        domain = domain_match.group(1).lower()

    # Extract plain body text (recursively search payload parts)
    body_text = _extract_body_from_payload(payload)

    return {
        "sender": sender,
        "subject": subject,
        "date": date_hdr,
        "body": body_text,
        "domain": domain,
    }


def _extract_body_from_payload(payload: Dict[str, Any]) -> str:
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})

    if body.get("data"):
        raw_data = body.get("data", "")
        decoded = _safe_base64_decode(raw_data)
        if "html" in mime_type:
            return extract_text_from_html(decoded)
        return decoded

    parts = payload.get("parts", [])
    plain_text = ""
    html_text = ""

    for part in parts:
        part_mime = part.get("mimeType", "")
        part_body = part.get("body", {})
        if part_body.get("data"):
            decoded = _safe_base64_decode(part_body.get("data", ""))
            if part_mime == "text/plain":
                plain_text += decoded + "\n"
            elif part_mime == "text/html":
                html_text += extract_text_from_html(decoded) + "\n"
        elif part.get("parts"):
            # Recursive check for nested multipart
            plain_text += _extract_body_from_payload(part)

    return plain_text.strip() if plain_text.strip() else html_text.strip()


def _safe_base64_decode(data_str: str) -> str:
    try:
        # Base64url to standard base64
        padded = data_str.replace('-', '+').replace('_', '/')
        while len(padded) % 4 != 0:
            padded += '='
        return base64.b64decode(padded).decode('utf-8', errors='ignore')
    except Exception:
        return ""


# ── 5. CLASSIFIER ─────────────────────────────────────────────────────────────

def classify_email(sender_domain: str, subject: str, body: str) -> Tuple[bool, str, Optional[str]]:
    """
    Confirms sender is a registered bank and classifies whether it's a real transaction alert.
    Returns: (is_transaction_alert, reason, bank_name)
    """
    # Check bank domain
    bank_name = None
    for domain_key, name in BANK_DOMAINS.items():
        if sender_domain.endswith(domain_key):
            bank_name = name
            break

    if not bank_name:
        return False, f"Sender domain '{sender_domain}' is not a recognized bank.", None

    full_text = f"{subject} {body}"

    # Filter out OTP / Promo / Statement notifications
    non_txn_match = _NON_TXN_KEYWORDS.search(full_text)
    if non_txn_match:
        matched_word = non_txn_match.group(0)
        # Exception check: if it says "statement" but has "debited/credited", make sure it's not a statement attachment notification
        if matched_word.lower() in ("otp", "one time password", "verification code", "security code"):
            return False, "Filtered out: Security/OTP message.", bank_name
        if matched_word.lower() in ("e-statement", "account statement", "monthly statement"):
            if not _TXN_KEYWORDS.search(full_text):
                return False, "Filtered out: Statement delivery notification.", bank_name
        if any(p in matched_word.lower() for p in ["offer", "pre-approved", "apply now"]):
            return False, "Filtered out: Promotional message.", bank_name

    # Check for transaction keywords
    if not _TXN_KEYWORDS.search(full_text):
        return False, "No transaction keywords (debited/credited/spent/paid) found.", bank_name

    return True, "Valid transaction alert email.", bank_name


# ── 6. SHARED FIELD EXTRACTORS ───────────────────────────────────────────────

def extract_amount(text: str) -> Optional[float]:
    """Extracts transaction amount (e.g., Rs. 1,500.50, INR 500, Rs 200)."""
    match = re.search(
        r'(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)',
        text,
        re.IGNORECASE
    )
    if match:
        amt_str = match.group(1).replace(',', '')
        try:
            return float(amt_str)
        except ValueError:
            pass
    return None


def extract_direction(text: str) -> str:
    """Determines direction: 'debit' or 'credit'."""
    credit_match = re.search(r'\b(credited|deposited|received|paid in|inflow)\b', text, re.IGNORECASE)
    debit_match = re.search(r'\b(debited|spent|withdrawn|paid to|transferred|outflow|purchase)\b', text, re.IGNORECASE)

    if credit_match and not debit_match:
        return "credit"
    if debit_match and not credit_match:
        return "debit"
    if credit_match and debit_match:
        # Resolve by position if both present (e.g. "A/c credited... for payment")
        return "credit" if credit_match.start() < debit_match.start() else "debit"
    return "debit"  # default heuristic for alerts


def extract_account_last4(text: str) -> Optional[str]:
    """Extracts 4-digit account or card last digits."""
    match = re.search(
        r'(?:A/c|Account|Card|a/c|AC|card|ending)\s*(?:no\.?|Num)?\s*(?:x+|X+|\*+)?(\d{4})\b',
        text,
        re.IGNORECASE
    )
    if match:
        return match.group(1)

    # Fallback pattern for "XX1234" or "x1234"
    match_xx = re.search(r'\b[xX\*]{2,12}(\d{4})\b', text)
    if match_xx:
        return match_xx.group(1)

    return None


def extract_mode(text: str) -> Optional[str]:
    """Extracts payment mode (UPI, IMPS, NEFT, RTGS, POS, ATM, NetBanking, Card)."""
    modes = ["UPI", "IMPS", "NEFT", "RTGS", "POS", "ATM", "NetBanking", "Credit Card", "Debit Card"]
    for mode in modes:
        if re.search(r'\b' + re.escape(mode) + r'\b', text, re.IGNORECASE):
            return mode
    return None


def extract_counterparty(text: str) -> Optional[str]:
    """Extracts payee or merchant name (e.g., 'to VPA xyz@upi', 'at AMAZON', 'info: ZOMATO')."""
    # VPA / UPI info
    vpa_match = re.search(r'(?:to|at|info:)\s+([A-Za-z0-9.\-_]+@[a-zA-Z]+)', text, re.IGNORECASE)
    if vpa_match:
        return vpa_match.group(1)

    # Merchant / Payee after 'to', 'at', 'towards'
    merchant_match = re.search(r'(?:to|at|towards|info:)\s+([A-Za-z0-9\s.&]{2,30}?)(?:\s+on|\s+ref|\s+avail|\s+bal|\.|$)', text, re.IGNORECASE)
    if merchant_match:
        name = merchant_match.group(1).strip()
        if not re.search(r'\b(A/c|account|Rs|INR|card)\b', name, re.IGNORECASE):
            return name

    return None


def extract_balance(text: str) -> Optional[float]:
    """Extracts balance after transaction if available."""
    match = re.search(
        r'(?:Bal|Balance|Avail Bal|Available Balance)\s*(?:is|:)?\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)',
        text,
        re.IGNORECASE
    )
    if match:
        amt_str = match.group(1).replace(',', '')
        try:
            return float(amt_str)
        except ValueError:
            pass
    return None


def extract_reference_no(text: str) -> Optional[str]:
    """Extracts Ref/Txn/UTR number."""
    match = re.search(
        r'(?:Ref|Txn|UTR|UPI Ref|Reference)\s*(?:No\.?|Num|ID)?\s*[:\s]*([A-Za-z0-9]{6,22})',
        text,
        re.IGNORECASE
    )
    if match:
        return match.group(1)
    return None


def extract_date(text: str, email_date_hdr: str = "") -> Optional[str]:
    """Extracts transaction date/datetime string."""
    # Pattern: 01-AUG-26, 01/08/2026, 01 Aug 2026 14:30:15
    match = re.search(
        r'\b(\d{1,2}[-/\s](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{1,2})[-/\s]\d{2,4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)\b',
        text,
        re.IGNORECASE
    )
    if match:
        return match.group(1)

    # Fallback to email header date if available
    if email_date_hdr:
        return email_date_hdr.strip()

    return None


# ── 7. PARSERS & FINGERPRINTING ──────────────────────────────────────────────

def compute_fingerprint(
    bank_name: str,
    account_last4: Optional[str],
    amount: float,
    direction: str,
    reference_no: Optional[str],
    txn_datetime: Optional[str]
) -> str:
    """
    Computes a date-level deterministic fingerprint for cross-channel deduping.
    Allows deduplication between email alerts and downloaded PDF statements.
    """
    date_part = ""
    if txn_datetime:
        date_part = str(txn_datetime).strip().split(" ")[0].split("T")[0].lower()

    raw_str = (
        f"{(bank_name or '').lower()}|"
        f"{account_last4 or 'none'}|"
        f"{amount:.2f}|"
        f"{(direction or '').lower()}|"
        f"{reference_no or 'none'}|"
        f"{date_part}"
    )
    return hashlib.sha256(raw_str.encode('utf-8')).hexdigest()[:32]


def parse_alert_text(
    bank_name: str,
    subject: str,
    body: str,
    email_date: str = ""
) -> ParsedTransaction:
    """Parses text content into ParsedTransaction model using generic + per-bank extractors."""
    full_text = f"{subject} {body}"

    amt = extract_amount(full_text)
    direction = extract_direction(full_text)
    acct = extract_account_last4(full_text)
    mode = extract_mode(full_text)
    counterparty = extract_counterparty(full_text)
    balance = extract_balance(full_text)
    ref = extract_reference_no(full_text)
    txn_date = extract_date(full_text, email_date)

    # Check if key required fields are missing
    needs_review = False
    if amt is None or amt <= 0:
        amt = 0.0
        needs_review = True
    if not txn_date:
        needs_review = True

    fp = compute_fingerprint(bank_name, acct, amt, direction, ref, txn_date)

    return ParsedTransaction(
        amount=amt,
        direction=direction,
        txn_datetime=txn_date,
        account_last4=acct,
        counterparty=counterparty,
        mode=mode,
        balance=balance,
        reference_no=ref,
        bank_name=bank_name,
        source="email_alert",
        needs_review=needs_review,
        fingerprint=fp,
    )


# ── 8. PUBLIC ENTRY POINTS ───────────────────────────────────────────────────

def process_gmail_message(
    message_dict: Dict[str, Any],
    seen_fingerprints: Optional[Set[str]] = None
) -> AlertResult:
    """
    Main public entry point for Gmail API message payload dicts.
    """
    if seen_fingerprints is None:
        seen_fingerprints = set()

    fields = extract_email_fields(message_dict)

    is_alert, reason, bank_name = classify_email(
        sender_domain=fields["domain"],
        subject=fields["subject"],
        body=fields["body"],
    )

    if not is_alert:
        return AlertResult(status="skipped", reason=reason, bank_name=bank_name)

    # Parse body
    txn = parse_alert_text(
        bank_name=bank_name or "Bank",
        subject=fields["subject"],
        body=fields["body"],
        email_date=fields["date"],
    )

    # Deduplication check
    if txn.fingerprint in seen_fingerprints:
        return AlertResult(
            status="duplicate",
            reason=f"Fingerprint '{txn.fingerprint[:10]}...' already processed.",
            bank_name=bank_name,
            transaction=txn
        )

    return AlertResult(
        status="parsed",
        reason="Transaction alert successfully parsed.",
        bank_name=bank_name,
        transaction=txn
    )
