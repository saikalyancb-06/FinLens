"""What comes in, how reliably, and whether any of it is a salary.

Total credits is a sum and says so. Everything interesting here is an inference,
and the module is organised around being explicit about which is which:

CALCULATED
    total_credits, monthly_income, income_volatility, income_stability — all
    arithmetic over amounts read off the statement.

INFERRED
    salary, recurring_income, income_sources — every one of these is a claim
    about what a credit *means*, built from a repeating pattern and a narration.
    A statement never says "salary"; it says "SALARY CREDIT ACME" every 30 days
    for the same amount, and we conclude.

Salary detection is scored rather than switched, because the evidence arrives in
degrees. A monthly recurring credit is the necessary condition; the narration,
the number of repetitions, the amount stability and the day-of-month stability
are four independent corroborations, and each is worth a stated number of
confidence points. The weights are in `_SALARY_EVIDENCE` so they can be argued
with — which is the point.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.b2b.canonical import CanonicalTxn
from app.b2b.metrics import (
    Metric,
    W_NO_SALARY_DETECTED,
    WarningCollector,
    calculated,
    unavailable,
)
from app.b2b.analysis.util import (
    MAX_INFERRED_CONFIDENCE,
    clean_key,
    coefficient_of_variation,
    day_of_month_spread,
    infer,
    monthly_buckets,
    months_covered,
    narration_of,
    rupees,
    safe_div,
    series_groups,
    series_members,
    stability_from_cv,
)

# Narration evidence for payroll. Mirrors the credit-direction SALARY rule in
# `app/categorization/merchants.py` (Income > Salary, 0.92) so that a row the
# category tree already calls salary and a series this module calls salary are
# recognising the same words. Kept as a local pattern rather than imported
# because the merchant module's rule is a per-row category rule, and this is a
# per-*series* test — the same vocabulary used for a different question.
_SALARY_NARRATION = (
    "SALARY", "SAL CREDIT", "SALCR", "SAL CR", "PAYROLL", "WAGES", "STIPEND",
    "REMUNERATION",
)

# The category-tree path a salaried credit lands on. A series whose rows the
# tree already placed under Income > Salary has independent corroboration from a
# completely different code path, and is worth as much as the narration test.
_SALARY_PATH_PREFIX = ("Income", "Salary")

# Evidence weights for the salary inference. They sum to 1.00 with the base, and
# the base is what a bare monthly recurring credit is worth on its own.
#
#   base 0.50      a monthly credit repeating at least three times. Real, but on
#                  its own it is equally consistent with rent received, a
#                  standing transfer from a relative, or a loan drawdown.
#   narration 0.20 the strongest single signal available: the employer told the
#                  bank what it was. Weighted below the pattern because a
#                  narration can be copied into an unrelated transfer.
#   tree 0.08      the category tree independently placed the rows in
#                  Income > Salary via merchant and concept rules.
#   repeats 0.10   six or more occurrences — the detector's own "high" tier. Six
#                  months is the period a lender asks for precisely because
#                  shorter runs do not distinguish a job from a windfall.
#   amount 0.07    coefficient of variation of the amounts at or under 5%. Real
#                  salaries move a little (variable pay, tax); anything flatter
#                  than 5% is a fixed credit.
#   day 0.05       day-of-month standard deviation at or under 3 days. Payroll
#                  lands on a date, shifted only by weekends and holidays.
_SALARY_EVIDENCE = {
    "base": 0.50,
    "narration": 0.20,
    "tree_category": 0.08,
    "six_or_more_occurrences": 0.10,
    "amount_stable": 0.07,
    "day_stable": 0.05,
}

# Above this, the series is treated as a salary rather than merely a candidate.
# 0.70 is the point at which the base pattern must have been corroborated by at
# least one other signal — the bare pattern alone scores 0.50 and cannot reach
# it.
SALARY_ACCEPT_THRESHOLD = 0.70

# Amount CV at or under which a series is "fixed".
AMOUNT_STABLE_CV = 0.05
# Day-of-month standard deviation at or under which a series is "on a date".
DAY_STABLE_STDEV = 3.0

# Confidence attached to a recurring series, by the detector's own tier. High
# means six or more occurrences; medium means three to five. Neither is a fact,
# so neither reaches 1.0.
_TIER_CONFIDENCE = {"high": 0.85, "medium": 0.65}

# Flows that are not income even though they are credits: money moved between
# the account holder's own accounts, and money coming back from a failed payment.
_NON_INCOME_FLOWS = {"TRANSFER", "REVERSAL"}


def compute_income(txns: Sequence[CanonicalTxn],
                   recurring: Sequence[Dict[str, Any]],
                   warnings: WarningCollector,
                   parse_confidence: float = 1.0
                   ) -> Tuple[Dict[str, Metric], Dict[str, Any]]:
    credits = [t for t in txns if (t.credit_paise or 0) > 0]
    total_credits = sum(int(t.credit_paise or 0) for t in credits)
    months = months_covered(txns)
    buckets = monthly_buckets(txns)
    monthly_inflows = [b["inflow"] for b in buckets.values()]

    raw: Dict[str, Any] = {
        "total_credits_paise": total_credits,
        "months": months,
        "monthly_inflow_paise": dict((m, b["inflow"]) for m, b in buckets.items()),
    }
    metrics: Dict[str, Metric] = {}

    metrics["total_credits"] = calculated(
        rupees(total_credits), unit="INR", confidence=parse_confidence,
        method="sum_of_credit_column",
        note=f"{len(credits)} credit rows.")

    monthly_income_paise = int(round(total_credits / months)) if months else None
    raw["monthly_income_paise"] = monthly_income_paise
    metrics["monthly_income"] = (
        calculated(rupees(monthly_income_paise), unit="INR",
                   confidence=parse_confidence,
                   method="total_credits_divided_by_calendar_months_covered",
                   note=f"Averaged over the {months} calendar months the "
                        "statement touches; partial end months count as whole "
                        "months, which understates rather than flatters.")
        if monthly_income_paise is not None
        else unavailable("The statement period could not be resolved.")
    )

    cv = coefficient_of_variation(monthly_inflows)
    raw["income_cv"] = cv
    if cv is None:
        reason = ("At least two calendar months of credits are needed to measure "
                  "how much income varies.")
        metrics["income_volatility"] = unavailable(reason)
        metrics["income_stability"] = unavailable(reason)
    else:
        metrics["income_volatility"] = calculated(
            round(cv, 4), unit="ratio", confidence=parse_confidence,
            method="population_coefficient_of_variation_of_monthly_credit_totals",
            note="Standard deviation of monthly income over its mean. 0 is a "
                 "flat income; 1 means the spread equals the average.")
        metrics["income_stability"] = calculated(
            round(stability_from_cv(cv), 4), unit="ratio",
            confidence=parse_confidence,
            method="one_over_one_plus_coefficient_of_variation",
            note="1.0 is a perfectly steady income; 0.5 means the month-to-month "
                 "spread equals the average month.")

    # ---- salary ----------------------------------------------------------
    groups = series_groups(txns)
    salary = _detect_salary(recurring, groups)
    raw["salary"] = salary
    if salary is None:
        warnings.add(
            W_NO_SALARY_DETECTED,
            "No credit series repeated monthly with enough corroboration to be "
            "called a salary. Income figures above are still valid; they are "
            "just not attributable to payroll.",
            severity="info",
        )
        metrics["salary"] = unavailable(
            "No monthly credit series reached the salary evidence threshold of "
            f"{SALARY_ACCEPT_THRESHOLD:.2f}.")
        metrics["salary_monthly"] = unavailable(
            "No salary series was identified.")
    else:
        metrics["salary"] = infer(
            rupees(salary["amount_paise"]), unit="INR",
            method="monthly_recurring_credit_with_narration_and_stability_evidence",
            confidence=salary["confidence"],
            note="Median of the series' credits, which is robust to a single "
                 "bonus month in a way the mean is not.",
            evidence=salary["evidence"])
        metrics["salary_monthly"] = infer(
            rupees(salary["monthly_paise"]), unit="INR",
            method="salary_series_total_divided_by_months_covered",
            confidence=salary["confidence"],
            evidence={"series": salary["evidence"]["series_name"],
                      "months": months})

    # ---- recurring income ------------------------------------------------
    inflow_series = [s for s in recurring if s.get("direction") == "inflow"]
    raw["recurring_income"] = inflow_series
    if inflow_series:
        monthly_equiv = _monthly_equivalent_paise(inflow_series)
        raw["recurring_income_monthly_paise"] = monthly_equiv
        tier_conf = _tier_confidence(inflow_series)
        metrics["recurring_income"] = infer(
            [_series_api(s) for s in inflow_series], unit="list",
            method="repeat_interval_clustering_over_narration_groups",
            confidence=tier_conf,
            note=f"{len(inflow_series)} credit series repeating on a recognised "
                 "cadence, at least three occurrences each.")
        metrics["recurring_income_monthly"] = infer(
            rupees(monthly_equiv), unit="INR",
            method="sum_of_series_amounts_normalised_to_a_30_day_month",
            confidence=tier_conf,
            note="Each series' typical amount scaled by 30 / its cadence in "
                 "days, so a quarterly credit contributes a third of itself.")
    else:
        raw["recurring_income_monthly_paise"] = 0
        metrics["recurring_income"] = unavailable(
            "No credit repeated at least three times on a recognised cadence.")
        metrics["recurring_income_monthly"] = unavailable(
            "No recurring credit series was identified.")

    # ---- how many distinct payers ----------------------------------------
    sources, identified_share = _income_sources(credits)
    raw["income_sources"] = sources
    if not credits:
        metrics["income_sources"] = unavailable("There are no credit rows.")
    else:
        # Even when every credit yields a key, this tops out below 0.90: two
        # spellings of one payer ("ACME TECH PVT LTD" / "ACME TECHNOLOGIES")
        # count twice, so the number is an upper bound on distinct payers.
        confidence = 0.55 + 0.35 * identified_share
        metrics["income_sources"] = infer(
            len(sources), unit="count",
            method="distinct_normalised_counterparty_keys_over_income_credits",
            confidence=confidence,
            note="Upper bound: two spellings of the same payer count twice.",
            evidence={"sources": sources[:25],
                      "identified_share": round(identified_share, 3)})

    return metrics, raw


# --------------------------------------------------------------------- salary

def _detect_salary(recurring: Sequence[Dict[str, Any]],
                   groups: Dict[Tuple[str, str], List[CanonicalTxn]]
                   ) -> Optional[Dict[str, Any]]:
    """Score every monthly credit series; return the best if it clears the bar.

    "Best" is by confidence, then by amount. Confidence first because a large
    but poorly-evidenced credit series (a quarterly loan drawdown, say) must not
    outrank a smaller well-evidenced payroll credit.
    """
    best: Optional[Dict[str, Any]] = None
    for series in recurring:
        if series.get("direction") != "inflow":
            continue
        if series.get("cadence") != "monthly":
            continue
        if series.get("occurrences", 0) < 3:
            continue

        rows = series_members(groups, series)
        if not rows:
            continue

        amounts = [int(r.credit_paise or 0) for r in rows]
        dates = [r.txn_date for r in rows if r.txn_date]
        amount_cv = coefficient_of_variation(amounts)
        day_spread = day_of_month_spread(dates)

        narration_hit = any(
            any(token in narration_of(r).upper() for token in _SALARY_NARRATION)
            for r in rows
        )
        tree_hit = any(
            (r.category_path or "").startswith(" > ".join(_SALARY_PATH_PREFIX))
            for r in rows
        )

        score = _SALARY_EVIDENCE["base"]
        matched = ["monthly_recurring_credit"]
        if narration_hit:
            score += _SALARY_EVIDENCE["narration"]
            matched.append("payroll_narration")
        if tree_hit:
            score += _SALARY_EVIDENCE["tree_category"]
            matched.append("category_tree_income_salary")
        if series.get("occurrences", 0) >= 6:
            score += _SALARY_EVIDENCE["six_or_more_occurrences"]
            matched.append("six_or_more_occurrences")
        if amount_cv is not None and amount_cv <= AMOUNT_STABLE_CV:
            score += _SALARY_EVIDENCE["amount_stable"]
            matched.append("amount_stable")
        if day_spread is not None and day_spread <= DAY_STABLE_STDEV:
            score += _SALARY_EVIDENCE["day_stable"]
            matched.append("day_of_month_stable")

        if score < SALARY_ACCEPT_THRESHOLD:
            continue

        # Clamped here, not only at the `Metric`. Downstream — affordability
        # multiplies this confidence into every ratio it reports — reads
        # `raw["salary"]["confidence"]`, and an unclamped 1.00 leaking into that
        # product is how an inferred DTI would come back claiming certainty.
        score = min(score, MAX_INFERRED_CONFIDENCE)

        candidate = {
            "amount_paise": int(series["amount_paise"]),
            "monthly_paise": int(round(sum(amounts) / max(1, len(
                {(d.year, d.month) for d in dates})))),
            "confidence": score,
            "evidence": {
                "series_name": series.get("name"),
                "cadence": series.get("cadence"),
                "occurrences": series.get("occurrences"),
                "median_gap_days": series.get("median_gap_days"),
                "typical_day_of_month": series.get("typical_day"),
                "amount_cv": round(amount_cv, 4) if amount_cv is not None else None,
                "day_of_month_stdev": (round(day_spread, 2)
                                       if day_spread is not None else None),
                "matched_evidence": matched,
                "dates": [d.isoformat() for d in dates],
                "amounts": [rupees(a) for a in amounts],
            },
        }
        if best is None or (candidate["confidence"], candidate["amount_paise"]) > \
                (best["confidence"], best["amount_paise"]):
            best = candidate
    return best


# ------------------------------------------------------------------ recurring

def _monthly_equivalent_paise(series: Sequence[Dict[str, Any]]) -> int:
    """Series amounts normalised to a 30-day month so they can be added up."""
    total = 0.0
    for s in series:
        nominal = max(1, int(s.get("nominal_days") or 30))
        total += float(s["amount_paise"]) * (30.0 / nominal)
    return int(round(total))


def _tier_confidence(series: Sequence[Dict[str, Any]]) -> float:
    """Weight the detector's own tiers by how much money each series carries."""
    weighted, total = 0.0, 0
    for s in series:
        amount = abs(int(s.get("amount_paise") or 0)) or 1
        weighted += _TIER_CONFIDENCE.get(s.get("confidence"), 0.6) * amount
        total += amount
    return weighted / total if total else 0.6


def _series_api(s: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": s.get("name"),
        "cadence": s.get("cadence"),
        "occurrences": s.get("occurrences"),
        "amount": rupees(int(s["amount_paise"])),
        "median_gap_days": s.get("median_gap_days"),
        "typical_day_of_month": s.get("typical_day"),
        "last_seen": s.get("last_date"),
        "pattern_confidence": _TIER_CONFIDENCE.get(s.get("confidence"), 0.6),
    }


# -------------------------------------------------------------------- sources

def _income_sources(credits: Sequence[CanonicalTxn]
                    ) -> Tuple[List[str], float]:
    """Distinct normalised payer keys, and how many credits yielded one."""
    keys: Dict[str, int] = {}
    identified = 0
    considered = 0
    for t in credits:
        if (t.flow_type or "") in _NON_INCOME_FLOWS:
            continue
        considered += 1
        key = clean_key(t)
        if not key or key == "Unidentified":
            continue
        identified += 1
        keys[key] = keys.get(key, 0) + 1
    share = safe_div(identified, considered) or 0.0
    ordered = sorted(keys, key=lambda k: (-keys[k], k))
    return ordered, share
