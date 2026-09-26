"""Report period resolution for the Treasury Reports page.

The page used to offer only a raw start/end date pair, which is fine for an
ad-hoc query and useless for the thing treasury people actually do every month:
compare this period against the last comparable one. That comparison is only
meaningful if both periods are derived by the same rule, so the rule lives here
rather than being re-implemented in the browser.

Two decisions worth stating:

* Periods are resolved on the server. A browser in a different timezone would
  otherwise disagree with the database about which day "this month" starts on,
  and month-end is exactly when that matters.
* "Previous comparable period" is the same *kind* of period immediately before,
  not "minus 30 days". February against January is a comparison a finance team
  recognises; 28 days against 31 is not.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date as date_type, timedelta
from typing import Optional, Tuple

CURRENT_MONTH = "current_month"
LAST_MONTH = "last_month"
CURRENT_QUARTER = "current_quarter"
LAST_QUARTER = "last_quarter"
FINANCIAL_YEAR = "financial_year"
LAST_FINANCIAL_YEAR = "last_financial_year"
CUSTOM = "custom"
ALL_TIME = "all_time"

PERIODS = (
    CURRENT_MONTH, LAST_MONTH, CURRENT_QUARTER, LAST_QUARTER,
    FINANCIAL_YEAR, LAST_FINANCIAL_YEAR, CUSTOM, ALL_TIME,
)

#: The Indian financial year runs April to March. Using the calendar year here
#: would put Q4 in the wrong year for every Indian filing this report feeds.
FY_START_MONTH = 4


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start: Optional[date_type]
    end: Optional[date_type]
    #: The comparable period immediately before this one, for variance analysis.
    prev_start: Optional[date_type] = None
    prev_end: Optional[date_type] = None
    prev_label: Optional[str] = None

    def as_dict(self) -> dict:
        iso = lambda d: d.isoformat() if d else None
        return {
            "key": self.key,
            "label": self.label,
            "start_date": iso(self.start),
            "end_date": iso(self.end),
            "previous": {
                "label": self.prev_label,
                "start_date": iso(self.prev_start),
                "end_date": iso(self.prev_end),
            } if self.prev_start or self.prev_end else None,
        }


def _month_bounds(year: int, month: int) -> Tuple[date_type, date_type]:
    return (date_type(year, month, 1),
            date_type(year, month, calendar.monthrange(year, month)[1]))


def _shift_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def _quarter_of(d: date_type) -> int:
    """1-4, counted from the start of the financial year, not January."""
    return ((d.month - FY_START_MONTH) % 12) // 3 + 1


def _fy_start(d: date_type) -> date_type:
    year = d.year if d.month >= FY_START_MONTH else d.year - 1
    return date_type(year, FY_START_MONTH, 1)


def _quarter_bounds(d: date_type) -> Tuple[date_type, date_type]:
    fy = _fy_start(d)
    q = _quarter_of(d)
    start_year, start_month = _shift_month(fy.year, fy.month, (q - 1) * 3)
    end_year, end_month = _shift_month(start_year, start_month, 2)
    return (date_type(start_year, start_month, 1),
            date_type(end_year, end_month, calendar.monthrange(end_year, end_month)[1]))


def _fy_label(start: date_type) -> str:
    return f"FY {start.year}-{str(start.year + 1)[-2:]}"


def resolve(
    key: Optional[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    today: Optional[date_type] = None,
) -> Period:
    """Turn a period key (plus optional explicit dates) into concrete bounds.

    `today` is injectable so the tests do not have to be re-dated every month.
    """
    today = today or date_type.today()
    key = (key or "").strip().lower() or None

    parse = lambda s: date_type.fromisoformat(s) if s else None

    # Explicit dates win. Somebody who typed a range meant that range, whatever
    # the preset dropdown happens to be showing.
    if key == CUSTOM or (key is None and (start_date or end_date)):
        start, end = parse(start_date), parse(end_date)
        prev_start = prev_end = None
        if start and end:
            span = (end - start).days + 1
            prev_end = start - timedelta(days=1)
            prev_start = prev_end - timedelta(days=span - 1)
        return Period(
            CUSTOM,
            f"{start or 'beginning'} to {end or 'today'}",
            start, end, prev_start, prev_end,
            f"{prev_start} to {prev_end}" if prev_start else None,
        )

    if key == LAST_MONTH:
        y, m = _shift_month(today.year, today.month, -1)
        start, end = _month_bounds(y, m)
        py, pm = _shift_month(y, m, -1)
        p_start, p_end = _month_bounds(py, pm)
        return Period(LAST_MONTH, start.strftime("%B %Y"), start, end,
                      p_start, p_end, p_start.strftime("%B %Y"))

    if key == CURRENT_QUARTER:
        start, end = _quarter_bounds(today)
        p_start, p_end = _quarter_bounds(start - timedelta(days=1))
        return Period(CURRENT_QUARTER, f"Q{_quarter_of(start)} {_fy_label(_fy_start(start))}",
                      start, end, p_start, p_end,
                      f"Q{_quarter_of(p_start)} {_fy_label(_fy_start(p_start))}")

    if key == LAST_QUARTER:
        this_start, _ = _quarter_bounds(today)
        start, end = _quarter_bounds(this_start - timedelta(days=1))
        p_start, p_end = _quarter_bounds(start - timedelta(days=1))
        return Period(LAST_QUARTER, f"Q{_quarter_of(start)} {_fy_label(_fy_start(start))}",
                      start, end, p_start, p_end,
                      f"Q{_quarter_of(p_start)} {_fy_label(_fy_start(p_start))}")

    if key == FINANCIAL_YEAR:
        start = _fy_start(today)
        end = date_type(start.year + 1, FY_START_MONTH, 1) - timedelta(days=1)
        p_start = date_type(start.year - 1, FY_START_MONTH, 1)
        p_end = start - timedelta(days=1)
        return Period(FINANCIAL_YEAR, _fy_label(start), start, end,
                      p_start, p_end, _fy_label(p_start))

    if key == LAST_FINANCIAL_YEAR:
        this = _fy_start(today)
        start = date_type(this.year - 1, FY_START_MONTH, 1)
        end = this - timedelta(days=1)
        p_start = date_type(start.year - 1, FY_START_MONTH, 1)
        p_end = start - timedelta(days=1)
        return Period(LAST_FINANCIAL_YEAR, _fy_label(start), start, end,
                      p_start, p_end, _fy_label(p_start))

    if key == ALL_TIME:
        # No previous period: there is nothing before all of time to compare to,
        # and inventing one would put a meaningless variance on the screen.
        return Period(ALL_TIME, "All time", None, None)

    # Default: the month in progress.
    start, end = _month_bounds(today.year, today.month)
    py, pm = _shift_month(today.year, today.month, -1)
    p_start, p_end = _month_bounds(py, pm)
    return Period(CURRENT_MONTH, start.strftime("%B %Y"), start, end,
                  p_start, p_end, p_start.strftime("%B %Y"))
