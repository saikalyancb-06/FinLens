"""Rate lookup and amount re-denomination.

The one rule this module exists to enforce: a stored amount is never rewritten.
`transactions.debit_paise` stays exactly what the bank posted, and every figure
here is a display value computed on read from that original.

That is not a stylistic preference. Conversion is lossy in both directions - a
US cent is 0.88 rupees, so a rupee amount rounded to cents and back can land up
to 44 paise from where it started. Converting only ever from the stored INR
means switching the Transactions tab to USD and back shows the exact original,
because the second switch re-reads the untouched column rather than converting a
converted number. Persisting the converted value instead would make that drift
permanent and compound it on every subsequent switch.
"""

from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.currency import Currency, CurrencyRate

# The base currency of the ledger. Every `*_paise` column is minor units of this
# unless the row says otherwise via `booked_currency`.
BASE_CURRENCY = "INR"

# Indicative seed rates. These are a starting point so the feature works out of
# the box, NOT a market feed: they are flat across all dates and were not
# sourced from a rate provider. Anything that matters - a statutory filing, a
# reported figure, a reconciliation against a bank advice - needs the real rate
# for the real date, entered through the rate editor. The API reports
# `source: "seed"` on every untouched row so this stays visible downstream.
#
# (code, name, symbol, decimals, indicative INR per unit, display order)
SEED_CURRENCIES: List[Tuple[str, str, str, int, str, int]] = [
    ("INR", "Indian Rupee",         "₹", 2, "1.00000000",   1),
    ("USD", "US Dollar",            "$",      2, "88.00000000",  2),
    ("EUR", "Euro",                 "€", 2, "95.50000000",  3),
    ("GBP", "Pound Sterling",       "£", 2, "112.00000000", 4),
    ("AED", "UAE Dirham",           "AED",    2, "24.00000000",  5),
    ("SGD", "Singapore Dollar",     "S$",     2, "65.00000000",  6),
    ("JPY", "Japanese Yen",         "¥", 0, "0.58000000",   7),
    ("AUD", "Australian Dollar",    "A$",     2, "57.50000000",  8),
    ("CAD", "Canadian Dollar",      "C$",     2, "63.00000000",  9),
    ("CHF", "Swiss Franc",          "CHF",    2, "101.00000000", 10),
    ("HKD", "Hong Kong Dollar",     "HK$",    2, "11.30000000",  11),
    ("SAR", "Saudi Riyal",          "SAR",    2, "23.50000000",  12),
    ("SEK", "Swedish Krona",        "kr",     2, "8.40000000",   13),
]

# The date seed rates are stamped with. Deliberately old: a rate dated far in
# the past is obviously a placeholder, and any real rate a user enters for an
# actual date will win the "latest on or before" lookup automatically.
SEED_AS_OF = date_type(2000, 1, 1)

# ISO 4217 minor-unit counts, keyed by code. A currency's decimals are a fixed
# property of the currency, not user data, so this is safe to read without a
# database round trip when all that is needed is how to render a stored integer.
DECIMALS_BY_CODE: Dict[str, int] = {c[0]: c[3] for c in SEED_CURRENCIES}
SYMBOL_BY_CODE: Dict[str, str] = {c[0]: c[2] for c in SEED_CURRENCIES}


def seed_currencies(db: Session, *, overwrite_rates: bool = False) -> Dict[str, int]:
    """Insert the seed currencies and their indicative rates if absent.

    Idempotent, and by default it will not touch a rate a user has edited:
    re-running the seeder must never silently revert a hand-entered rate back to
    the shipped placeholder.
    """
    added_ccy = 0
    added_rate = 0
    for code, name, symbol, decimals, rate, order in SEED_CURRENCIES:
        ccy = db.get(Currency, code)
        if ccy is None:
            db.add(Currency(code=code, name=name, symbol=symbol,
                            decimals=decimals, display_order=order))
            added_ccy += 1
        else:
            # Metadata is ours to correct; the rate is not.
            ccy.name, ccy.symbol = name, symbol
            ccy.decimals, ccy.display_order = decimals, order

        existing = db.execute(
            select(CurrencyRate).where(CurrencyRate.code == code,
                                       CurrencyRate.as_of == SEED_AS_OF)
        ).scalar_one_or_none()
        if existing is None:
            db.add(CurrencyRate(code=code, as_of=SEED_AS_OF,
                                inr_per_unit=Decimal(rate), source="seed",
                                note="Indicative placeholder, not a market rate"))
            added_rate += 1
        elif overwrite_rates and existing.source == "seed":
            existing.inr_per_unit = Decimal(rate)

    db.flush()
    return {"currencies_added": added_ccy, "rates_added": added_rate}


def currency_map(db: Session) -> Dict[str, Currency]:
    """Every active currency, keyed by code."""
    rows = db.execute(select(Currency).where(Currency.is_active.is_(True))).scalars()
    return {c.code: c for c in rows}


def rate_on(db: Session, code: str, on: Optional[date_type] = None) -> Optional[Decimal]:
    """INR per one unit of `code`, using the newest rate not after `on`.

    `on=None` means today, so a bare `rate_on(db, "EUR")` is "the current rate".
    It used to mean "skip the date filter", which fell through to the *earliest*
    row on file - almost always the shipped placeholder. Nothing looked broken:
    the caller asking for today's rate quietly got a rate from the year 2000, and
    a "value this at today's price" view returned the same number as the
    historical one.

    Falling *forward* when no earlier rate exists is intentional and unchanged. A
    transaction dated before the first rate anyone entered is otherwise
    unconvertible, and a blank cell is worse than the earliest known rate -
    provided the caller surfaces that it is approximate, which the API does via
    `display_is_exact`.
    """
    if code == BASE_CURRENCY:
        return Decimal(1)

    stmt = select(CurrencyRate).where(CurrencyRate.code == code)
    backward = db.execute(
        stmt.where(CurrencyRate.as_of <= (on or date_type.today()))
        .order_by(CurrencyRate.as_of.desc()).limit(1)
    ).scalar_one_or_none()
    if backward is not None:
        return Decimal(backward.inr_per_unit)

    forward = db.execute(
        stmt.order_by(CurrencyRate.as_of.asc()).limit(1)
    ).scalar_one_or_none()
    return Decimal(forward.inr_per_unit) if forward is not None else None


def rates_for_dates(
    db: Session, code: str, dates: Iterable[date_type]
) -> Dict[date_type, Optional[Decimal]]:
    """Resolve many dates against one currency in a single pass.

    The list endpoint converts up to 50,000 rows. Calling `rate_on` per row
    would issue one query per row; loading the currency's whole rate history
    once and bisecting in memory is a fixed two queries regardless of row count.
    """
    if code == BASE_CURRENCY:
        return {d: Decimal(1) for d in dates}

    history = db.execute(
        select(CurrencyRate.as_of, CurrencyRate.inr_per_unit)
        .where(CurrencyRate.code == code)
        .order_by(CurrencyRate.as_of.asc())
    ).all()
    if not history:
        return {d: None for d in dates}

    import bisect

    keys = [row[0] for row in history]
    vals = [Decimal(row[1]) for row in history]
    earliest = vals[0]

    out: Dict[date_type, Optional[Decimal]] = {}
    for d in dates:
        if d is None:
            out[d] = vals[-1]
            continue
        i = bisect.bisect_right(keys, d) - 1
        out[d] = vals[i] if i >= 0 else earliest
    return out


def _scale(decimals: int) -> Decimal:
    return Decimal(10) ** decimals


def convert_minor(
    amount_minor: Optional[int],
    from_code: str,
    to_code: str,
    from_decimals: int,
    to_decimals: int,
    from_rate: Optional[Decimal],
    to_rate: Optional[Decimal],
) -> Optional[int]:
    """Convert minor units of one currency to minor units of another.

    Routed through INR rather than a direct pair rate, because INR is the only
    leg the rate table stores. The cost is one extra rounding step; the benefit
    is that adding a currency needs one rate, not one rate per existing pair.

    Rounds half-up at the target's own precision - the convention Indian bank
    statements use - and only at the very end, so a JPY-to-GBP conversion does
    not round to whole yen on the way through.
    """
    if amount_minor is None:
        return None
    if from_code == to_code:
        return amount_minor
    if from_rate is None or to_rate is None or to_rate == 0:
        return None

    major = Decimal(amount_minor) / _scale(from_decimals)
    inr = major * from_rate
    target_major = inr / to_rate
    return int((target_major * _scale(to_decimals)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def format_minor(amount_minor: Optional[int], decimals: int) -> Optional[float]:
    """Minor units to a display float. Presentation only - never arithmetic."""
    if amount_minor is None:
        return None
    return float(Decimal(amount_minor) / _scale(decimals))
