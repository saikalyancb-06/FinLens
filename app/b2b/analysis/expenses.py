"""What goes out, and how much of it the borrower could stop paying.

The essential/discretionary split is the only judgement in this module that a
reasonable person could disagree with, so it is a data table rather than a chain
of `if` statements. `ESSENTIALITY_BY_ROOT` maps each of the 22 roots of the
category tree, and `ESSENTIALITY_OVERRIDES` refines the roots that are genuinely
mixed at level two. A caller who underwrites differently passes
`config={"essentiality_overrides": {...}}` and gets their own policy applied to
the same rows — without editing this file and without the mapping becoming
invisible, because the resolved table is returned in the response.

Three buckets, not two:

    essential       housing, utilities, healthcare, debt service, insurance,
                    tax, education, commuting, groceries. Money that keeps
                    going out if the borrower loses their job.
    discretionary   dining out, entertainment, shopping, travel, donations.
                    Compressible under stress.
    unattributed    transfers, investments, cash withdrawals, reversals. Money
                    that left the account without being consumption. Counted in
                    total debits — it did leave — but never counted as either
                    essential or discretionary, because calling an ATM
                    withdrawal "discretionary" invents a fact about cash that
                    the statement cannot support.

Keeping the third bucket visible is the honest move. Folding transfers into
discretionary would flatter every disposable-income figure downstream; folding
them into essential would do the reverse.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from app.b2b.canonical import CanonicalTxn
from app.b2b.metrics import (
    Metric,
    WarningCollector,
    calculated,
    unavailable,
)
from app.b2b.analysis.util import (
    coefficient_of_variation,
    infer,
    monthly_buckets,
    months_covered,
    narration_of,
    rupees,
    safe_div,
    stability_from_cv,
)

ESSENTIAL = "essential"
DISCRETIONARY = "discretionary"
UNATTRIBUTED = "unattributed"

# One entry per root of `app.categorization.hierarchy.TREE`. Every root is
# listed even where the answer looks obvious, so that a new root added to the
# tree shows up here as a missing key rather than being silently defaulted.
ESSENTIALITY_BY_ROOT: Dict[str, str] = {
    "Income": UNATTRIBUTED,               # a credit; never an expense
    "Food & Dining": DISCRETIONARY,       # groceries carved out below
    "Shopping": DISCRETIONARY,
    "Housing": ESSENTIAL,                 # rent and maintenance stop for nobody
    "Transportation": ESSENTIAL,          # commuting; air travel carved out below
    "Bills & Utilities": ESSENTIAL,
    "Healthcare": ESSENTIAL,
    "Education": ESSENTIAL,               # school fees are contractual in India
    "Entertainment": DISCRETIONARY,
    "Travel": DISCRETIONARY,
    "Financial": ESSENTIAL,               # bank charges are not optional
    "Transfers": UNATTRIBUTED,            # movement, not consumption
    "Investments": UNATTRIBUTED,          # saving, not spending
    "Loans & Credit": ESSENTIAL,          # debt service is the first claim
    "Insurance": ESSENTIAL,
    "Taxes & Government": ESSENTIAL,
    "Business & Professional": ESSENTIAL, # cost of earning the income
    "Personal & Family": DISCRETIONARY,
    "Cash": UNATTRIBUTED,                 # what the cash bought is unknowable
    "Donations & Charity": DISCRETIONARY,
    "Fees & Charges": ESSENTIAL,          # penalties are owed, not chosen
    "Refunds & Reversals": UNATTRIBUTED,  # money coming back
}

# Level-two refinements for roots that are genuinely mixed. Keyed on the first
# two levels of the path, longest match wins over the root.
ESSENTIALITY_OVERRIDES: Dict[Tuple[str, ...], str] = {
    ("Food & Dining", "Groceries"): ESSENTIAL,
    ("Food & Dining", "Restaurants"): DISCRETIONARY,
    ("Food & Dining", "Food Delivery"): DISCRETIONARY,
    ("Food & Dining", "Cafes & Beverages"): DISCRETIONARY,
    ("Transportation", "Air Travel"): DISCRETIONARY,
    ("Transportation", "Ride Hailing"): DISCRETIONARY,
    ("Shopping", "Personal Care"): ESSENTIAL,
    ("Personal & Family", "Family Transfer"): UNATTRIBUTED,
    ("Personal & Family", "Allowance"): UNATTRIBUTED,
    ("Education", "Student Loan"): ESSENTIAL,
    ("Entertainment", "OTT / Streaming"): DISCRETIONARY,
}

# A credit-card bill is debt service in cash-flow terms, but the spend it
# settles was already categorised somewhere if the card statement was uploaded
# too — and was not, if it was not. Counted separately for exactly that reason.
_CREDIT_CARD_MARKER = "Credit Card"

_TRANSFER_FLOWS = {"TRANSFER"}

# Below this the cash inference is a hint, not a finding. Same threshold the
# policy engine uses (`policy_engine.CASH_MIN_CONFIDENCE`), so a withdrawal the
# compliance layer counts and one this layer counts are the same withdrawals.
CASH_MIN_CONFIDENCE = 0.6


def resolve_essentiality(overrides: Optional[Dict[str, str]] = None
                         ) -> Tuple[Dict[str, str], Dict[Tuple[str, ...], str]]:
    """Merge caller policy over the defaults. Returns (by_root, by_path)."""
    by_root = dict(ESSENTIALITY_BY_ROOT)
    by_path = dict(ESSENTIALITY_OVERRIDES)
    for key, value in (overrides or {}).items():
        parts = tuple(p.strip() for p in str(key).split(">") if p.strip())
        if len(parts) == 1:
            by_root[parts[0]] = value
        elif parts:
            by_path[parts] = value
    return by_root, by_path


def classify_essentiality(t: CanonicalTxn,
                          by_root: Dict[str, str],
                          by_path: Dict[Tuple[str, ...], str]) -> str:
    """Which of the three buckets one debit falls in."""
    path = tuple(p.strip() for p in (t.category_path or "").split(">") if p.strip())
    # A self-transfer is movement whatever the tree called it.
    if (t.flow_type or "") in _TRANSFER_FLOWS:
        return UNATTRIBUTED
    for depth in range(min(len(path), 3), 1, -1):
        hit = by_path.get(path[:depth])
        if hit:
            return hit
    if path:
        hit = by_root.get(path[0])
        if hit:
            return hit
    # An unplaced row is unattributed, never discretionary. Guessing here is how
    # a disposable-income figure gets quietly inflated.
    return UNATTRIBUTED


def compute_expenses(txns: Sequence[CanonicalTxn],
                     recurring: Sequence[Dict[str, Any]],
                     warnings: WarningCollector,
                     parse_confidence: float = 1.0,
                     config: Optional[Dict[str, Any]] = None
                     ) -> Tuple[Dict[str, Metric], Dict[str, Any]]:
    from app.compliance.cash_inference import classify_cash

    config = config or {}
    by_root, by_path = resolve_essentiality(config.get("essentiality_overrides"))

    debits = [t for t in txns if (t.debit_paise or 0) > 0]
    total_debits = sum(int(t.debit_paise or 0) for t in debits)
    months = months_covered(txns)
    buckets = monthly_buckets(txns)
    monthly_outflows = [b["outflow"] for b in buckets.values()]

    split = {ESSENTIAL: 0, DISCRETIONARY: 0, UNATTRIBUTED: 0}
    by_category: Dict[str, int] = {}
    cash_paise, cash_rows = 0, []
    transfer_paise, transfer_count = 0, 0
    card_paise, card_count = 0, 0

    for t in debits:
        amount = int(t.debit_paise or 0)
        bucket = classify_essentiality(t, by_root, by_path)
        split[bucket] += amount
        root = (t.category_path or "").split(">")[0].strip() or "Unclassified"
        by_category[root] = by_category.get(root, 0) + amount

        is_cash, kind, cash_conf = classify_cash(narration_of(t))
        # An ATM rail is corroboration on its own: the narration test and the
        # rail test are independent readings of the same string, and either
        # alone is enough to call the row a cash withdrawal.
        if ((is_cash and kind == "cash_withdrawal" and cash_conf >= CASH_MIN_CONFIDENCE)
                or t.transaction_method == "ATM"):
            cash_paise += amount
            cash_rows.append({
                "date": t.txn_date.isoformat() if t.txn_date else None,
                "amount": rupees(amount),
                "method": t.transaction_method,
                "cash_confidence": round(cash_conf, 2) if is_cash else None,
                "narration": narration_of(t)[:160],
            })

        if (t.flow_type or "") in _TRANSFER_FLOWS:
            transfer_paise += amount
            transfer_count += 1

        if _CREDIT_CARD_MARKER in (t.category_path or ""):
            card_paise += amount
            card_count += 1

    raw: Dict[str, Any] = {
        "total_debits_paise": total_debits,
        "months": months,
        "split_paise": dict(split),
        "by_category_paise": dict(by_category),
        "cash_withdrawals_paise": cash_paise,
        "cash_withdrawal_count": len(cash_rows),
        "transfers_paise": transfer_paise,
        "credit_card_paise": card_paise,
        "monthly_outflow_paise": dict((m, b["outflow"]) for m, b in buckets.items()),
    }

    metrics: Dict[str, Metric] = {}
    metrics["total_debits"] = calculated(
        rupees(total_debits), unit="INR", confidence=parse_confidence,
        method="sum_of_debit_column", note=f"{len(debits)} debit rows.")

    monthly_expense_paise = int(round(total_debits / months)) if months else None
    raw["monthly_expenses_paise"] = monthly_expense_paise
    metrics["monthly_expenses"] = (
        calculated(rupees(monthly_expense_paise), unit="INR",
                   confidence=parse_confidence,
                   method="total_debits_divided_by_calendar_months_covered")
        if monthly_expense_paise is not None
        else unavailable("The statement period could not be resolved."))

    cv = coefficient_of_variation(monthly_outflows)
    raw["expense_cv"] = cv
    metrics["expense_stability"] = (
        calculated(round(stability_from_cv(cv), 4), unit="ratio",
                   confidence=parse_confidence,
                   method="one_over_one_plus_coefficient_of_variation_of_monthly_debits")
        if cv is not None else
        unavailable("At least two calendar months of debits are needed."))

    # The split rests on the category tree, which is inference; the arithmetic
    # on top of it is not. Confidence is the share of debit value the tree
    # actually placed — if half the money is unattributed, the split is half a
    # picture and says so.
    attributed = split[ESSENTIAL] + split[DISCRETIONARY]
    attribution_share = safe_div(attributed, total_debits) or 0.0
    split_confidence = 0.45 + 0.5 * attribution_share

    for bucket, key in ((ESSENTIAL, "essential_expenses"),
                        (DISCRETIONARY, "discretionary_expenses")):
        metrics[key] = infer(
            rupees(split[bucket]), unit="INR",
            method="category_tree_root_mapped_to_essentiality_table",
            confidence=split_confidence,
            evidence={"mapping": "ESSENTIALITY_BY_ROOT + ESSENTIALITY_OVERRIDES",
                      "attributed_share_of_debits": round(attribution_share, 3)})

    metrics["unattributed_expenses"] = calculated(
        rupees(split[UNATTRIBUTED]), unit="INR", confidence=parse_confidence,
        method="debits_the_essentiality_table_deliberately_does_not_classify",
        note="Transfers, investments, cash withdrawals and unplaced rows. Real "
             "money out, but not consumption that can be called essential or "
             "discretionary.")

    metrics["essential_share"] = (
        infer(round(safe_div(split[ESSENTIAL], attributed), 4), unit="ratio",
              method="essential_over_essential_plus_discretionary",
              confidence=split_confidence,
              note="Share of *attributed* consumption, not of all debits.")
        if attributed else
        unavailable("No debit was attributed to either bucket, so there is no "
                    "share to report."))

    metrics["expenses_by_category"] = calculated(
        [{"category": k, "amount": rupees(v),
          "essentiality": by_root.get(k, UNATTRIBUTED)}
         for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])],
        unit="list", confidence=parse_confidence,
        method="sum_of_debits_grouped_by_category_tree_root")

    metrics["essentiality_mapping"] = calculated(
        {"by_root": by_root,
         "by_path": {" > ".join(k): v for k, v in by_path.items()}},
        unit="mapping", method="resolved_essentiality_policy",
        note="The exact table used for this response, defaults merged with any "
             "caller overrides.")

    # ---- recurring outflows ---------------------------------------------
    outflow_series = [s for s in recurring if s.get("direction") == "outflow"]
    raw["recurring_expenses"] = outflow_series
    if outflow_series:
        monthly_equiv = _monthly_equivalent_paise(outflow_series)
        raw["recurring_expenses_monthly_paise"] = monthly_equiv
        conf = _tier_confidence(outflow_series)
        metrics["recurring_expenses"] = infer(
            [_series_api(s) for s in outflow_series], unit="list",
            method="repeat_interval_clustering_over_narration_groups",
            confidence=conf,
            note=f"{len(outflow_series)} debit series on a recognised cadence.")
        metrics["recurring_expenses_monthly"] = infer(
            rupees(monthly_equiv), unit="INR",
            method="sum_of_series_amounts_normalised_to_a_30_day_month",
            confidence=conf)
    else:
        raw["recurring_expenses_monthly_paise"] = 0
        metrics["recurring_expenses"] = unavailable(
            "No debit repeated at least three times on a recognised cadence.")
        metrics["recurring_expenses_monthly"] = unavailable(
            "No recurring debit series was identified.")

    # ---- cash, transfers, cards -----------------------------------------
    # Cash is inferred: a bank statement has no cash flag, only wording.
    metrics["cash_withdrawals"] = infer(
        rupees(cash_paise), unit="INR",
        method="narration_cash_inference_or_atm_rail",
        confidence=0.80 if cash_rows else 0.60,
        note="Withdrawals identified from narration wording and the ATM rail. "
             "Cash paid over a counter and labelled only with a branch code "
             "cannot be seen at all.",
        evidence={"count": len(cash_rows), "rows": cash_rows[:25]})
    metrics["cash_withdrawal_count"] = infer(
        len(cash_rows), unit="count",
        method="narration_cash_inference_or_atm_rail",
        confidence=0.80 if cash_rows else 0.60)

    metrics["transfers_out"] = infer(
        rupees(transfer_paise), unit="INR",
        method="flow_type_TRANSFER_from_narration_and_category_path",
        confidence=0.75,
        note="Debits the flow detector read as movement between accounts rather "
             "than spending.",
        evidence={"count": transfer_count})

    metrics["credit_card_payments"] = infer(
        rupees(card_paise), unit="INR",
        method="category_path_contains_credit_card",
        confidence=0.80,
        note="Bill settlements, not card spend. What the card bought is only "
             "visible if the card statement was analysed too.",
        evidence={"count": card_count})

    return metrics, raw


def _monthly_equivalent_paise(series: Sequence[Dict[str, Any]]) -> int:
    total = 0.0
    for s in series:
        nominal = max(1, int(s.get("nominal_days") or 30))
        total += float(s["amount_paise"]) * (30.0 / nominal)
    return int(round(total))


def _tier_confidence(series: Sequence[Dict[str, Any]]) -> float:
    from app.b2b.analysis.income import _TIER_CONFIDENCE
    weighted, total = 0.0, 0
    for s in series:
        amount = abs(int(s.get("amount_paise") or 0)) or 1
        weighted += _TIER_CONFIDENCE.get(s.get("confidence"), 0.6) * amount
        total += amount
    return weighted / total if total else 0.6


def _series_api(s: Dict[str, Any]) -> Dict[str, Any]:
    from app.b2b.analysis.income import _series_api as render
    return render(s)
