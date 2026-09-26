"""Recurring payment series detection and 30/60/90-day cash balance forecast.

Implements the specification from §5 of the Treasury Intelligence module:
1. Detects repeating transaction series per entity by counterparty/flow.
2. Identifies regular cadences (daily, weekly, fortnightly, monthly, quarterly, annual) allowing weekend/holiday drift.
3. Classifies confidence into High and Medium tiers.
4. Rolls forward cadences to generate Conservative and Expected 30/60/90-day projected balances.
5. Surfaces driver breakdown for auditability.
"""

from __future__ import annotations

import datetime
import re
import statistics
from collections import defaultdict
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from app.models.transaction import Transaction


CADENCES = [
    ("daily", 1, 1, 4),
    ("weekly", 7, 5, 9),
    ("bi-weekly", 14, 10, 18),
    ("monthly", 30, 19, 45),
    ("quarterly", 90, 70, 115),
    ("semi-annual", 180, 140, 220),
    ("annual", 365, 300, 420),
]


# Compiled once. `re.sub` with a pattern string re-parses the cache key on every
# call, and this pair runs twice per transaction per detection pass.
_CHANNEL_PREFIX_RE = re.compile(r"^(NEFT|IMPS|RTGS|UPI|EBANK|ACH|CMS)[-/:\s]+", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"[0-9]{6,}")


@lru_cache(maxsize=16384)
def _clean_key_from_raw(raw: str) -> str:
    """The grouping key for one already-extracted narration string.

    Split out from `_clean_key` purely so it can be memoised: it is a pure
    function of `raw`, so the same string can only ever produce the same key.
    The forecast backtest replays detection over nine growing prefixes of the
    same ledger, which asked for the same few hundred narrations again and again
    — most of the regex work in that endpoint was recomputing answers it had
    already had.
    """
    raw = _CHANNEL_PREFIX_RE.sub("", raw)
    raw = _LONG_DIGITS_RE.sub("", raw)
    tokens = [tok for tok in raw.split() if len(tok) > 1 and not tok.isdigit()]
    return " ".join(tokens[:4]).title() if tokens else (raw[:30].title() or "Unidentified")


def _clean_key(t: Transaction) -> str:
    """Derive clean counterparty or narration key for grouping."""
    return _clean_key_from_raw(
        (t.counterparty or t.narration_clean or t.narration_raw or "").strip()
    )


def detect_recurring_series(
    transactions: List[Transaction],
    ref_date: Optional[datetime.date] = None,
) -> List[Dict[str, Any]]:
    """Detect recurring transaction series from a list of transactions."""
    if not transactions or len(transactions) < 3:
        return []

    txns = [t for t in transactions if (t.debit_paise or 0) > 0 or (t.credit_paise or 0) > 0]
    if len(txns) < 3:
        return []

    txn_dates = [t.txn_date for t in txns if t.txn_date]
    if not txn_dates:
        return []

    max_txn_date = max(txn_dates)
    if not ref_date or ref_date > max_txn_date:
        ref_date = max_txn_date

    # Group by (flow_direction, counterparty_key)
    groups = defaultdict(list)
    for t in txns:
        if not t.txn_date:
            continue
        direction = "inflow" if (t.credit_paise or 0) > 0 else "outflow"
        amount = t.credit_paise if direction == "inflow" else t.debit_paise
        if not amount or amount <= 0:
            continue
        key = (direction, _clean_key(t).lower())
        groups[key].append((t.txn_date, amount, t))

    series_list: List[Dict[str, Any]] = []

    for (direction, key_label), rows in groups.items():
        if len(rows) < 3:
            continue

        # Sort chronologically
        rows.sort(key=lambda r: r[0])
        dates = [r[0] for r in rows]
        amounts = [r[1] for r in rows]

        # Calculate gaps in days between consecutive occurrences
        gaps = [(dates[i+1] - dates[i]).days for i in range(len(dates) - 1)]
        if not gaps:
            continue

        median_gap = statistics.median(gaps)
        if median_gap < 0:
            continue

        # Match to known cadence
        matched_cadence = None
        for name, nominal_days, min_days, max_days in CADENCES:
            if min_days <= median_gap <= max_days:
                matched_cadence = (name, nominal_days)
                break

        if not matched_cadence:
            continue

        cadence_name, nominal_days = matched_cadence

        # Trailing median amount (last up to 10 occurrences)
        trailing_median_paise = int(statistics.median(amounts[-10:]))

        # Recency check: active in recent history of statement
        days_since_last = (ref_date - dates[-1]).days
        max_allowed_inactive = max(60, int(nominal_days * 3))
        if days_since_last > max_allowed_inactive:
            continue

        # Confidence tiering
        # High: >=6 occurrences, Medium: >=3 occurrences
        tier = "high" if len(rows) >= 6 else "medium"

        display_name = _clean_key(rows[-1][2])
        if len(display_name) > 40:
            display_name = display_name[:37] + "..."

        typical_day = dates[-1].day

        series_list.append({
            "name": display_name,
            "direction": direction,
            "cadence": cadence_name,
            "nominal_days": nominal_days,
            "median_gap_days": round(median_gap, 1),
            "occurrences": len(rows),
            "amount_paise": trailing_median_paise,
            "amount_rupees": round(trailing_median_paise / 100.0, 2),
            "last_date": dates[-1].isoformat(),
            "last_date_obj": dates[-1],
            "typical_day": typical_day,
            "confidence": tier,
        })

    return series_list


def compute_30_60_90_forecast(
    closing_balance_paise: int,
    transactions: List[Transaction],
    history_days: int,
    ref_date: Optional[datetime.date] = None,
) -> Dict[str, Any]:
    """Compute 30/60/90-day projected balance bands based on recurring series and run rate."""
    if history_days < 30:  # Require at least 1 month of history
        return {
            "available": False,
            "status": "Not enough history to project",
            "history_days": history_days,
            "drivers": [],
            "horizons": {
                "day_30": None,
                "day_60": None,
                "day_90": None,
            },
        }

    txn_dates = [t.txn_date for t in transactions if t.txn_date]
    if txn_dates:
        max_txn_date = max(txn_dates)
        if not ref_date or ref_date > max_txn_date:
            ref_date = max_txn_date
    elif not ref_date:
        ref_date = datetime.date.today()

    series = detect_recurring_series(transactions, ref_date=ref_date)
    horizons_days = [30, 60, 90]
    expected_flows: Dict[int, int] = {30: 0, 60: 0, 90: 0}
    conservative_flows: Dict[int, int] = {30: 0, 60: 0, 90: 0}

    if series:
        for s in series:
            nom = max(1, s["nominal_days"])
            amt = s["amount_paise"]
            is_high = (s["confidence"] == "high")
            signed_amt = amt if s["direction"] == "inflow" else -amt

            for h in horizons_days:
                occurrences_in_h = int(h / nom)
                if occurrences_in_h > 0:
                    flow = signed_amt * occurrences_in_h
                    expected_flows[h] += flow
                    if is_high:
                        conservative_flows[h] += flow
    else:
        # Baseline projection from trailing daily run rate
        tot_in = sum(t.credit_paise or 0 for t in transactions)
        tot_out = sum(t.debit_paise or 0 for t in transactions)
        daily_net = int((tot_in - tot_out) / max(1, history_days))
        for h in horizons_days:
            expected_flows[h] = daily_net * h
            conservative_flows[h] = daily_net * h if daily_net <= 0 else 0

    horizons_res = {}
    for h in horizons_days:
        cons_bal = closing_balance_paise + conservative_flows[h]
        exp_bal = closing_balance_paise + expected_flows[h]
        horizons_res[f"day_{h}"] = {
            "conservative_paise": cons_bal,
            "expected_paise": exp_bal,
            "conservative": round(cons_bal / 100.0, 2),
            "expected": round(exp_bal / 100.0, 2),
            "net_flow_expected": round(expected_flows[h] / 100.0, 2),
            "net_flow_conservative": round(conservative_flows[h] / 100.0, 2),
        }

    driver_summary = [
        {
            "name": s["name"],
            "direction": s["direction"],
            "cadence": s["cadence"],
            "amount": s["amount_rupees"],
            "typical_day": s["typical_day"],
            "confidence": s["confidence"],
            "occurrences": s["occurrences"],
        }
        for s in sorted(series, key=lambda x: (x["confidence"] != "high", -abs(x["amount_paise"])))
    ]

    return {
        "available": True,
        "status": "Estimated · based on recurring patterns",
        "is_estimated": True,
        "history_days": history_days,
        "drivers": driver_summary,
        "horizons": horizons_res,
    }
