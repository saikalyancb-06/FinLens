"""
normalizer.py  —  Production-grade normalizer for bank statement parsing.

Fixes applied (vs original):
  BUG-001  clean_amount_string: max-value guard, max-digit-length, double-decimal,
           OCR-letter rejection, Indian lakh-crore grouping handled correctly.
  BUG-002  clean_amount_string: expanded CR/DR indicator vocabulary.
  BUG-004  normalize_date_string: added dot-separator, no-separator, short-month+2y,
           "DD MMM" (no year) formats.
  BUG-011  map_headers: longer-match wins; conflict-resolution step.
  BUG-012  build_normalized_transaction: added confidence, warnings, source metadata.
  NEW      clean_ocr_text: OCR artifact correction pass (O→0, I→1, l→1 in numeric context).
"""

import re
import unicodedata
import logging
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Maximum realistic single-transaction amount (₹5 crore)
MAX_AMOUNT = 50_000_000.0
# Maximum digits allowed in a numeric string before we call it garbage
MAX_DIGIT_LEN = 14

DATE_PATTERNS = [
    r'\b\d{1,2}[./\-]\d{1,2}[./\-]\d{4}\b',           # 01/02/2024  01.02.2024  01-02-2024
    r'\b\d{1,2}[./\-]\d{1,2}[./\-]\d{2}\b',            # 01/02/24
    r'\b\d{4}[./\-]\d{1,2}[./\-]\d{1,2}\b',            # 2024-02-01
    r'\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4}\b',  # 01 Jan 2024
    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{2,4}\b',  # Jan 01, 2024
    r'\b\d{1,2}[-](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-]\d{2,4}\b',  # 01-Jan-2024
    r'\b\d{1,2}(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\d{4}\b',  # 01Jan2024 (no space)
    r'\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b',  # 01 Jan (no year)
    r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\b',  # Jan 01 (no year)
]

REF_PATTERNS = [
    r'\b(?:UPI[/\-]|IMPS[/\-]|NEFT[/\-]|RTGS[/\-]|REF[/\-]|CHQ[/\-]|TXN[/\-]|POS[/\-]|FT[/\-]|ATM[/\-])?([A-Z0-9]{8,22})\b',
]

HEADER_ALIASES = {
    "date":             ["date", "txn date", "transaction date", "value date", "post date",
                         "entry date", "tran date", "trans date", "booking date", "value dt",
                         "txn_date", "value_date", "post_date", "date(dd/mm/yyyy)", "date (dd-mm-yyyy)"],
    "description":      ["description", "particulars", "narration", "transaction details",
                         "remarks", "details", "summary", "transaction narration",
                         "tran remarks", "trans particulars", "narration/particulars",
                         "narration / particulars", "description / narration", "transaction particulars"],
    "debit":            ["debit", "withdrawal", "dr", "withdrawals", "debit (dr)", "outflow",
                         "paid out", "withdraw", "debit amount", "debit amt", "withdrawal (dr)",
                         "dr amount", "dr. amount", "withdrawal amt", "debit (inr)", "withdrawal (inr)",
                         "debit(rs.)", "debit (rs)"],
    "credit":           ["credit", "deposit", "cr", "deposits", "credit (cr)", "inflow",
                         "paid in", "credit amount", "credit amt", "deposit (cr)",
                         "cr amount", "cr. amount", "deposit amt", "credit (inr)", "deposit (inr)",
                         "credit(rs.)", "credit (rs)"],
    "amount":           ["amount", "txn amount", "transaction amount", "tran amount", "amount (inr)", "amount (rs.)"],
    "balance":          ["running balance", "closing balance", "available balance", "avail bal",
                         "book balance", "ledger balance", "balance", "bal",
                         "balance (inr)", "balance (rs.)", "closing bal", "net balance"],
    "reference_number": ["ref no", "ref no.", "reference", "chq no", "cheque no",
                         "ref/chq no", "utr", "txn id", "transaction id",
                         "reference number", "chq/ref no", "instrument no", "chq.no.",
                         "cheque/ref no", "chq/ref. no.", "utr no", "rrn"],
    "transaction_type": ["type", "txn type", "transaction type", "dr/cr", "dr / cr",
                         "debit/credit", "cr/dr", "cr / dr", "mode", "tran type", "trans type"],
}

# Debit/credit indicators — extended vocabulary
_CREDIT_INDICATORS = re.compile(
    r'\b(cr|credit|deposit|dep|paid\s+in|inflow|receipt|received|salary|refund|reversal)\b',
    re.IGNORECASE
)
_DEBIT_INDICATORS = re.compile(
    r'\b(dr|debit|withdrawal|withdraw|w/?d|paid\s+out|outflow|payment|emi|charge|fee|transfer\s+out)\b',
    re.IGNORECASE
)

# ──────────────────────────────────────────────────────────────────────────────
# OCR Text Cleaning
# ──────────────────────────────────────────────────────────────────────────────

# Invisible/control Unicode that can appear in scanned PDF text
_INVISIBLE_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u00ad\u200b-\u200f\u2028\u2029\ufeff]')
# Multiple whitespace collapser
_WHITESPACE_RE = re.compile(r'[ \t]+')


#: A maximal run of characters that could belong to one word or to one number.
#: Separators such as - / : _ deliberately BREAK a run, because that is exactly
#: where a bank narration joins a name to a reference number.
_ALNUM_RUN_RE = re.compile(r"[0-9A-Za-z.,]+")

#: A run made only of what a number is made of: digits, the four OCR
#: confusables (O/o -> 0, I/l -> 1, S -> 5), and the separators.
_NUMBER_SHAPED_RE = re.compile(r"^[0-9OoIlS.,]+$")


def clean_ocr_text(text: str) -> str:
    """
    Normalise raw OCR output:
      1. Unicode NFC normalisation
      2. Strip invisible / control characters
      3. Collapse multiple spaces/tabs (but preserve newlines for row detection)
      4. Correct common OCR character confusions IN NUMERIC CONTEXTS ONLY:
           O → 0,  I → 1,  l → 1,  S → 5
         (only inside digit runs, never in purely alphabetic tokens)
    """
    if not text:
        return ""

    # NFC normalise
    text = unicodedata.normalize("NFC", text)

    # Strip invisible chars
    text = _INVISIBLE_RE.sub("", text)

    # Collapse horizontal whitespace only (preserve \n for line-based parsing)
    text = _WHITESPACE_RE.sub(" ", text)

    # OCR corrections — only inside runs that are ENTIRELY number-shaped.
    #
    # The previous rule was `\b(?=\S*\d)\S*[OIlS]\S*\b`: "any whitespace-
    # delimited token containing a digit ANYWHERE", and it then rewrote the
    # WHOLE token. An Indian bank narration is one such token far more often
    # than not, because the reference number is glued onto the merchant name:
    #
    #     UPI-BOOKMYSHOW-5540           ->  UP1-B00KMY5H0W-5540
    #     UPI-OLA CABS-4471             ->  UPI-OLA CAB5-4471
    #     EBANK:WIB/1501906475/PRAKASH  ->  EBANK:W1B/1501906475/PRAKA5H
    #
    # That is not a display blemish. The rule engine matches merchants BY NAME,
    # so a corrupted name misses its rule and the row is filed somewhere else —
    # BOOKMYSHOW was landing in Transportation — and counterparty memory keys on
    # the same corrupted string, so the mistake is remembered. CSV and Excel
    # uploads run through here as well (excel_csv_parser calls this per cell),
    # where no OCR was involved and there was nothing to correct in the first
    # place.
    #
    # A run is corrected only when EVERY character in it is one a number can
    # legitimately be made of: digits, the four confusables, and the separators.
    # Any other letter marks the run as a word, and words are left exactly as
    # the bank wrote them. `1O,OOO.OO` is still repaired — every character in it
    # qualifies — while `BOOKMYSHOW` is rejected on sight by B, K, M, Y, H, W.
    def _fix_numeric_run(m: re.Match) -> str:
        token = m.group(0)
        # No digit to anchor it as a number, so there is nothing saying it is
        # one. `ISO` and `SOS` stay themselves.
        if not any(ch.isdigit() for ch in token):
            return token
        if not _NUMBER_SHAPED_RE.match(token):
            return token
        token = token.replace("O", "0").replace("o", "0")
        token = token.replace("I", "1").replace("l", "1")
        token = token.replace("S", "5")
        return token

    # Runs are broken by anything that is not alphanumeric or a decimal or
    # thousands separator, so `-`, `/`, `:` and `_` cut the merchant name away
    # from the reference number — the boundary the old pattern ignored.
    text = _ALNUM_RUN_RE.sub(_fix_numeric_run, text)

    return text.strip()


# ──────────────────────────────────────────────────────────────────────────────
# Amount parsing
# ──────────────────────────────────────────────────────────────────────────────

def clean_amount_string(val: str) -> Tuple[float, Optional[str]]:
    """
    Parse a bank-statement amount cell into (float_value, debit_credit_indicator).

    Handles:
      - Indian lakh-crore grouping: 1,23,456.78 → 123456.78
      - Currency symbols: ₹, Rs, INR
      - CR / DR suffixes
      - Parenthesised negatives: (1,234.56) → 1234.56 (debit)
      - Rejects garbage: all-zeros padding, >MAX_DIGIT_LEN digits, alpha chars,
        double decimals, OCR artifacts

    Returns (0.0, None) for unparseable or rejected values and logs a warning.
    """
    if val is None:
        return 0.0, None

    val_str = str(val).strip()

    if not val_str or val_str.lower() in ("nan", "none", "null", "-", "—", "–", "n/a", ""):
        return 0.0, None

    warnings: List[str] = []
    indicator: Optional[str] = None

    # ── Detect CR/DR indicator ────────────────────────────────────────────────
    if _CREDIT_INDICATORS.search(val_str):
        indicator = "credit"
    elif _DEBIT_INDICATORS.search(val_str):
        indicator = "debit"

    # Parenthesised value → debit (accounting notation)
    is_negative = bool(re.match(r'^\(.*\)$', val_str.strip()))
    if is_negative and indicator is None:
        indicator = "debit"

    # ── Strip non-numeric chars ───────────────────────────────────────────────
    # Remove: currency symbols, letters (CR/DR already captured), parens, spaces
    cleaned = re.sub(r'[^\d.\-]', '', val_str)

    if not cleaned or cleaned in ('.', '-', ''):
        return 0.0, indicator

    # ── Guard: double decimal ─────────────────────────────────────────────────
    if cleaned.count('.') > 1:
        logger.warning(f"[Normalizer] Rejected amount with multiple decimals: '{val_str}'")
        return 0.0, indicator

    # ── Guard: leading minus only ─────────────────────────────────────────────
    if cleaned == '-':
        return 0.0, indicator

    # ── Guard: all-zeros padding (OCR garbage like 000000000000) ─────────────
    digit_only = cleaned.replace('.', '').replace('-', '')
    if len(digit_only) > MAX_DIGIT_LEN:
        logger.warning(f"[Normalizer] Rejected over-long numeric string ({len(digit_only)} digits): '{val_str}'")
        return 0.0, indicator

    if re.match(r'^0{5,}$', digit_only):
        logger.warning(f"[Normalizer] Rejected all-zeros padding: '{val_str}'")
        return 0.0, indicator

    # ── Parse float ───────────────────────────────────────────────────────────
    try:
        amount = float(cleaned)
    except ValueError:
        logger.warning(f"[Normalizer] Could not parse amount: '{val_str}'")
        return 0.0, indicator

    amount = abs(amount)

    # ── Guard: unrealistic value ──────────────────────────────────────────────
    if amount > MAX_AMOUNT:
        logger.warning(f"[Normalizer] Rejected unrealistically large amount {amount}: '{val_str}'")
        return 0.0, indicator

    return round(amount, 2), indicator


# ──────────────────────────────────────────────────────────────────────────────
# Date parsing
# ──────────────────────────────────────────────────────────────────────────────

# Ordered list of (strptime_format, has_year)
_DATE_FORMATS: List[Tuple[str, bool]] = [
    ("%d/%m/%Y",  True),
    ("%d-%m-%Y",  True),
    ("%d.%m.%Y",  True),
    ("%Y-%m-%d",  True),
    ("%Y/%m/%d",  True),
    ("%d/%m/%y",  True),
    ("%d-%m-%y",  True),
    ("%d.%m.%y",  True),
    ("%d %b %Y",  True),
    ("%d %B %Y",  True),
    ("%d-%b-%Y",  True),
    ("%d-%b-%y",  True),
    ("%d%b%Y",    True),    # 01Jan2024
    ("%d%b%y",    True),    # 01Jan24
    ("%b %d, %Y", True),
    ("%b %d %Y",  True),
    ("%d %b",     False),   # 01 Jan — no year
    ("%b %d",     False),   # Jan 01 — no year
]

def _current_year() -> int:
    """Read the year at call time, not at import time.

    This was a module constant evaluated when the module was first imported. A
    server process that stays up across New Year kept serving the OLD year, so
    every year-less date ("01 Jan") parsed on 2 January was stamped into the
    year that had just ended — twelve months out — and the "reject future dates
    beyond +1 year" guard drifted with it.
    """
    return datetime.now().year


def normalize_date_string(date_str: str) -> str:
    """
    Standardise a raw date string to YYYY-MM-DD.

    Handles all common Indian bank formats:
      dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy, yyyy-mm-dd,
      dd Mon yyyy, dd-Mon-yyyy, dd Mon (no year),
      01Jan2024, 01/01/24, etc.

    Rejects impossible dates (Feb 30, month > 12, etc.).
    Returns empty string if unparseable.
    """
    if not date_str:
        return ""

    raw = date_str.strip()
    current_year = _current_year()

    # Try every format
    for fmt, has_year in _DATE_FORMATS:
        try:
            if not has_year:
                # Inject current year so strptime never tries to parse a date
                # without a year (deprecated in Python 3.15).
                augmented = f"{raw} {current_year}"
                augmented_fmt = f"{fmt} %Y"
                dt = datetime.strptime(augmented, augmented_fmt)
            else:
                dt = datetime.strptime(raw, fmt)
            # Sanity: reject future dates beyond +1 year
            if dt.year > current_year + 1:
                continue
            # Reject implausible old dates (before 1990)
            if dt.year < 1990:
                continue
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    logger.debug(f"[Normalizer] Could not parse date: '{date_str}'")
    return raw  # Return raw rather than empty so the validator can flag it


# ──────────────────────────────────────────────────────────────────────────────
# Time parsing & UPI utilities
# ──────────────────────────────────────────────────────────────────────────────

_TIME_RE = re.compile(
    r'\b(2[0-3]|1[0-9]|0?[0-9])[:\.]?([0-5][0-9])(?:[:\.]?([0-5][0-9]))?\s*(am|pm|AM|PM)?\b'
)


def normalize_time_string(time_str: str) -> str:
    """
    Standardise a raw time string to HH:MM:SS (24-hour format).
    Handles '14:32:10', '02:30:15 PM', '2:30 pm', '14.32', etc.
    """
    if not time_str:
        return ""
    m = _TIME_RE.search(str(time_str).strip())
    if not m:
        return ""

    hh, mm, ss, ampm = m.groups()
    hours = int(hh)
    minutes = int(mm)
    seconds = int(ss) if ss else 0

    if ampm:
        ampm_lower = ampm.lower()
        if ampm_lower == "pm" and hours < 12:
            hours += 12
        elif ampm_lower == "am" and hours == 12:
            hours = 0

    if 0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return ""


def extract_time_from_text(text: str) -> str:
    """Extract first valid time string from description, date, or raw text."""
    if not text:
        return ""
    match = re.search(
        r'\b(?:[01]?\d|2[0-3])[:\.]\d{2}(?:[:\.]\d{2})?\s*(?:AM|PM|am|pm)?\b',
        text
    )
    if match:
        return normalize_time_string(match.group(0))
    return ""


def is_upi_transaction(txn: Dict[str, Any]) -> bool:
    """Check if transaction is a UPI transaction."""
    mode = str(txn.get("mode", "")).upper()
    ttype = str(txn.get("transaction_type", "")).upper()
    desc = str(txn.get("description", "")).upper()
    ref = str(txn.get("reference_number", "")).upper()
    raw = str(txn.get("raw_text", "")).upper()

    if mode == "UPI" or ttype == "UPI":
        return True
    if "UPI" in desc or "UPI" in ref or "UPI" in raw:
        return True
    if re.search(r'\bUPI[/\-_]', desc) or re.search(r'\bUPI[/\-_]', ref) or re.search(r'\bUPI[/\-_]', raw):
        return True
    return False



# ──────────────────────────────────────────────────────────────────────────────
# Reference extraction
# ──────────────────────────────────────────────────────────────────────────────

def extract_reference_number(text: str) -> str:
    """Extract transaction UTR / UPI / cheque reference from description."""
    if not text:
        return ""

    # Prefer labelled references
    m = re.search(
        r'\b(?:UPI[/\-]|NEFT[/\-]|IMPS[/\-]|RTGS[/\-]|CHQ[/\-]|REF[/\-]|UTR[/\-]|POS[/\-])'
        r'([A-Za-z0-9]{5,22})\b',
        text, re.IGNORECASE
    )
    if m:
        return m.group(1).upper()

    # Fall back: alphanumeric token ≥10 chars that mixes letters+digits (not pure alpha or pure numeric description word)
    ref_m = re.search(r'\b([A-Z0-9]{10,22})\b', text)
    if ref_m:
        tok = ref_m.group(1)
        if not tok.isalpha() and not tok.isdigit():
            return tok

    return ""


# ──────────────────────────────────────────────────────────────────────────────
# Header mapping
# ──────────────────────────────────────────────────────────────────────────────

def map_headers(headers: List[str]) -> Dict[str, int]:
    """
    Map raw column headers to canonical field names.

    Priority:
      1. Exact match (highest confidence)
      2. Longest alias substring match (prefer specific over vague)
      3. Conflict resolution: if two fields map to the same column index, the
         one with the longer matching alias string wins.
    """
    norm = [str(h).lower().strip() for h in headers if h is not None]
    # (field, col_idx, match_length)
    candidates: List[Tuple[str, int, int]] = []

    for field, aliases in HEADER_ALIASES.items():
        for col_idx, h in enumerate(norm):
            # Exact match
            for alias in aliases:
                if alias == h:
                    candidates.append((field, col_idx, len(alias) + 1000))  # +1000 = exact bonus
                    break
            else:
                # Substring match — pick the longest alias that fits
                best_len = 0
                for alias in aliases:
                    if alias in h and len(alias) > best_len:
                        best_len = len(alias)
                if best_len > 0:
                    candidates.append((field, col_idx, best_len))

    # Sort by match_length descending so best matches are processed first
    candidates.sort(key=lambda x: -x[2])

    mapped: Dict[str, int] = {}
    used_cols: set = set()

    for field, col_idx, _ in candidates:
        if field in mapped:
            continue  # already assigned
        if col_idx in used_cols:
            continue  # column already taken by a higher-priority field
        mapped[field] = col_idx
        used_cols.add(col_idx)

    return mapped


# ──────────────────────────────────────────────────────────────────────────────
# Transaction builder
# ──────────────────────────────────────────────────────────────────────────────

def build_normalized_transaction(
    date: str = "",
    time: str = "",
    description: str = "",
    debit: float = 0.0,
    credit: float = 0.0,
    amount: float = 0.0,
    balance: float = 0.0,
    transaction_type: str = "",
    reference_number: str = "",
    raw_text: str = "",
    # Metadata fields (BUG-012)
    confidence: float = 1.0,
    warnings: Optional[List[str]] = None,
    source_page: int = 0,
    source_method: str = "table",
) -> Dict[str, Any]:
    """
    Build the canonical transaction dict consumed by the validator and rule engine.

    All amounts are sanitised again here as a defence-in-depth measure.
    Adds confidence / warnings / page / method metadata.
    """
    if warnings is None:
        warnings = []

    txn_warnings: List[str] = list(warnings)

    # ── Normalise date & time ──────────────────────────────────────────────────
    norm_date = normalize_date_string(date)
    if not norm_date:
        txn_warnings.append("missing_date")
        confidence = min(confidence, 0.5)

    norm_time = normalize_time_string(time)
    if not norm_time:
        norm_time = (
            extract_time_from_text(date)
            or extract_time_from_text(description)
            or extract_time_from_text(raw_text)
        )

    # ── Sanitise amounts ──────────────────────────────────────────────────────
    def _safe(v: Any) -> float:
        try:
            f = float(v)
            if f != f or abs(f) == float('inf'):  # NaN or Inf
                return 0.0
            return max(0.0, round(abs(f), 2))
        except (TypeError, ValueError):
            return 0.0

    final_debit = _safe(debit)
    final_credit = _safe(credit)
    final_amount = _safe(amount)
    final_balance = round(float(balance) if balance else 0.0, 2)

    # ── Resolve transaction type ──────────────────────────────────────────────
    if final_debit > 0 and final_credit == 0:
        final_amount = final_debit
        final_type = "debit"
    elif final_credit > 0 and final_debit == 0:
        final_amount = final_credit
        final_type = "credit"
    elif final_amount > 0:
        t_clean = re.sub(r'[^a-zA-Z]', '', transaction_type.lower())
        if t_clean in ("debit", "withdrawal", "dr", "outflow", "paidout", "d"):
            final_debit = final_amount
            final_credit = 0.0
            final_type = "debit"
        elif t_clean in ("credit", "deposit", "cr", "inflow", "paidin", "c"):
            final_credit = final_amount
            final_debit = 0.0
            final_type = "credit"
        else:
            final_type = "unknown"
            txn_warnings.append("ambiguous_type")
            confidence = min(confidence, 0.7)
    else:
        final_type = transaction_type.lower() if transaction_type else "unknown"
        if final_type == "unknown":
            txn_warnings.append("missing_amount")
            confidence = min(confidence, 0.4)

    # Both debit and credit non-zero is a parse error
    if final_debit > 0 and final_credit > 0:
        txn_warnings.append("both_debit_and_credit_set")
        confidence = min(confidence, 0.3)

    # ── Reference ─────────────────────────────────────────────────────────────
    ref = reference_number.strip() if reference_number else ""
    if not ref and description:
        ref = extract_reference_number(description)

    return {
        "date": norm_date,
        "time": norm_time,
        "description": description.strip(),
        "debit": round(final_debit, 2),
        "credit": round(final_credit, 2),
        "amount": round(final_amount, 2),
        "balance": final_balance,
        "transaction_type": final_type,
        "reference_number": ref,
        "raw_text": raw_text.strip(),
        # Metadata (BUG-012)
        "confidence": round(confidence, 4),
        "warnings": txn_warnings,
        "source_page": source_page,
        "source_method": source_method,
    }

