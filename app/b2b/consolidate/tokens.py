"""Low-level token parsing shared by every statement layout: dates and amounts.

Indian bank statements write the same things a dozen ways — `05-08-2025`,
`05/08/2025`, `01-JAN-2026`, `23-Mar-26`, `2025-04-01`; `1,03,07,226.42`,
`70,875.00Cr`, `-10786320.64`, `7,97,675.48CR`. Everything is normalised here,
once, so the layout parsers only decide *where* a value is, never how to read it.

Money is held in integer paise from the moment it is read. Floats never touch a
balance check: `0.1 + 0.2 != 0.3` is exactly the kind of error a one-paisa
continuity test would then report as a missing transaction.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional, Tuple

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# Anchored date patterns, most specific first.
_DATE_PATTERNS = [
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$"), "dmy"),
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"), "ymd"),
    (re.compile(r"^(\d{1,2})[-/ ]([A-Za-z]{3})[A-Za-z]*[-/ ,]*(\d{4})$"), "dMy"),
    (re.compile(r"^(\d{1,2})[-/ ]([A-Za-z]{3})[-/ ](\d{2})$"), "dMyy"),
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2})$"), "dmyy"),
]

DATE_SEARCH = re.compile(
    r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[-/ ][A-Za-z]{3}[-/ ]\d{4}"
    r"|\d{1,2}-[A-Za-z]{3}-\d{2}(?!\d))")


def parse_date(text: Optional[str]) -> Optional[_dt.date]:
    """Parse one date token. Returns None for anything that is not a date."""
    if not text:
        return None
    s = text.strip().strip("()").strip()
    for rx, kind in _DATE_PATTERNS:
        m = rx.match(s)
        if not m:
            continue
        try:
            if kind == "dmy":
                d, mo, y = int(m[1]), int(m[2]), int(m[3])
            elif kind == "ymd":
                y, mo, d = int(m[1]), int(m[2]), int(m[3])
            elif kind == "dMy":
                d, mo, y = int(m[1]), _MONTHS.get(m[2].lower()), int(m[3])
            elif kind == "dMyy":
                d, mo, y = int(m[1]), _MONTHS.get(m[2].lower()), 2000 + int(m[3])
            else:
                d, mo, y = int(m[1]), int(m[2]), 2000 + int(m[3])
            if not mo:
                return None
            return _dt.date(y, mo, d)
        except (ValueError, TypeError):
            return None
    return None


def first_date(text: Optional[str]) -> Optional[_dt.date]:
    """The first date found anywhere in a string (cells like '23-Mar-26\\n(23-Mar-26)')."""
    if not text:
        return None
    for m in DATE_SEARCH.finditer(text):
        d = parse_date(m.group(1))
        if d:
            return d
    return None


# An amount token: optional sign, Indian or western grouping, exactly two
# decimals, optional Cr/Dr suffix (with or without a space before it).
AMOUNT_RX = re.compile(r"^(-)?((?:\d{1,3}(?:,\d{2,3})+|\d+)\.\d{2})\s*(CR|DR|Cr|Dr|cr|dr)?$")


def parse_amount(text: Optional[str]) -> Optional[Tuple[int, Optional[str]]]:
    """Parse an amount cell into (paise, suffix) or None.

    `suffix` is 'CR', 'DR' or None. A leading minus is folded into the paise
    value; the suffix is returned separately because its meaning depends on the
    column (on a balance it is a sign, on an amount it is a direction).
    """
    if text is None:
        return None
    s = str(text).strip().replace("\n", "").replace(" ", "")
    if not s or s in {"-", "--", "0", "NIL"}:
        return None
    s = s.replace("INR", "").replace("Rs.", "").replace("₹", "")
    m = AMOUNT_RX.match(s)
    if not m:
        return None
    paise = int(round(float(m[2].replace(",", "")) * 100))
    if m[1]:
        paise = -paise
    suffix = m[3].upper() if m[3] else None
    return paise, suffix


def is_amount(text: str) -> bool:
    return parse_amount(text) is not None


def balance_paise(text: Optional[str]) -> Optional[int]:
    """A running balance, signed: 'Dr' suffix or a minus sign means overdrawn."""
    parsed = parse_amount(text)
    if parsed is None:
        return None
    paise, suffix = parsed
    if suffix == "DR" and paise > 0:
        paise = -paise
    return paise


def fmt(paise: Optional[int]) -> Optional[float]:
    return None if paise is None else round(paise / 100.0, 2)
