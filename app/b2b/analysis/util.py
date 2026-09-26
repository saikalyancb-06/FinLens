"""Shared arithmetic for the analysis layer.

Nothing in here makes a judgement about money; it only counts, buckets and
divides. The judgements live in the sibling modules so that a reviewer can read
one file to check one claim.

Two conventions this whole package obeys:

* **Paise in, rupees out.** Every intermediate is an integer number of minor
  units. `from_minor` is called exactly once per figure, at the point the figure
  is wrapped in a `Metric` for the response.
* **An inferred number is never certain.** `infer()` below is the only way this
  package builds an INFERRED metric, and it clamps confidence at
  `MAX_INFERRED_CONFIDENCE`. `Metric.__post_init__` enforces that an inferred
  metric *has* a confidence; it cannot enforce that the confidence is honest, so
  that is enforced here instead of at 47 call sites.
"""
from __future__ import annotations

import datetime
import statistics
from collections import OrderedDict, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.b2b.canonical import CanonicalTxn, from_minor
from app.b2b.metrics import Metric, inferred

# The ceiling on any inferred confidence.
#
# 0.97 rather than 0.99: every inference in this package rests on a narration
# string a bank wrote for a human, and there is no statement format in which a
# narration is a guarantee. A payroll run labelled "SALARY" can be a director's
# loan; an "EMI" line can be a standing instruction to a relative. Three points
# of doubt is the smallest honest amount, and it means a client can never treat
# an inferred figure as if it were printed on the statement.
MAX_INFERRED_CONFIDENCE = 0.97

# Days in an average month, used only where a rate has to be annualised. Never
# used to count months — months are counted as calendar months, see
# `months_in_span`.
DAYS_PER_MONTH = 30.44


def infer(value: Any, method: str, confidence: float, **kwargs) -> Metric:
    """Build an INFERRED metric with the confidence ceiling applied."""
    return inferred(value, method=method,
                    confidence=min(float(confidence), MAX_INFERRED_CONFIDENCE),
                    **kwargs)


# --------------------------------------------------------------------- rows

def narration_of(t: CanonicalTxn) -> str:
    return (t.narration_clean or t.narration_raw or "").strip()


def sort_key(t: CanonicalTxn) -> Tuple[datetime.date, int]:
    """Statement order: posting date, then the row's position in the file.

    Row index breaks the tie rather than amount or narration, because within one
    day the file's own order is the only record of the sequence the bank applied
    the entries in, and the running balance column is only interpretable in that
    order.
    """
    return (t.txn_date or datetime.date.min,
            t.row_index if t.row_index is not None else 0)


def sorted_rows(txns: Sequence[CanonicalTxn]) -> List[CanonicalTxn]:
    return sorted([t for t in txns if t.txn_date is not None], key=sort_key)


def period_bounds(txns: Sequence[CanonicalTxn]
                  ) -> Tuple[Optional[datetime.date], Optional[datetime.date]]:
    dates = [t.txn_date for t in txns if t.txn_date]
    if not dates:
        return None, None
    return min(dates), max(dates)


def period_days(txns: Sequence[CanonicalTxn]) -> int:
    """Inclusive day count of the statement period."""
    start, end = period_bounds(txns)
    if not start or not end:
        return 0
    return (end - start).days + 1


# ------------------------------------------------------------------- months

def month_key(d: datetime.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _next_month(year: int, month: int) -> Tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def months_in_span(start: Optional[datetime.date],
                   end: Optional[datetime.date]) -> List[str]:
    """Every calendar month the period touches, including months with no rows.

    A month in which nothing happened is a real data point — an income series
    that skips March is less stable than one that does not — so an empty month
    must appear in the series rather than being silently dropped. Grouping only
    the months that have transactions is how a gap turns into a flattering
    average.

    Partial months at either end count as whole months. That is the convention
    lenders already use when they ask for "six-month average income", and it is
    conservative for income (a larger denominator) at the cost of being
    generous for expenses. The `statement` block reports which end months are
    partial so a caller can re-derive it differently if they want to.
    """
    if not start or not end:
        return []
    out: List[str] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = _next_month(y, m)
    return out


def monthly_buckets(txns: Sequence[CanonicalTxn]) -> "OrderedDict[str, Dict[str, int]]":
    """{month: {inflow, outflow, net, count}} in paise, empty months included."""
    start, end = period_bounds(txns)
    buckets: "OrderedDict[str, Dict[str, int]]" = OrderedDict(
        (m, {"inflow": 0, "outflow": 0, "net": 0, "count": 0})
        for m in months_in_span(start, end)
    )
    for t in txns:
        if not t.txn_date:
            continue
        b = buckets.setdefault(month_key(t.txn_date),
                               {"inflow": 0, "outflow": 0, "net": 0, "count": 0})
        b["inflow"] += int(t.credit_paise or 0)
        b["outflow"] += int(t.debit_paise or 0)
        b["net"] += t.signed_paise
        b["count"] += 1
    return buckets


def months_covered(txns: Sequence[CanonicalTxn]) -> int:
    start, end = period_bounds(txns)
    return len(months_in_span(start, end))


# ---------------------------------------------------------------- statistics

def safe_div(numerator: float, denominator: float) -> Optional[float]:
    if not denominator:
        return None
    return numerator / denominator


def mean(values: Sequence[float]) -> Optional[float]:
    return statistics.fmean(values) if values else None


def coefficient_of_variation(values: Sequence[float]) -> Optional[float]:
    """Population standard deviation over the mean magnitude.

    Population rather than sample: the months in a statement are the whole
    population of months the statement covers, not a sample drawn from a longer
    history we are trying to estimate. Using the sample form would inflate the
    spread on a short statement, which is exactly the case where the figure is
    already least reliable.

    `abs(mean)` because a net-cashflow series can average to a negative number,
    and a negative CV is meaningless.
    """
    vals = [float(v) for v in values]
    if len(vals) < 2:
        return None
    avg = statistics.fmean(vals)
    if avg == 0:
        return None
    return statistics.pstdev(vals) / abs(avg)


def stability_from_cv(cv: Optional[float]) -> Optional[float]:
    """Turn a coefficient of variation into a 0-1 stability score.

    `1 / (1 + cv)`, not `1 - cv`. Both put a perfectly flat series at 1.0, but
    subtraction goes negative as soon as the standard deviation exceeds the
    mean, which happens routinely on a real expense series and would have to be
    clamped — and a clamp destroys the ordering between "volatile" and "wildly
    volatile". The reciprocal form is monotone, bounded in (0, 1] and never
    needs clamping: cv=0 -> 1.00, cv=0.25 -> 0.80, cv=1 -> 0.50, cv=4 -> 0.20.
    """
    if cv is None:
        return None
    return 1.0 / (1.0 + max(0.0, cv))


def day_of_month_spread(dates: Sequence[datetime.date]) -> Optional[float]:
    """Population stdev of the day-of-month, for cadence-regularity tests."""
    if len(dates) < 2:
        return None
    return statistics.pstdev([d.day for d in dates])


# ------------------------------------------------- recurring-series plumbing

def clean_key(t: CanonicalTxn) -> str:
    """The same grouping key `detect_recurring_series` uses, so we can rejoin.

    The detector returns a summary per series and not the rows that made it, but
    almost every judgement downstream — is this salary, is this an EMI, how
    stable is the amount — needs the rows. Rather than reimplement the grouping
    (and drift from it), the detector's own key function is imported and reused,
    so a series here contains exactly the rows the detector counted.
    """
    from app.treasury.recurring_detector import _clean_key
    return _clean_key(t)


def series_groups(txns: Sequence[CanonicalTxn]
                  ) -> Dict[Tuple[str, str], List[CanonicalTxn]]:
    """{(direction, key): rows} mirroring the detector's own grouping."""
    groups: Dict[Tuple[str, str], List[CanonicalTxn]] = defaultdict(list)
    for t in txns:
        if not t.txn_date:
            continue
        direction = "inflow" if (t.credit_paise or 0) > 0 else "outflow"
        if not (t.credit_paise or 0) and not (t.debit_paise or 0):
            continue
        groups[(direction, clean_key(t).lower())].append(t)
    for rows in groups.values():
        rows.sort(key=sort_key)
    return dict(groups)


def series_members(groups: Dict[Tuple[str, str], List[CanonicalTxn]],
                   series: Dict[str, Any]) -> List[CanonicalTxn]:
    """The rows behind one detected series.

    The detector truncates a long display name to 40 characters, so an exact key
    match can miss; the prefix fallback covers that without matching two genuinely
    different series, because the first 37 characters of a narration key are far
    more discriminating than the series count in any real statement.
    """
    name = str(series.get("name") or "").lower()
    direction = series.get("direction")
    exact = groups.get((direction, name))
    if exact:
        return exact
    prefix = name[:37].rstrip(".")
    if not prefix:
        return []
    for (d, key), rows in groups.items():
        if d == direction and key.startswith(prefix):
            return rows
    return []


# ------------------------------------------------------------------ balances

def daily_balance_series(rows: Sequence[CanonicalTxn],
                         opening_paise: Optional[int]
                         ) -> List[Tuple[datetime.date, int]]:
    """One closing balance per calendar day of the period, carried forward.

    A bank balance is a step function: it changes when a transaction posts and
    holds its value on every day in between. Averaging only the days that have
    rows would weight a busy Monday the same as a quiet fortnight and give a
    number no lender would recognise — which is why the time-weighted average
    daily balance is the figure they actually ask for.

    Days before the first balance-carrying row take the period opening balance.
    That is the only defensible fill: it is what the account held before anything
    in this statement happened.

    Returns [] when there is nothing to carry — no balance column and no known
    opening — rather than inventing zeros.
    """
    ordered = sorted_rows(rows)
    if not ordered:
        return []
    start, end = ordered[0].txn_date, ordered[-1].txn_date

    # Last balance stated on each day; a day with several rows takes the last.
    eod: Dict[datetime.date, int] = {}
    for t in ordered:
        if t.balance_paise is not None:
            eod[t.txn_date] = int(t.balance_paise)

    if not eod and opening_paise is None:
        return []

    carried = opening_paise
    out: List[Tuple[datetime.date, int]] = []
    day = start
    while day <= end:
        if day in eod:
            carried = eod[day]
        if carried is not None:
            out.append((day, carried))
        day += datetime.timedelta(days=1)
    return out


def rupees(paise: Optional[int]) -> Optional[float]:
    """Response-boundary conversion. The only place minor units leave."""
    return from_minor(paise) if paise is not None else None


def money_list(items: Iterable[Tuple[str, int]]) -> List[Dict[str, Any]]:
    return [{"label": label, "amount": rupees(amount)} for label, amount in items]
