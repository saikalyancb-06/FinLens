"""Opening, closing, extremes — and the average daily balance.

The average daily balance is the reason this module is more than a `min()` and a
`max()`. Every other figure here is a row-level read; the ADB is a
*time-weighted* average over the statement period, holding the last known
balance across every day that had no transaction. It is the figure Indian banks
compute for AMB penalties, the figure an overdraft limit is sized against, and
the figure a lender asks for when they want to know whether an account is
genuinely funded or just briefly topped up on the day the statement was pulled.
Averaging the balance column instead — which is what a naive implementation does
— weights a day with nine transactions nine times and a quiet fortnight once.

Provenance rules applied here:

* A balance the bank printed is EXTRACTED. Min, max and closing are selections
  from that column, so they stay EXTRACTED.
* Opening is EXTRACTED only when a prior row states it. With a single statement
  and no prior history it is reconstructed by reversing the in-period movement,
  which is arithmetic over extracted values -> CALCULATED, with `basis` naming
  which of the four reconstructions was used.
* The averages are CALCULATED. The carry-forward in the ADB is not a judgement
  about what the data means; it is the definition of a daily balance given a
  complete row set. The `note` says so, because on an *incomplete* row set it
  would be wrong, and the caller can see from `reconciliation_status` whether
  the rows reconcile.

With no balance column at all, every balance-derived figure is `unavailable`
with a reason and `NO_BALANCE_COLUMN` is raised. Returning 0 would be read as
"this account holds nothing", which is a materially different and false claim.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.b2b.canonical import CanonicalTxn
from app.b2b.metrics import (
    Metric,
    W_NO_BALANCE_COLUMN,
    W_NO_OPENING_BALANCE,
    WarningCollector,
    calculated,
    extracted,
    unavailable,
)
from app.b2b.analysis.util import (
    daily_balance_series,
    period_bounds,
    rupees,
    sorted_rows,
)

logger = logging.getLogger(__name__)

_NO_BALANCE_REASON = (
    "The statement carries no running-balance column, so no balance figure can "
    "be read or reconstructed from it."
)


def compute_balances(txns: Sequence[CanonicalTxn],
                     warnings: WarningCollector,
                     parse_confidence: float = 1.0
                     ) -> Tuple[Dict[str, Metric], Dict[str, Any]]:
    """Return (metrics, raw) where raw carries paise-precise internals."""
    rows = sorted_rows(txns)
    with_balance = [t for t in rows if t.balance_paise is not None]
    raw: Dict[str, Any] = {
        "balance_rows": len(with_balance),
        "total_rows": len(rows),
        "daily_series": [],
        "opening_paise": None,
        "closing_paise": None,
        "reported_closing_paise": None,
        "basis": None,
        "min_paise": None,
        "max_paise": None,
        "average_paise": None,
        "average_daily_paise": None,
    }

    count_metric = calculated(
        len(with_balance), unit="count",
        method="rows_carrying_a_running_balance",
        confidence=parse_confidence,
        note=f"{len(with_balance)} of {len(rows)} rows state a balance.",
    )

    if not rows:
        return {"balance_rows_with_value": count_metric}, raw

    if not with_balance:
        warnings.add(
            W_NO_BALANCE_COLUMN,
            "No running-balance column was found. Opening, closing, minimum, "
            "maximum and average daily balance cannot be determined, and the "
            "statement's internal consistency cannot be verified.",
            severity="critical",
            detail={"rows": len(rows)},
        )
        movement = sum(t.signed_paise for t in rows)
        raw["net_movement_paise"] = movement
        metrics = {
            key: unavailable(_NO_BALANCE_REASON)
            for key in ("opening_balance", "closing_balance", "min_balance",
                        "max_balance", "average_balance", "average_daily_balance")
        }
        # Net movement IS computable without balances and is not a balance, so
        # it is reported rather than suppressed. It is the change in the
        # position, not the position.
        metrics["net_movement"] = calculated(
            rupees(movement), unit="INR", confidence=parse_confidence,
            method="sum_of_credits_minus_debits",
            note="Change in the position over the period. Not a balance: the "
                 "account's actual level is unknown without a balance column.",
        )
        metrics["balance_rows_with_value"] = count_metric
        return metrics, raw

    opening_paise, reported_closing_paise, basis = _opening_and_closing(rows)
    movement = sum(t.signed_paise for t in rows)

    # Closing keeps the statement identity (opening + in - out) and the bank's
    # own last stated figure rides alongside. Where the two differ, the rows do
    # not fully explain the change in cash, and that gap is a finding to show
    # rather than a number to quietly overwrite.
    identity_closing = opening_paise + movement
    closing_paise = reported_closing_paise if reported_closing_paise is not None else identity_closing

    balances = [int(t.balance_paise) for t in with_balance]
    min_paise, max_paise = min(balances), max(balances)
    average_paise = int(round(sum(balances) / len(balances)))

    series = daily_balance_series(rows, opening_paise)
    adb_paise = (int(round(sum(b for _, b in series) / len(series)))
                 if series else None)

    start, end = period_bounds(rows)
    raw.update({
        "opening_paise": opening_paise,
        "closing_paise": closing_paise,
        "reported_closing_paise": reported_closing_paise,
        "identity_closing_paise": identity_closing,
        "basis": basis,
        "min_paise": min_paise,
        "max_paise": max_paise,
        "average_paise": average_paise,
        "average_daily_paise": adb_paise,
        "net_movement_paise": movement,
        "daily_series": series,
        "days_in_period": len(series),
    })

    if basis in ("reconstructed", "derived"):
        warnings.add(
            W_NO_OPENING_BALANCE,
            "No balance was stated before the period, so the opening balance "
            f"was {basis} from the rows in this statement.",
            severity="info",
            detail={"basis": basis},
        )

    metrics: Dict[str, Metric] = {}

    # `reported` is the only basis on which the opening is something the bank
    # actually said. Everything else is arithmetic we did.
    metrics["opening_balance"] = (
        extracted(rupees(opening_paise), unit="INR", basis=basis,
                  confidence=parse_confidence,
                  note="Balance stated before the period began.")
        if basis == "reported" else
        calculated(rupees(opening_paise), unit="INR", basis=basis,
                   confidence=parse_confidence,
                   method="reverse_in_period_movement_from_first_stated_balance",
                   note="No balance was stated before the period; this is the "
                        "opening implied by the statement's own rows.")
    )

    metrics["closing_balance"] = (
        extracted(rupees(closing_paise), unit="INR", basis="reported",
                  confidence=parse_confidence,
                  note="Last running balance printed on the statement.")
        if reported_closing_paise is not None else
        calculated(rupees(closing_paise), unit="INR", basis="derived",
                   confidence=parse_confidence,
                   method="opening_plus_credits_minus_debits")
    )

    if reported_closing_paise is not None and reported_closing_paise != identity_closing:
        metrics["closing_balance_identity_gap"] = calculated(
            rupees(reported_closing_paise - identity_closing), unit="INR",
            method="reported_closing_minus_opening_plus_movement",
            confidence=parse_confidence,
            note="The bank's closing figure and the sum of the rows disagree by "
                 "this much. Rows are probably missing from the extract.",
        )

    metrics["min_balance"] = extracted(
        rupees(min_paise), unit="INR", basis="reported", confidence=parse_confidence,
        note="Lowest running balance printed on the statement.")
    metrics["max_balance"] = extracted(
        rupees(max_paise), unit="INR", basis="reported", confidence=parse_confidence,
        note="Highest running balance printed on the statement.")

    metrics["average_balance"] = calculated(
        rupees(average_paise), unit="INR", confidence=parse_confidence,
        method="arithmetic_mean_of_stated_row_balances",
        note="Mean of the balance column, one weight per row. Weighted by "
             "transaction count, not by time — use average_daily_balance for "
             "the time-weighted figure.")

    if adb_paise is None:
        metrics["average_daily_balance"] = unavailable(
            "The statement period could not be resolved, so days could not be "
            "weighted.")
    else:
        metrics["average_daily_balance"] = calculated(
            rupees(adb_paise), unit="INR", confidence=parse_confidence,
            method="time_weighted_daily_balance_carry_forward",
            basis=basis,
            note=(
                f"Mean of the end-of-day balance on each of the {len(series)} "
                f"days from {start} to {end}, carrying the last stated balance "
                "across days with no transaction. Assumes the row set is "
                "complete for the period; check reconciliation_status."
            ),
        )

    metrics["net_movement"] = calculated(
        rupees(movement), unit="INR", confidence=parse_confidence,
        method="sum_of_credits_minus_debits")
    metrics["balance_rows_with_value"] = count_metric
    metrics["days_in_period"] = calculated(
        len(series), unit="days", confidence=parse_confidence,
        method="inclusive_calendar_days_between_first_and_last_row")

    return metrics, raw


def _opening_and_closing(rows: List[CanonicalTxn]
                         ) -> Tuple[int, Optional[int], str]:
    """Delegate to the production balance resolver, or fall back in place.

    `app.api.reports._account_balances` is pure — it takes two lists and an
    account id and touches no session — and it encodes four ranked ways to
    recover an opening balance plus the reasoning for their order. Reusing it
    means the API and the product's own treasury reports cannot disagree about
    what "opening balance" means.

    It is imported lazily because `app.api.reports` pulls in the ORM layer at
    module scope, and this package must stay importable without a database. If
    that import fails, the identical fallback below keeps the analysis running
    with the one case a single statement can actually hit.
    """
    account_ids = {t.account_id for t in rows}
    acc_id = account_ids.pop() if len(account_ids) == 1 else None

    try:
        from app.api.reports import _account_balances
        return _account_balances(rows, [], acc_id)
    except Exception as exc:  # pragma: no cover - only if the ORM import breaks
        logger.warning("[b2b] falling back to local opening-balance recovery: %s", exc)

    with_balance = [t for t in rows if t.balance_paise is not None]
    if not with_balance:
        return 0, None, "derived"
    first = with_balance[0]
    upto = rows[:rows.index(first) + 1]
    opening = int(first.balance_paise) - sum(t.signed_paise for t in upto)
    return opening, int(with_balance[-1].balance_paise), "reconstructed"


def continuity_check(txns: Sequence[CanonicalTxn],
                     tolerance_paise: int = 100) -> Dict[str, Any]:
    """Do consecutive stated balances differ by exactly the transaction amount?

    This is the statement's own internal proof, and it is the only evidence the
    API has that the parse did not drop or duplicate a row. Checked here rather
    than through `anomaly_engine._detect_incorrect_balances` because that
    detector groups rows by statement or account id, and a canonical row set
    from a single uploaded file carries neither — it would silently check
    nothing.

    Tolerance is one rupee (100 paise), matching the detector's own threshold.
    Sub-rupee drift is a rounding artefact of a statement that prints two
    decimals; anything larger is a real break.
    """
    rows = [t for t in sorted_rows(txns) if t.balance_paise is not None]
    if len(rows) < 2:
        return {"verifiable": False, "pairs": 0, "matched": 0, "breaks": []}

    matched, breaks = 0, []
    for prev, cur in zip(rows, rows[1:]):
        delta = int(cur.balance_paise) - int(prev.balance_paise)
        drift = abs(delta - cur.signed_paise)
        if drift <= tolerance_paise:
            matched += 1
        else:
            breaks.append({
                "date": cur.txn_date.isoformat() if cur.txn_date else None,
                "row_index": cur.row_index,
                "expected_change": rupees(cur.signed_paise),
                "stated_change": rupees(delta),
                "drift": rupees(drift),
            })
            cur.balance_anomaly = True

    pairs = len(rows) - 1
    return {"verifiable": True, "pairs": pairs, "matched": matched,
            "breaks": breaks, "score": matched / pairs if pairs else 0.0}
