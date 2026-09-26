"""Existing debt service, read out of the outflows.

An EMI is never stated on a bank statement. What is stated is a debit of the
same amount, on roughly the same day, every month, usually with the letters EMI
or the name of a lender in it. So every figure in this module is INFERRED, and
each detected obligation carries its own confidence and the evidence that
produced it — because the difference between a ₹12,500 EMI and a ₹12,500
standing transfer to a sibling changes an affordability decision and the
statement does not distinguish them.

Two admission routes, deliberately asymmetric:

**Named.** The narration says EMI, LOAN, or a lending product, or the category
tree placed the series under Loans & Credit. This is direct evidence and scores
high.

**Structural only.** A monthly series with a near-fixed amount on a near-fixed
day, which is what an EMI looks like from the outside. On its own this also
describes rent, a SIP, a school fee and an insurance premium — so the structural
route is admitted *only* for series the category tree did not already place
somewhere that is definitely not debt, and it scores low enough that it can
never masquerade as a named EMI. Without that guard, every fixed monthly rent
payment in the country becomes a loan obligation and every DTI ratio is wrong.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple

from app.b2b.canonical import CanonicalTxn
from app.b2b.metrics import Metric, WarningCollector, calculated, unavailable
from app.b2b.analysis.util import (
    coefficient_of_variation,
    day_of_month_spread,
    infer,
    narration_of,
    rupees,
    series_groups,
    series_members,
)

# Words a lender's own debit carries. Deliberately narrow: "PAYMENT" and
# "TRANSFER" appear on everything and would admit the whole statement.
_DEBT_NARRATION = re.compile(
    r"\b("
    r"EMI|E\.M\.I|LOAN|LN\s*A/?C|INSTAL?LMENT|INSTALMENT|"
    r"HOME\s*LN|CAR\s*LN|AUTO\s*LN|PERSONAL\s*LN|"
    r"HOUSING\s*LOAN|VEHICLE\s*LOAN|EDU\s*LOAN|GOLD\s*LOAN|"
    r"HDFCL|BAJAJFIN|BAJAJ\s*FIN|TATA\s*CAPITAL|CHOLA|SHRIRAM|MUTHOOT|"
    r"CREDIT\s*CARD\s*PAYMENT|CC\s*PAYMENT|CARD\s*PAYMENT"
    r")\b",
    re.IGNORECASE,
)

# The category-tree roots that ARE debt service.
_DEBT_ROOTS = {"Loans & Credit"}

# Roots that are definitely not debt service. A monthly fixed outflow the tree
# has already placed in one of these is not admitted on structure alone.
_NOT_DEBT_ROOTS = {
    "Housing", "Food & Dining", "Bills & Utilities", "Investments",
    "Insurance", "Transfers", "Cash", "Shopping", "Entertainment", "Travel",
    "Transportation", "Donations & Charity", "Personal & Family", "Income",
    "Refunds & Reversals", "Healthcare", "Education", "Taxes & Government",
}

# Structural admission thresholds.
#
# 0.02 on the amount: a real EMI is a contractual constant. Two percent is wide
# enough for a floating-rate reset mid-statement and narrow enough to exclude a
# utility bill, which moves by far more than that month to month.
EMI_AMOUNT_CV_MAX = 0.02
# 3 days on the posting date: an auto-debit mandate presents on a fixed date and
# drifts only over weekends and bank holidays.
EMI_DAY_STDEV_MAX = 3.0
# Three occurrences is the detector's own minimum for calling anything a series.
EMI_MIN_OCCURRENCES = 3

# Confidence by admission route. Neither reaches 1.0 and the structural route
# stays below the acceptance bar used elsewhere for a reason: it is a shape, not
# a statement.
_CONF_NAMED_AND_STRUCTURAL = 0.90
_CONF_NAMED = 0.80
_CONF_TREE_ONLY = 0.75
_CONF_STRUCTURAL_ONLY = 0.55


def compute_debt(txns: Sequence[CanonicalTxn],
                 recurring: Sequence[Dict[str, Any]],
                 warnings: WarningCollector,
                 parse_confidence: float = 1.0
                 ) -> Tuple[Dict[str, Metric], Dict[str, Any]]:
    groups = series_groups(txns)
    emis = _detect_emis(recurring, groups)

    total_monthly = sum(e["monthly_paise"] for e in emis)
    raw: Dict[str, Any] = {
        "emis": emis,
        "total_monthly_emi_paise": total_monthly,
        "emi_count": len(emis),
    }

    metrics: Dict[str, Metric] = {}

    if not emis:
        metrics["detected_emis"] = unavailable(
            "No outflow series carried loan or EMI evidence, and none was a "
            "fixed-amount monthly debit the category tree left unplaced.")
        metrics["total_monthly_emi"] = calculated(
            0.0, unit="INR", confidence=parse_confidence,
            method="sum_of_detected_emi_obligations",
            note="Zero detected obligations. This is a floor, not a fact: an "
                 "EMI serviced from another account is invisible here.")
        metrics["emi_count"] = calculated(
            0, unit="count", confidence=parse_confidence,
            method="count_of_detected_emi_obligations")
        metrics["credit_obligations"] = unavailable(
            "No loan or credit obligation was identified in this statement.")
        return metrics, raw

    # The aggregate is only as good as its weakest admitted member, weighted by
    # money: one large well-evidenced EMI should not be dragged down by a small
    # structural guess, and vice versa.
    weighted_conf = (sum(e["confidence"] * e["monthly_paise"] for e in emis)
                     / total_monthly) if total_monthly else min(
                         e["confidence"] for e in emis)

    metrics["detected_emis"] = infer(
        [_emi_api(e) for e in emis], unit="list",
        method="recurring_monthly_outflow_with_lender_evidence_or_fixed_amount_structure",
        confidence=weighted_conf,
        note=f"{len(emis)} obligation(s). Each carries its own confidence and "
             "the evidence that admitted it.")

    metrics["total_monthly_emi"] = infer(
        rupees(total_monthly), unit="INR",
        method="sum_of_detected_emi_obligations_normalised_to_a_month",
        confidence=weighted_conf,
        note="A floor. Obligations serviced from an account other than this one "
             "cannot appear.",
        evidence={"obligations": [e["name"] for e in emis]})

    metrics["emi_count"] = infer(
        len(emis), unit="count",
        method="count_of_detected_emi_obligations",
        confidence=weighted_conf)

    obligations = _credit_obligations(txns)
    raw["credit_obligations"] = obligations
    metrics["credit_obligations"] = (
        infer([_obligation_api(o) for o in obligations], unit="list",
              method="category_tree_loans_and_credit_rows_grouped_by_counterparty",
              confidence=0.70,
              note="Every row the tree placed under Loans & Credit, grouped by "
                   "lender key — including one-off fees and disbursements that "
                   "are not part of a monthly series.")
        if obligations else
        unavailable("No row was placed under Loans & Credit."))

    return metrics, raw


def _detect_emis(recurring: Sequence[Dict[str, Any]],
                 groups: Dict[Tuple[str, str], List[CanonicalTxn]]
                 ) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for series in recurring:
        if series.get("direction") != "outflow":
            continue
        if series.get("cadence") != "monthly":
            continue
        if (series.get("occurrences") or 0) < EMI_MIN_OCCURRENCES:
            continue

        rows = series_members(groups, series)
        if not rows:
            continue

        amounts = [int(r.debit_paise or 0) for r in rows]
        dates = [r.txn_date for r in rows if r.txn_date]
        amount_cv = coefficient_of_variation(amounts)
        day_spread = day_of_month_spread(dates)
        roots = {(r.category_path or "").split(">")[0].strip() for r in rows}
        roots.discard("")

        named = any(_DEBT_NARRATION.search(narration_of(r)) for r in rows)
        tree_says_debt = bool(roots & _DEBT_ROOTS)
        tree_says_not_debt = bool(roots) and roots.issubset(_NOT_DEBT_ROOTS)

        structural = (
            amount_cv is not None and amount_cv <= EMI_AMOUNT_CV_MAX
            and day_spread is not None and day_spread <= EMI_DAY_STDEV_MAX
        )

        evidence: List[str] = []
        if named:
            evidence.append("lender_or_emi_narration")
        if tree_says_debt:
            evidence.append("category_tree_loans_and_credit")
        if structural:
            evidence.append("fixed_amount_fixed_day_monthly")

        if named or tree_says_debt:
            confidence = (_CONF_NAMED_AND_STRUCTURAL if (named and structural)
                          else _CONF_NAMED if named else _CONF_TREE_ONLY)
        elif structural and not tree_says_not_debt:
            confidence = _CONF_STRUCTURAL_ONLY
        else:
            continue

        distinct_months = len({(d.year, d.month) for d in dates}) or 1
        out.append({
            "name": series.get("name"),
            "amount_paise": int(series["amount_paise"]),
            "monthly_paise": int(round(sum(amounts) / distinct_months)),
            "confidence": confidence,
            "occurrences": series.get("occurrences"),
            "cadence": series.get("cadence"),
            "typical_day": series.get("typical_day"),
            "amount_cv": round(amount_cv, 4) if amount_cv is not None else None,
            "day_stdev": round(day_spread, 2) if day_spread is not None else None,
            "category_roots": sorted(roots),
            "evidence": evidence,
            "route": "named" if (named or tree_says_debt) else "structural",
            "dates": [d.isoformat() for d in dates],
        })

    out.sort(key=lambda e: -e["monthly_paise"])
    return out


def _credit_obligations(txns: Sequence[CanonicalTxn]) -> List[Dict[str, Any]]:
    """Every Loans & Credit row, grouped by lender key."""
    from app.b2b.analysis.util import clean_key

    grouped: Dict[str, Dict[str, Any]] = {}
    for t in txns:
        root = (t.category_path or "").split(">")[0].strip()
        if root not in _DEBT_ROOTS:
            continue
        key = clean_key(t)
        entry = grouped.setdefault(key, {
            "name": key, "count": 0, "debit_paise": 0, "credit_paise": 0,
            "paths": set(), "first": None, "last": None,
        })
        entry["count"] += 1
        entry["debit_paise"] += int(t.debit_paise or 0)
        entry["credit_paise"] += int(t.credit_paise or 0)
        if t.category_path:
            entry["paths"].add(t.category_path)
        if t.txn_date:
            entry["first"] = min(entry["first"] or t.txn_date, t.txn_date)
            entry["last"] = max(entry["last"] or t.txn_date, t.txn_date)
    return sorted(grouped.values(), key=lambda e: -e["debit_paise"])


def _emi_api(e: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": e["name"],
        "amount": rupees(e["amount_paise"]),
        "monthly_amount": rupees(e["monthly_paise"]),
        "occurrences": e["occurrences"],
        "typical_day_of_month": e["typical_day"],
        "confidence": round(e["confidence"], 3),
        "admitted_by": e["route"],
        "evidence": e["evidence"],
        "amount_cv": e["amount_cv"],
        "day_of_month_stdev": e["day_stdev"],
        "category_roots": e["category_roots"],
        "dates": e["dates"],
    }


def _obligation_api(o: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": o["name"],
        "transactions": o["count"],
        "paid_out": rupees(o["debit_paise"]),
        "received": rupees(o["credit_paise"]),
        "category_paths": sorted(o["paths"]),
        "first_seen": o["first"].isoformat() if o["first"] else None,
        "last_seen": o["last"].isoformat() if o["last"] else None,
    }
