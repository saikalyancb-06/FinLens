"""Statement header facts: bank, account number, period, printed balances.

Read from the statement text itself, never from the filename. Filenames in the
sample set are either masked (`XXXXXXXXXX2911_...pdf`) or free-form, and a file
renamed by whoever emailed it must not change which account its rows land in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.b2b.consolidate.tokens import balance_paise, first_date

# IFSC prefix -> bank. The IFSC is printed on every Indian statement and is the
# least ambiguous bank identifier on it.
IFSC_BANKS = {
    "UTIB": "Axis Bank", "IOBA": "Indian Overseas Bank", "SBIN": "State Bank of India",
    "BARB": "Bank of Baroda", "HDFC": "HDFC Bank", "ICIC": "ICICI Bank",
    "KKBK": "Kotak Mahindra Bank", "CNRB": "Canara Bank", "PUNB": "Punjab National Bank",
    "UBIN": "Union Bank of India", "IDIB": "Indian Bank", "YESB": "Yes Bank",
    "IDFB": "IDFC First Bank", "INDB": "IndusInd Bank", "FDRL": "Federal Bank",
    "KARB": "Karnataka Bank", "MAHB": "Bank of Maharashtra", "BKID": "Bank of India",
    "CBIN": "Central Bank of India", "UCBA": "UCO Bank", "PSIB": "Punjab & Sind Bank",
    "SIBL": "South Indian Bank", "KVBL": "Karur Vysya Bank", "CIUB": "City Union Bank",
    "TMBL": "Tamilnad Mercantile Bank", "RATN": "RBL Bank", "AUBL": "AU Small Finance Bank",
    "ESFB": "Equitas Small Finance Bank", "DBSS": "DBS Bank", "SCBL": "Standard Chartered",
}

# Fallback when no IFSC is printed: names as they appear in statement headers.
_BANK_WORDS = [
    ("INDIAN OVERSEAS BANK", "Indian Overseas Bank"), ("STATE BANK OF INDIA", "State Bank of India"),
    ("AXIS BANK", "Axis Bank"), ("AXIS ACCOUNT", "Axis Bank"), ("BANK OF BARODA", "Bank of Baroda"),
    ("BOB WORLD", "Bank of Baroda"), ("HDFC BANK", "HDFC Bank"), ("ICICI BANK", "ICICI Bank"),
    ("KOTAK", "Kotak Mahindra Bank"), ("CANARA BANK", "Canara Bank"),
]

_IFSC_RX = re.compile(r"\b([A-Z]{4})0[A-Z0-9]{6}\b")

# Label, then (within a short window, possibly across a line break) the number.
_ACCOUNT_RX = [
    re.compile(r"(?:Account|A/C|Acct)\.?\s*(?:No|Number|Num)\.?\s*[:.\-]*\s*([0-9]{9,20})", re.I),
    re.compile(r"(?:Account|A/C)\s*(?:No|Number)[^0-9]{0,120}?([0-9]{9,20})", re.I | re.S),
]

_PERIOD_RX = re.compile(
    r"(?:period|from)\D{0,30}?(\d{1,4}[-/][\dA-Za-z]{1,3}[-/]\d{2,4})[\s:]*(?:to|-)[\s:]*"
    r"(\d{1,4}[-/][\dA-Za-z]{1,3}[-/]\d{2,4})", re.I)

_AMT = r"(-?[\d,]+\.\d{2}\s*(?:CR|DR|Cr|Dr)?)"
_OPENING_RX = [
    re.compile(r"Opening\s+Balance\s*[:\-]?\s*(?:INR|Rs\.?)?\s*" + _AMT, re.I),
    # SBI summary: the brought-forward figure starts the line that carries the
    # Dr and Cr counts ("7,63,175.48CR   375   104").
    re.compile(r"Brought\s+Forward[\s\S]{0,400}?^\s*" + _AMT + r"\s+\d+\s+\d+\s*$", re.I | re.M),
]
_CLOSING_RX = [
    re.compile(r"Closing\s+Balance\s*[:\-]?\s*(?:INR|Rs\.?)?\s*" + _AMT, re.I),
    re.compile(r"Grand\s+Total\s*:?\s*[\d,.\s]*?" + _AMT + r"\s*$", re.I | re.M),
]


@dataclass
class StatementMeta:
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    ifsc: Optional[str] = None
    account_holder: Optional[str] = None
    period_from: Optional[str] = None
    period_to: Optional[str] = None
    printed_opening_paise: Optional[int] = None
    printed_closing_paise: Optional[int] = None
    notes: List[str] = field(default_factory=list)


def detect_meta(first_page_text: str, last_page_text: str = "") -> StatementMeta:
    meta = StatementMeta()
    text = first_page_text or ""

    m = _IFSC_RX.search(text)
    if m:
        meta.ifsc = m.group(0)
        meta.bank_name = IFSC_BANKS.get(m.group(1))
    if not meta.bank_name:
        upper = (text + "\n" + last_page_text).upper()
        for word, name in _BANK_WORDS:
            if word in upper:
                meta.bank_name = name
                break

    for rx in _ACCOUNT_RX:
        m = rx.search(text)
        if m:
            meta.account_number = m.group(1)
            break
    if not meta.account_number:
        meta.account_number = _account_by_proximity(text)

    m = _PERIOD_RX.search(text) or _PERIOD_RX.search(last_page_text or "")
    if m:
        a, b = first_date(m.group(1)), first_date(m.group(2))
        meta.period_from = a.isoformat() if a else None
        meta.period_to = b.isoformat() if b else None

    for rx in _OPENING_RX:
        m = rx.search(text) or rx.search(last_page_text or "")
        if m:
            meta.printed_opening_paise = balance_paise(m.group(1).replace(" ", ""))
            break
    for rx in _CLOSING_RX:
        m = rx.search(last_page_text or "")
        if m:
            meta.printed_closing_paise = balance_paise(m.group(1).replace(" ", ""))
            break
    return meta


def _account_by_proximity(text: str) -> Optional[str]:
    """Label and value printed on different lines (IOB: ': 1655...' ABOVE 'Account No')."""
    labels = [m.start() for m in re.finditer(r"Account\s*(?:No|Number)|A/C\s*No", text, re.I)]
    if not labels:
        return None
    best = None
    for m in re.finditer(r"(?<![\d])(\d{9,18})(?![\d])", text):
        dist = min(abs(m.start() - l) for l in labels)
        if best is None or dist < best[0]:
            best = (dist, m.group(1))
    return best[1] if best and best[0] < 400 else None


_FILENAME_BANKS = [
    (r"\bIOB\b", "Indian Overseas Bank"), (r"\bSBI\b", "State Bank of India"),
    (r"\bAXIS\b", "Axis Bank"), (r"\bHDFC\b", "HDFC Bank"), (r"\bICICI\b", "ICICI Bank"),
    (r"\bBOB\b", "Bank of Baroda"), (r"\bKOTAK\b", "Kotak Mahindra Bank"),
    (r"\bCANARA\b", "Canara Bank"),
]


def bank_from_filename(name: str) -> Optional[str]:
    """Last resort only — used when the statement text names no bank at all."""
    stem = re.sub(r"[_\-.]", " ", name or "").upper()
    for rx, bank in _FILENAME_BANKS:
        if re.search(rx, stem):
            return bank
    return None


_NOT_A_NAME = re.compile(r"(STATEMENT|ACCOUNT|BANK|BRANCH|TYPE|DATE|PAGE|CUSTOMER|JOINT|REPORT|"
                         r"ADDRESS|IFSC|PERIOD|NOMINEE|OVERSEAS|SAVINGS|CURRENT|MICR|CKYC|PHONE|"
                         r"MYSORE|CORP OG|STREET|ROAD|CROSS|LAYOUT|NAGAR)", re.I)
_NAME = r"([A-Z][A-Z .&]{2,60}?)"


def _ok(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    name = re.split(r"\s{2,}|,", name.strip())[0].strip(" .:")
    name = re.sub(r"^(M/S\.?|MR\.|MRS\.|MS\.|SMT\.)\s*", "", name, flags=re.I).strip()
    if len(name) >= 3 and not _NOT_A_NAME.search(name) and re.search(r"[A-Z]{3}", name):
        return name.upper()
    return None


def detect_holder(text: str) -> Optional[str]:
    """Account holder name, best effort. Used only as evidence for internal transfers."""
    t = text or ""
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    tries = []
    for i, l in enumerate(lines):
        if re.search(r"Account\s*Holder\s*Name", l, re.I):
            m = re.search(r"Account\s*Holder\s*Name\s*[:\-]?\s*" + _NAME + r"(?:\s{2,}|,|$)", l)
            tries.append(m.group(1) if m else None)
            if i > 0:
                m = re.match(r":\s*" + _NAME + r"(?:\s{2,}|,|$)", lines[i - 1])
                tries.append(m.group(1) if m else None)
        if re.search(r"\bA/C\s*NO\b", l) and i + 1 < len(lines):          # IOB passbook
            tries.append(lines[i + 1])
        if re.match(r"Account\s*Number\s*-", l) and i > 0:               # IOB net banking
            tries.append(lines[i - 1])
        if re.search(r"Account\s*Name", l, re.I) and i + 1 < len(lines):  # Bank of Baroda
            tries.append(lines[i + 1])
        m = re.search(r"(?:^|\s)(?:M/S\.\s?|M/S\s|Mr\.|Mrs\.|Ms\.|Smt\.)\s*" + _NAME + r"(?:\s{2,}|,|$)", l)
        if m:
            tries.append(m.group(1))
    for cand in tries:
        name = _ok(cand)
        if name:
            return name
    for l in lines[:6]:                                                 # Axis: holder first
        name = _ok(re.split(r"\s{2,}", l)[0])
        if name and re.fullmatch(r"[A-Z][A-Z .&]{3,60}", name):
            return name
    return None
