"""Pluggable sources of INR exchange rates.

Both concrete sources are the same keyless JSON API asked for a different
publisher: FBIL for India's official reference rate, and the central-bank
aggregate for the currencies FBIL does not cover. There is no scraper here any
more - see the module docstring in `frankfurter.py` for why the RBI one was
deleted rather than fixed.
"""

from app.currency.sources.base import (  # noqa: F401
    MAX_STEP_CHANGE,
    RateQuote,
    RateSource,
    RateSourceError,
    RejectedQuote,
    parse_rate,
    validate,
)
from app.currency.sources.frankfurter import (  # noqa: F401
    FbilRateSource,
    FrankfurterRateSource,
)
