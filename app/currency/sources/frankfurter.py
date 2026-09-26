"""Keyless JSON exchange rates, from a chosen benchmark publisher.

One HTTP client serves both sources this application uses, because both are the
same API asked for different publishers:

* **FBIL** - Financial Benchmarks India. Since July 2018 FBIL, not RBI, computes
  and publishes India's official reference rate; RBI's own page republishes it.
  This is the number to cite in anything statutory. Covers USD, EUR, GBP, JPY
  and AED.
* **default** - the aggregate of ~80 central banks, used for the seven
  currencies FBIL does not publish (SGD, AUD, CAD, CHF, HKD, SAR, SEK). These
  are market reference rates with no standing under Indian tax or FEMA practice.

Every quote carries the publisher in `source`, so a figure derived from the
official rate stays distinguishable from one derived from a market rate all the
way to the UI badge.

WHY NOT SCRAPE rbi.org.in
An earlier version did, because RBI publishes only an ASP.NET page. It was ~250
lines of fragile parsing, and the site answers HTTP 418 to every automated
request - including its own robots.txt - so it never worked outside a browser.
Getting around that would have meant impersonating one, which is bot-detection
evasion. The rate was never the problem; the server was. FBIL publishes the same
benchmark as JSON, so the scraper was deleted rather than worked around.

The response parsing is deliberately tolerant about *shape* and strict about
*content*: it accepts any of the response layouts the service has used, but
refuses anything that is not a positive number for a currency that was asked for.
"""

from __future__ import annotations

import logging
from datetime import date as date_type, timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Sequence

from app.currency.sources.base import RateQuote, RateSourceError

logger = logging.getLogger(__name__)

BASE_URL = "https://api.frankfurter.dev/v2/rates"

# India's official reference rate, published daily by FBIL.
FBIL_CURRENCIES = ("USD", "EUR", "GBP", "JPY", "AED")

# Everything else in the seeded table, from the general central-bank aggregate.
FALLBACK_CURRENCIES = ("SGD", "AUD", "CAD", "CHF", "HKD", "SAR", "SEK",
                       "AED", "USD", "GBP", "EUR", "JPY")

# The v2 API answers with a flat array of records:
#   [{"date":"2026-08-18","base":"INR","quote":"USD","rate":0.01047}, ...]
# for both a single date and a time series. Older versions nested a mapping
# under "rates"; both shapes are accepted so an upgrade at their end does not
# silently zero out this source.
_RATE_KEYS = ("rates", "quotes")


def _as_decimal(value: object) -> Optional[Decimal]:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return out if out > 0 else None


def _extract_by_date(payload: object) -> Dict[Optional[str], Dict[str, Decimal]]:
    """Normalise every response shape into {date_or_None: {code: rate}}.

    Rather than branch on which endpoint was called, this inspects what actually
    came back - the endpoint contract is the thing most likely to drift, and the
    shape is self-describing.
    """
    # Current shape: a flat list of one record per (date, quote).
    if isinstance(payload, list):
        out: Dict[Optional[str], Dict[str, Decimal]] = {}
        for record in payload:
            if not isinstance(record, dict):
                continue
            code = str(record.get("quote") or record.get("symbol") or "").upper()
            value = _as_decimal(record.get("rate"))
            if not code or value is None:
                continue
            out.setdefault(record.get("date"), {})[code] = value
        if not out:
            raise RateSourceError(
                "Rate API returned a list with no usable {date, quote, rate} "
                f"records (got {len(payload)} items)")
        return out

    if not isinstance(payload, dict):
        raise RateSourceError(
            f"Rate API returned {type(payload).__name__}, expected a list or object")

    container: Optional[dict] = None
    for key in _RATE_KEYS:
        if isinstance(payload.get(key), dict):
            container = payload[key]
            break
    if container is None:
        raise RateSourceError(
            f"Rate API response has no recognised rates mapping "
            f"(looked for {', '.join(_RATE_KEYS)}); got keys: "
            f"{sorted(payload)[:8]}"
        )

    # Time series: every value is itself a mapping keyed by currency.
    if container and all(isinstance(v, dict) for v in container.values()):
        out: Dict[Optional[str], Dict[str, Decimal]] = {}
        for day, mapping in container.items():
            parsed = {k.upper(): _as_decimal(v) for k, v in mapping.items()}
            out[day] = {k: v for k, v in parsed.items() if v is not None}
        return out

    flat = {k.upper(): _as_decimal(v) for k, v in container.items()}
    single_date = payload.get("date") or payload.get("as_of")
    return {single_date: {k: v for k, v in flat.items() if v is not None}}


class FrankfurterRateSource:
    """Fetches INR rates from a keyless public API."""

    name = "frankfurter"
    supported = FALLBACK_CURRENCIES
    #: Publisher to request. None = the service's default aggregate.
    providers: Optional[str] = None
    note: Optional[str] = "Market reference rate, no statutory standing in India"

    def __init__(self, timeout: float = 15.0, url: str = BASE_URL,
                 range_timeout: Optional[float] = None) -> None:
        self.timeout = timeout
        # A date range returns one record per currency per day, so a year of
        # twelve currencies is ~3,000 records against a single day's twelve.
        # Reusing the latest-rate timeout made long backfills fail on nothing
        # but payload size, and the script reported "nothing was stored" for
        # what was really a too-tight deadline.
        self.range_timeout = range_timeout if range_timeout is not None else timeout * 4
        self.url = url

    def _get(self, params: Dict[str, str], timeout: Optional[float] = None) -> object:
        import httpx
        try:
            with httpx.Client(timeout=timeout or self.timeout, follow_redirects=True,
                              headers={"User-Agent": "KredoTreasury/1.0"}) as client:
                res = client.get(self.url, params=params)
        except Exception as exc:
            # A read/connect timeout is the signature of an over-large window,
            # so it is worth retrying smaller. Anything else is not.
            timed_out = "timeout" in f"{type(exc).__name__} {exc}".lower()
            raise RateSourceError(f"Rate API unreachable: {exc}",
                                  retryable=timed_out) from exc

        if res.status_code != 200:
            raise RateSourceError(
                f"Rate API returned HTTP {res.status_code}: {res.text[:160]}")
        try:
            payload = res.json()
        except ValueError as exc:
            raise RateSourceError("Rate API did not return JSON") from exc
        if not isinstance(payload, (dict, list)):
            raise RateSourceError(
                f"Rate API returned {type(payload).__name__}, expected a list or object")
        return payload

    @staticmethod
    def _to_quotes(
        by_date: Dict[Optional[str], Dict[str, Decimal]],
        wanted: Iterable[str],
        fallback_date: date_type,
        source: str = "frankfurter",
        note: Optional[str] = "Market reference rate, no statutory standing in India",
    ) -> List[RateQuote]:
        """Invert base=INR quotes into INR-per-unit.

        The API answers "how many SGD is one INR"; the ledger stores "how many
        INR is one SGD". Inverting here rather than at read time keeps one
        convention in the database - mixing the two is the kind of mistake that
        looks right for the currencies near parity and is wildly wrong for yen.
        """
        wanted_set = {c.upper() for c in wanted}
        quotes: List[RateQuote] = []
        for day, mapping in by_date.items():
            as_of = fallback_date
            if isinstance(day, str):
                try:
                    as_of = date_type.fromisoformat(day)
                except ValueError:
                    pass
            for code, per_inr in mapping.items():
                if code not in wanted_set or per_inr <= 0:
                    continue
                quotes.append(RateQuote(
                    code=code,
                    as_of=as_of,
                    inr_per_unit=(Decimal(1) / per_inr),
                    source=source,
                    note=note,
                ))
        return quotes

    def _params(self, wanted: Sequence[str]) -> Dict[str, str]:
        params = {"base": "INR", "quotes": ",".join(sorted(wanted))}
        if self.providers:
            params["providers"] = self.providers
        return params

    def fetch_latest(self, codes: Sequence[str]) -> List[RateQuote]:
        wanted = [c.upper() for c in codes if c.upper() in self.supported]
        if not wanted:
            return []
        payload = self._get(self._params(wanted))
        return self._to_quotes(_extract_by_date(payload), wanted, date_type.today(),
                               source=self.name, note=self.note)

    def fetch_range(
        self, codes: Sequence[str], start: date_type, end: date_type
    ) -> List[RateQuote]:
        wanted = [c.upper() for c in codes if c.upper() in self.supported]
        if not wanted:
            return []
        params = self._params(wanted)
        params.update({"from": start.isoformat(), "to": end.isoformat()})
        payload = self._get(params, timeout=self.range_timeout)
        return self._to_quotes(_extract_by_date(payload), wanted, end,
                               source=self.name, note=self.note)


class FbilRateSource(FrankfurterRateSource):
    """India's official reference rate, published daily by FBIL since July 2018.

    Same endpoint, same client - only the publisher differs. Asked first for the
    five currencies it covers, because falling back to a market rate when the
    official one is available would quietly downgrade the provenance of a number
    that may end up in a filing.
    """

    name = "fbil"
    supported = FBIL_CURRENCIES
    providers = "FBIL"
    note = "FBIL reference rate (India's official benchmark)"
