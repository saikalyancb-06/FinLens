"""Net position, the month-by-month series, and the forward projection.

Almost everything here is CALCULATED: a net flow is a subtraction and a savings
rate is a division. The one inference is the 30/60/90 forecast, which is a claim
about the future built on detected recurring series, and it is labelled as such.

The monthly series includes months with no transactions. That is the whole point
of it — a gap month is the strongest available signal that the extract is
incomplete or that the account went dormant, and dropping it turns both into an
invisible improvement in every stability figure.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from app.b2b.canonical import CanonicalTxn
from app.b2b.metrics import Metric, WarningCollector, calculated, unavailable
from app.b2b.analysis.util import (
    coefficient_of_variation,
    infer,
    monthly_buckets,
    months_covered,
    period_days,
    rupees,
    safe_div,
    stability_from_cv,
)

# The forecaster refuses to project on less than a month of history, and says so
# rather than extrapolating. Mirrored here so the reason reaches the response.
FORECAST_MIN_HISTORY_DAYS = 30

# Forecast confidence by band. The projection is a roll-forward of detected
# cadences, so it is only as good as the series behind it and decays with
# horizon: the further out, the more unmodelled one-offs accumulate.
_FORECAST_CONFIDENCE = {30: 0.70, 60: 0.60, 90: 0.50}


def compute_cashflow(txns: Sequence[CanonicalTxn],
                     balances_raw: Dict[str, Any],
                     income_raw: Dict[str, Any],
                     expenses_raw: Dict[str, Any],
                     warnings: WarningCollector,
                     parse_confidence: float = 1.0,
                     ref_date=None
                     ) -> Tuple[Dict[str, Metric], Dict[str, Any]]:
    buckets = monthly_buckets(txns)
    months = months_covered(txns)
    total_in = income_raw.get("total_credits_paise", 0)
    total_out = expenses_raw.get("total_debits_paise", 0)
    net = total_in - total_out
    monthly_nets = [b["net"] for b in buckets.values()]

    raw: Dict[str, Any] = {
        "net_flow_paise": net,
        "monthly": [
            {"month": m, "inflow": b["inflow"], "outflow": b["outflow"],
             "net": b["net"], "count": b["count"]}
            for m, b in buckets.items()
        ],
        "months": months,
    }

    metrics: Dict[str, Metric] = {}
    metrics["net_flow"] = calculated(
        rupees(net), unit="INR", confidence=parse_confidence,
        method="total_credits_minus_total_debits")

    monthly_net_paise = int(round(net / months)) if months else None
    raw["monthly_net_paise"] = monthly_net_paise
    metrics["monthly_surplus"] = (
        calculated(rupees(monthly_net_paise), unit="INR",
                   confidence=parse_confidence,
                   method="net_flow_divided_by_calendar_months_covered",
                   note="Negative values are a deficit; the key is not renamed "
                        "so a client never has to look in two places.")
        if monthly_net_paise is not None
        else unavailable("The statement period could not be resolved."))

    savings_rate = safe_div(net, total_in)
    raw["savings_rate"] = savings_rate
    metrics["savings_rate"] = (
        calculated(round(savings_rate, 4), unit="ratio",
                   confidence=parse_confidence,
                   method="net_flow_divided_by_total_credits",
                   note="Share of everything that came in that was still there "
                        "at the end. Negative means the account was drawn down.")
        if savings_rate is not None
        else unavailable("There were no credits, so there is no rate to report."))

    cv = coefficient_of_variation(monthly_nets)
    raw["net_cv"] = cv
    metrics["cashflow_stability"] = (
        calculated(round(stability_from_cv(cv), 4), unit="ratio",
                   confidence=parse_confidence,
                   method="one_over_one_plus_coefficient_of_variation_of_monthly_net",
                   note="1.0 is an identical net every month. Computed on the "
                        "spread of monthly net around its own mean, so a "
                        "consistently negative cashflow scores as stable — read "
                        "it beside monthly_surplus, not instead of it.")
        if cv is not None else
        unavailable("At least two calendar months, with a non-zero average net, "
                    "are needed to measure how much cashflow varies."))

    metrics["monthly_series"] = calculated(
        [{"month": m, "inflow": rupees(b["inflow"]),
          "outflow": rupees(b["outflow"]), "net": rupees(b["net"]),
          "count": b["count"]}
         for m, b in buckets.items()],
        unit="list", confidence=parse_confidence,
        method="credits_and_debits_grouped_by_calendar_month",
        note="Months with no transactions are present with zeros.")

    # ---- runway ----------------------------------------------------------
    closing = balances_raw.get("closing_paise")
    monthly_expense = expenses_raw.get("monthly_expenses_paise")
    if closing is not None and monthly_expense:
        runway = closing / monthly_expense
        raw["runway_months"] = runway
        metrics["expense_runway_months"] = calculated(
            round(runway, 2), unit="months", confidence=parse_confidence,
            method="closing_balance_divided_by_average_monthly_expenses",
            note="How long the closing balance covers the average month's "
                 "outgoings with no further income.")
    else:
        metrics["expense_runway_months"] = unavailable(
            "Needs both a closing balance and a non-zero average monthly "
            "expense figure.")

    # ---- forecast --------------------------------------------------------
    forecast_metrics, forecast_raw = _forecast(txns, closing, ref_date)
    metrics.update(forecast_metrics)
    raw["forecast"] = forecast_raw
    return metrics, raw


def _forecast(txns: Sequence[CanonicalTxn],
              closing_paise: Optional[int],
              ref_date) -> Tuple[Dict[str, Metric], Dict[str, Any]]:
    """Roll the detected cadences forward 30, 60 and 90 days.

    Delegates to `treasury.recurring_detector.compute_30_60_90_forecast`, which
    is the same projection the product's own treasury screen shows — so an
    integrator and the borrower are never told two different numbers.

    Without a closing balance there is no level to project from. Projecting the
    *flows* alone would still be possible, but a balance band is what the field
    means and returning flows under that name would be misleading, so it is
    reported unavailable instead.
    """
    if closing_paise is None:
        return ({"forecast_30_60_90": unavailable(
            "A closing balance is required to project a balance forward; the "
            "statement carries no usable balance column.")}, {})

    days = period_days(txns)
    if days < FORECAST_MIN_HISTORY_DAYS:
        return ({"forecast_30_60_90": unavailable(
            f"Only {days} days of history; at least {FORECAST_MIN_HISTORY_DAYS} "
            "are needed before a projection means anything.")}, {})

    from app.treasury.recurring_detector import compute_30_60_90_forecast
    result = compute_30_60_90_forecast(int(closing_paise), list(txns), days,
                                       ref_date=ref_date)
    if not result.get("available"):
        return ({"forecast_30_60_90": unavailable(
            result.get("status") or "The projection could not be computed.")},
            result)

    horizons = result.get("horizons") or {}
    payload = {
        "as_of": (ref_date.isoformat() if ref_date else None),
        "history_days": result.get("history_days"),
        "drivers": result.get("drivers", []),
        "horizons": {
            key: {
                "expected": band.get("expected"),
                "conservative": band.get("conservative"),
                "net_flow_expected": band.get("net_flow_expected"),
                "net_flow_conservative": band.get("net_flow_conservative"),
                "confidence": _FORECAST_CONFIDENCE.get(int(key.split("_")[1]), 0.5),
            }
            for key, band in horizons.items() if band
        },
    }
    # One confidence for the block, the weakest horizon in it, plus per-horizon
    # confidences inside. A single number over three horizons has to be the
    # worst of them or it overstates the far end.
    block_confidence = min(
        [h["confidence"] for h in payload["horizons"].values()] or [0.5])
    return ({"forecast_30_60_90": infer(
        payload, unit="projection",
        method="recurring_cadence_roll_forward_from_closing_balance",
        confidence=block_confidence,
        note="Expected rolls every detected series forward; conservative counts "
             "only the high-confidence ones. Neither models an unmodelled "
             "one-off, which is what usually moves a real balance.")}, result)
