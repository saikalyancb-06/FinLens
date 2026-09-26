"""Scrape RBI reference rates from rbi.org.in.

WHY A SCRAPER AT ALL, WHEN JSON APIS EXIST
The RBI reference rate is the rate an Indian auditor expects to see behind a
converted figure. RBI does not publish it as an API - it publishes it as an
ASP.NET page - so a scraper is the only way to get the authoritative number.
For the seven currencies RBI does not cover, `frankfurter.py` is used instead,
because scraping a site that already offers JSON would be strictly worse.

WHAT MAKES THIS PAGE AWKWARD
It is ASP.NET WebForms. Querying a date range is a POST that must echo back the
`__VIEWSTATE`, `__VIEWSTATEGENERATOR` and `__EVENTVALIDATION` tokens harvested
from a prior GET. Those tokens are single-use and tied to the session, so every
query is two requests.

HOW THIS SURVIVES A REDESIGN
The parser does not key on CSS classes, element ids, or column positions - all
three change without warning and all three fail silently when they do. It finds
the header row, reads the currency codes *out of the header*, and maps column
index to currency from what it read. A reordered column, a renamed class, or a
newly added currency all keep working; only a genuine restructure breaks it,
and that raises rather than returning a wrong number.

Every value still passes the sanity gates in `base.py` before it is stored.
Defensive parsing reduces how often this is wrong; it does not make it right.
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_type, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from app.currency.sources.base import (
    RateQuote, RateSourceError, parse_rate,
)

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://www.rbi.org.in/scripts/ReferenceRateArchive.aspx"

# RBI publishes a reference rate for these and no others. Requesting anything
# else from this source is a routing bug, not a missing rate.
RBI_CURRENCIES = ("USD", "GBP", "EUR", "JPY")

# JPY is quoted per 100 yen on the RBI page, not per yen — the column header
# reads "JPY" but the figure is for a hundred of them. Storing it unscaled
# would value the yen at ~58 rupees instead of ~0.58, a factor of 100 on every
# yen transaction in the ledger.
PER_HUNDRED = {"JPY"}

_DATE_FORMATS = ("%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %B %Y")
_CODE_RE = re.compile(r"\b([A-Z]{3})\b")


def _parse_date(text: str) -> Optional[date_type]:
    cleaned = str(text).replace("\xa0", " ").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _cell_text(cell) -> str:
    return cell.get_text(" ", strip=True).replace("\xa0", " ").strip()


def _own_rows(table) -> List[object]:
    """Rows belonging to this table, not to a table nested inside it.

    BeautifulSoup's find_all recurses, so a layout table that wraps the real
    grid reports the grid's rows as its own. Left unfiltered that made the
    wrapper look like the best candidate and shifted every column by one - the
    parser returned confident, plausible, wrong numbers instead of failing.
    """
    return [r for r in table.find_all("tr") if r.find_parent("table") is table]


def _own_cells(row) -> List[object]:
    """Direct cells of this row. `recursive=False` for the same reason."""
    return row.find_all(["td", "th"], recursive=False)


def find_rate_table(soup) -> Tuple[object, int, Dict[int, str]]:
    """Locate the rates table and read its column layout from its own header.

    Returns (table, date_column_index, {column_index: currency_code}).

    Raises rather than guessing. A page that no longer contains a table whose
    header names a date column and at least one currency is a page this parser
    does not understand, and pretending otherwise would put invented numbers
    into a ledger.
    """
    best: Optional[Tuple[object, int, Dict[int, str]]] = None
    best_depth = -1

    for table in soup.find_all("table"):
        # How deeply nested this table is. On a tie, the deeper table wins:
        # the real data grid is always inside the layout chrome, never outside.
        depth = len(table.find_parents("table"))

        for row in _own_rows(table)[:5]:          # header is near the top
            cells = _own_cells(row)
            if len(cells) < 2:
                continue

            date_col: Optional[int] = None
            codes: Dict[int, str] = {}
            for idx, cell in enumerate(cells):
                text = _cell_text(cell).upper()
                if date_col is None and "DATE" in text:
                    date_col = idx
                    continue
                match = _CODE_RE.search(text)
                if match and match.group(1) in RBI_CURRENCIES:
                    codes[idx] = match.group(1)

            if date_col is not None and codes:
                # Prefer the table naming the most currencies; on a tie prefer
                # the innermost. RBI wraps the data grid in layout tables that
                # also contain the word "Date".
                if best is None or (len(codes), depth) > (len(best[2]), best_depth):
                    best = (table, date_col, codes)
                    best_depth = depth

    if best is None:
        raise RateSourceError(
            "No RBI rate table found: no table has a header naming a date "
            "column and at least one of " + ", ".join(RBI_CURRENCIES) + ". "
            "The page layout has changed and the parser needs updating."
        )
    return best


def parse_archive_html(html: str) -> List[RateQuote]:
    """Turn an archive results page into quotes. Pure function - unit testable."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:                      # pragma: no cover
        raise RateSourceError(
            "beautifulsoup4 is not installed - run: pip install beautifulsoup4 lxml"
        ) from exc

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:                               # lxml missing at runtime
        soup = BeautifulSoup(html, "html.parser")

    table, date_col, codes = find_rate_table(soup)

    quotes: List[RateQuote] = []
    for row in _own_rows(table):
        cells = _own_cells(row)
        if len(cells) <= date_col:
            continue
        as_of = _parse_date(_cell_text(cells[date_col]))
        if as_of is None:
            continue                                 # header or spacer row

        for idx, code in codes.items():
            if idx >= len(cells):
                continue
            value = parse_rate(_cell_text(cells[idx]))
            if value is None:
                continue                             # holiday, '-' cell
            if code in PER_HUNDRED:
                value = value / Decimal(100)
            quotes.append(RateQuote(
                code=code, as_of=as_of, inr_per_unit=value, source="rbi",
                note="RBI reference rate",
            ))
    return quotes


class RbiRateSource:
    """Fetches RBI reference rates over HTTP."""

    name = "rbi"
    supported = RBI_CURRENCIES

    def __init__(self, timeout: float = 20.0, url: str = ARCHIVE_URL) -> None:
        self.timeout = timeout
        self.url = url

    # -- HTTP ---------------------------------------------------------------

    def _client(self):
        import httpx
        return httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                # Identifying the caller is the courteous thing to do and makes
                # this traceable in RBI's logs if it ever misbehaves.
                "User-Agent": "KredoTreasury/1.0 (bank reconciliation; contact: automation@kredo.in)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )

    @staticmethod
    def _hidden_fields(html: str) -> Dict[str, str]:
        """Harvest the ViewState tokens the POST has to echo back."""
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:                   # pragma: no cover
            raise RateSourceError("beautifulsoup4 is not installed") from exc
        soup = BeautifulSoup(html, "html.parser")
        fields: Dict[str, str] = {}
        for inp in soup.find_all("input", attrs={"type": "hidden"}):
            name = inp.get("name")
            if name:
                fields[name] = inp.get("value", "")
        return fields

    def _post_range(self, start: date_type, end: date_type) -> str:
        try:
            return self._post_range_inner(start, end)
        except RateSourceError:
            raise
        except Exception as exc:
            timed_out = "timeout" in f"{type(exc).__name__} {exc}".lower()
            raise RateSourceError(f"RBI unreachable: {exc}",
                                  retryable=timed_out) from exc

    def _post_range_inner(self, start: date_type, end: date_type) -> str:
        with self._client() as client:
            first = client.get(self.url)
            if first.status_code != 200:
                raise RateSourceError(
                    f"RBI returned HTTP {first.status_code} for the archive page")

            form = self._hidden_fields(first.text)
            if "__VIEWSTATE" not in form:
                raise RateSourceError(
                    "RBI archive page has no __VIEWSTATE - the page is not the "
                    "expected ASP.NET form (captive portal or redirect?)")

            # Field names are what the page ships today. They are sent
            # alongside the harvested hidden fields, so an unexpected name is a
            # no-op on the server rather than a crash here.
            form.update({
                "hdnXmlDoc": "",
                "UsrFrmDate": start.strftime("%d/%m/%Y"),
                "UsrToDate": end.strftime("%d/%m/%Y"),
                "txtFromDate": start.strftime("%d/%m/%Y"),
                "txtToDate": end.strftime("%d/%m/%Y"),
                "DDLCurrency": "All",
                "BtnSearch": "Search",
                "btnSubmit": "Submit",
            })

            result = client.post(self.url, data=form)
            if result.status_code != 200:
                # A status code is a decision, not a capacity problem: the same
                # request at half the size gets the same answer.
                raise RateSourceError(
                    f"RBI returned HTTP {result.status_code} for the rate query",
                    retryable=False)
            return result.text

    # -- RateSource ---------------------------------------------------------

    def fetch_latest(self, codes: Sequence[str]) -> List[RateQuote]:
        """Most recent published rate per code.

        Queries the last ten days rather than today: RBI publishes nothing on
        weekends or bank holidays, so 'today' is empty for roughly a third of
        the calendar and a same-day query would report a broken scraper every
        Sunday.
        """
        today = date_type.today()
        quotes = self.fetch_range(codes, today - timedelta(days=10), today)

        newest: Dict[str, RateQuote] = {}
        for q in quotes:
            if q.code not in newest or q.as_of > newest[q.code].as_of:
                newest[q.code] = q
        return list(newest.values())

    def fetch_range(
        self, codes: Sequence[str], start: date_type, end: date_type
    ) -> List[RateQuote]:
        wanted = {c.upper() for c in codes} & set(RBI_CURRENCIES)
        if not wanted:
            return []
        html = self._post_range(start, end)
        return [q for q in parse_archive_html(html) if q.code in wanted]
