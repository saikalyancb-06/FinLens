"""Extract the foreign-currency leg from a bank narration.

"Actual" mode in the Transactions tab is only honest if the foreign amount comes
off the bank's own advice. Deriving it by dividing the booked INR by a rate from
our table would produce a figure that differs from the customer's document by
the bank's margin - small, consistent, and exactly the kind of discrepancy that
costs an afternoon during a reconciliation.

So this parser is deliberately conservative. It returns a result only when the
narration actually names a currency and an amount; it never guesses, never falls
back to a table rate, and returns None far more often than not. A missing FX leg
shows the row in INR, which is true. A wrong one shows a number that was never
on any document, which is worse than nothing.

WHAT IT WILL NOT DO
    - Infer currency from the counterparty's country.
    - Treat a bare number near the word USD as an amount without a separator.
    - Accept an amount that would imply an implausible rate when the narration
      also quotes one (the cross-check below).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from app.currency.service import DECIMALS_BY_CODE

# Only currencies the rate table knows. A narration mentioning ZAR is left
# unparsed rather than producing a row that cannot be converted or displayed.
_KNOWN = sorted(c for c in DECIMALS_BY_CODE if c != "INR")
_CODES = "|".join(_KNOWN)

# "USD 13,079.09"  /  "USD13079.09"  /  "EUR 2,537.27"
_AMOUNT_AFTER = re.compile(
    rf"\b(?P<code>{_CODES})\s*(?P<amount>\d{{1,3}}(?:,\d{{2,3}})*(?:\.\d{{1,4}})?|\d+\.\d{{1,4}})\b"
)
# "13,079.09 USD"
_AMOUNT_BEFORE = re.compile(
    rf"\b(?P<amount>\d{{1,3}}(?:,\d{{2,3}})*(?:\.\d{{1,4}})?|\d+\.\d{{1,4}})\s*(?P<code>{_CODES})\b"
)
# "@ 88.71"  /  "RATE 88.7100"  /  "@88.71"
_RATE = re.compile(r"(?:@|\bRATE\b|\bEXCH\s*RATE\b|\bFX\s*RATE\b)\s*:?\s*(?P<rate>\d+(?:\.\d{1,6})?)")

# Words that mean the number next to a currency code is the bank's fee rather
# than the transaction. Position matters and a bare keyword search is not enough:
# "SWIFT CHARGES USD 25.00" is a fee line, but "USD 306.36 MANAGEMENT FEE
# RECEIPT" is a 306-dollar receipt that merely contains the word "fee". Only a
# charge word immediately *preceding* the currency code disqualifies the amount.
_CHARGE_HINT = re.compile(
    r"\b(CHARGE|CHARGES|COMMISSION|FEE|FEES|GST|TCS|TDS|LEVY|WITHHOLDING)\b"
)
_CHARGE_LOOKBACK = 24  # characters


@dataclass(frozen=True)
class FxLeg:
    currency: str
    amount_minor: int
    rate: Optional[Decimal]      # INR per one unit, as quoted in the narration
    quoted_rate: bool            # True when the rate came off the advice


def _to_minor(amount: str, code: str) -> Optional[int]:
    try:
        value = Decimal(amount.replace(",", ""))
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    scale = Decimal(10) ** DECIMALS_BY_CODE.get(code, 2)
    return int((value * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_fx_leg(narration: str, *, booked_minor: Optional[int] = None) -> Optional[FxLeg]:
    """Return the foreign leg named in `narration`, or None.

    `booked_minor` is the INR paise the bank actually posted. When both it and a
    quoted rate are present, the three numbers must agree to within 5% or the
    parse is rejected: a narration where the amount, the rate and the booked
    total do not reconcile has been misread, and the safe response is to show
    the row in INR rather than to publish an invented foreign figure.
    """
    if not narration:
        return None
    text = narration.upper()

    match = _AMOUNT_AFTER.search(text) or _AMOUNT_BEFORE.search(text)
    if match is None:
        return None

    code = match.group("code")
    amount_minor = _to_minor(match.group("amount"), code)
    if amount_minor is None:
        return None

    # A bank-charge line quotes a currency too, but the figure is the fee.
    lead = text[max(0, match.start() - _CHARGE_LOOKBACK):match.start()]
    if _CHARGE_HINT.search(lead):
        return None

    rate_match = _RATE.search(text)
    rate: Optional[Decimal] = None
    if rate_match:
        try:
            candidate = Decimal(rate_match.group("rate"))
        except InvalidOperation:
            candidate = None
        # "@ 0.58" for JPY is a rate; "@ 2" almost certainly is not.
        if candidate is not None and Decimal("0.001") <= candidate <= Decimal("100000"):
            rate = candidate

    if rate is not None and booked_minor:
        implied_inr = (Decimal(amount_minor) /
                       (Decimal(10) ** DECIMALS_BY_CODE.get(code, 2))) * rate
        booked_inr = Decimal(booked_minor) / 100
        if booked_inr > 0:
            drift = abs(implied_inr - booked_inr) / booked_inr
            if drift > Decimal("0.05"):
                return None

    return FxLeg(currency=code, amount_minor=amount_minor, rate=rate,
                 quoted_rate=rate is not None)


def derive_rate(leg: FxLeg, booked_minor: Optional[int]) -> Optional[Decimal]:
    """The effective INR-per-unit for this row.

    Prefers the rate the bank quoted. Falls back to the one implied by the two
    amounts, which is exact by construction because both came off the same
    advice - unlike a table rate, which did not.
    """
    if leg.rate is not None:
        return leg.rate
    if not booked_minor or leg.amount_minor <= 0:
        return None
    major = Decimal(leg.amount_minor) / (Decimal(10) ** DECIMALS_BY_CODE.get(leg.currency, 2))
    if major == 0:
        return None
    return (Decimal(booked_minor) / 100 / major).quantize(Decimal("0.00000001"))
