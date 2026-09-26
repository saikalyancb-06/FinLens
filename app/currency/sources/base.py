"""Common shape for anything that can produce an INR exchange rate.

Two implementations sit behind this: an HTML scrape of the RBI reference-rate
page, and a JSON API for the currencies RBI does not publish. They agree on
this interface so the refresher does not care which one answered, and so a
third source can be added without touching the scheduling or the sanity gates.

EVERY rate that reaches the database passes through `validate()` first. A rate
is not a display preference - it silently multiplies every converted figure on
the screen - so a scraper that starts returning a page number instead of a
price must fail loudly rather than quietly rewrite the ledger's appearance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Protocol, Sequence

# Sanity band for INR-per-unit. KWD, the world's most valuable currency, sits
# near 290; IDR near 0.005. Anything outside this is not a currency rate.
MIN_PLAUSIBLE = Decimal("0.0001")
MAX_PLAUSIBLE = Decimal("1000")

# A rate that moves more than this against the last known value in one step is
# refused by default. Real currencies do move this much - a devaluation, a peg
# break - but so does a parser that has latched onto the wrong table column, and
# the second is far more likely on any given Tuesday. Refusing means the old
# rate stays and an operator gets told, which is recoverable; accepting means
# every figure on the Transactions tab is silently wrong, which is not.
MAX_STEP_CHANGE = Decimal("0.10")   # 10%


class RateSourceError(RuntimeError):
    """The source could not be reached or its response was unusable.

    `retryable` distinguishes "this request was too big" from "this source said
    no". A timeout on a year-long window may well succeed when split in half; an
    HTTP 403, 418 or 500 will fail identically at any size, and retrying it four
    times just multiplies the wait before reporting the same thing.
    """

    def __init__(self, *args, retryable: bool = False) -> None:
        super().__init__(*args)
        self.retryable = retryable


@dataclass(frozen=True)
class RateQuote:
    """One currency's INR value on one day, and where it came from."""

    code: str
    as_of: date_type
    inr_per_unit: Decimal
    source: str            # "rbi" | "frankfurter" | "manual" | "seed"
    note: Optional[str] = None


@dataclass(frozen=True)
class RejectedQuote:
    quote: RateQuote
    reason: str


def parse_rate(raw: object) -> Optional[Decimal]:
    """Turn a scraped cell into a Decimal, or None if it is not a number.

    RBI prints '-' on bank holidays and occasionally pads cells with
    non-breaking spaces, so this has to tolerate both without treating either
    as zero. Zero is never a valid rate and returning it would make every
    conversion in that currency divide by nothing.
    """
    if raw is None:
        return None
    text = str(raw).replace(" ", " ").replace(",", "").strip()
    if not text or text in {"-", "--", "N/A", "NA", "*"}:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


def validate(
    quote: RateQuote,
    previous: Optional[Decimal] = None,
    *,
    max_step: Decimal = MAX_STEP_CHANGE,
) -> Optional[str]:
    """Return a rejection reason, or None when the quote is acceptable."""
    if quote.inr_per_unit <= 0:
        return "rate is not positive"
    if not (MIN_PLAUSIBLE <= quote.inr_per_unit <= MAX_PLAUSIBLE):
        return (f"rate {quote.inr_per_unit} is outside the plausible band "
                f"{MIN_PLAUSIBLE}-{MAX_PLAUSIBLE} INR per unit")
    if previous is not None and previous > 0:
        drift = abs(quote.inr_per_unit - previous) / previous
        if drift > max_step:
            return (f"rate {quote.inr_per_unit} moves {drift * 100:.1f}% from the "
                    f"last known {previous}, over the {max_step * 100:.0f}% limit")
    return None


class RateSource(Protocol):
    """Anything that can answer 'what was this currency worth in INR'."""

    name: str
    #: Codes this source can actually answer for. The refresher uses this to
    #: route, so a currency RBI does not publish is never requested from it and
    #: never produces a spurious "missing" warning.
    supported: Sequence[str]

    def fetch_latest(self, codes: Sequence[str]) -> List[RateQuote]:
        """The most recent published rate for each code."""
        ...

    def fetch_range(
        self, codes: Sequence[str], start: date_type, end: date_type
    ) -> List[RateQuote]:
        """Every published rate per code between two dates, inclusive."""
        ...
