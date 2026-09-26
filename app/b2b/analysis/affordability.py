"""Can this borrower carry a new loan, and by how much.

Every ratio here divides an inferred number by another inferred number — income
that was inferred from recurring credits, EMIs that were inferred from recurring
debits — so every ratio is INFERRED, and its confidence is the *product* of the
confidences of its inputs, not the average. Multiplying is the conservative
choice and the right one: a ratio can only be trusted as far as its weakest
input, and averaging would let a well-evidenced income figure paper over a
guessed EMI.

The proposed EMI itself is the exception. Given a principal, a rate and a
tenure, it is the standard amortisation formula and nothing else — pure
arithmetic on numbers the caller supplied, so it is CALCULATED at full
confidence. The uncertainty is entirely in whether the borrower can pay it.

Without loan parameters the module still answers the questions that do not need
them: the existing-EMI-to-income ratio, and disposable income after obligations.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from app.b2b.metrics import Metric, WarningCollector, calculated, unavailable
from app.b2b.analysis.util import infer, rupees, safe_div

# ---------------------------------------------------------------- thresholds
#
# FOIR / DTI bands. These are the fixed-obligation-to-income ratios Indian
# retail lenders actually underwrite to, and they are constants here rather
# than magic numbers in a branch so that a caller can read the policy off the
# response and argue with it.
#
# 0.40  Comfortable. Forty percent of net income going to fixed obligations is
#       the ceiling most public-sector and large private lenders apply to a
#       salaried applicant with no compensating factors. Below it, the loan is
#       serviceable on the income evidence alone.
# 0.50  Stretched. Between 40% and 50% the file is still bankable but needs
#       something else in its favour — a co-applicant, a longer tenure, a larger
#       down payment. Reported as MARGINAL, never as a pass.
# Above 0.50 the proposed obligation is not supported by the income this
# statement evidences. That is a statement about the evidence, not a credit
# decision: the borrower may have income this account never sees.
DTI_COMFORTABLE = 0.40
DTI_STRETCHED = 0.50

# A borrower also has to eat. Even at a passing DTI, a file that leaves less
# than this much per month after every fixed obligation is flagged, because the
# ratio alone stops being meaningful at low absolute incomes — 40% of ₹18,000
# and 40% of ₹4,00,000 are not the same decision.
MIN_DISPOSABLE_PAISE = 15_000_00

VERDICT_AFFORDABLE = "AFFORDABLE"
VERDICT_MARGINAL = "MARGINAL"
VERDICT_NOT_AFFORDABLE = "NOT_AFFORDABLE"
VERDICT_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


def emi_for(principal: float, annual_rate_pct: float, tenure_months: int
            ) -> Optional[float]:
    """Standard reducing-balance amortisation payment.

        EMI = P * r * (1 + r)^n / ((1 + r)^n - 1),  r = annual% / 12 / 100

    The zero-rate case is not a limit worth taking numerically — at r = 0 the
    formula is 0/0 — so it is handled directly as P / n, which is what an
    interest-free instalment is.
    """
    if not principal or not tenure_months or tenure_months <= 0:
        return None
    r = float(annual_rate_pct) / 12.0 / 100.0
    n = int(tenure_months)
    if r <= 0:
        return round(float(principal) / n, 2)
    factor = (1.0 + r) ** n
    return round(float(principal) * r * factor / (factor - 1.0), 2)


def compute_affordability(income_raw: Dict[str, Any],
                          expenses_raw: Dict[str, Any],
                          debt_raw: Dict[str, Any],
                          income_metrics: Dict[str, Metric],
                          debt_metrics: Dict[str, Metric],
                          warnings: WarningCollector,
                          loan: Optional[Dict[str, Any]] = None
                          ) -> Tuple[Dict[str, Metric], Dict[str, Metric], Dict[str, Any]]:
    """Returns (loan_block, affordability_block, raw)."""
    loan = loan or {}
    metrics: Dict[str, Metric] = {}
    loan_block: Dict[str, Metric] = {}
    raw: Dict[str, Any] = {}

    # Income basis: the salary if one was evidenced, otherwise average monthly
    # credits. Salary first because a lender underwrites to reliable income, and
    # total credits include one-off receipts a borrower cannot repeat.
    salary = income_raw.get("salary")
    if salary:
        income_paise = int(salary["monthly_paise"])
        income_conf = float(salary["confidence"])
        income_basis = "detected_salary"
    else:
        income_paise = income_raw.get("monthly_income_paise")
        # Average monthly credits is arithmetic, but using it *as income* is the
        # inference: it counts refunds, transfers in and one-off receipts as if
        # they recurred.
        income_conf = 0.60
        income_basis = "average_monthly_credits"

    existing_emi_paise = int(debt_raw.get("total_monthly_emi_paise") or 0)
    emi_conf = _confidence_of(debt_metrics.get("total_monthly_emi"), default=0.60)
    monthly_expense_paise = expenses_raw.get("monthly_expenses_paise") or 0

    raw.update({
        "income_paise": income_paise,
        "income_basis": income_basis,
        "income_confidence": income_conf,
        "existing_emi_paise": existing_emi_paise,
    })

    if not income_paise:
        reason = ("No monthly income could be established from this statement, "
                  "so no affordability ratio can be computed.")
        for key in ("existing_emi_to_income", "proposed_dti", "total_dti",
                    "disposable_income", "verdict"):
            metrics[key] = unavailable(reason)
        if loan:
            loan_block.update(_loan_echo(loan))
        return loan_block, metrics, raw

    # ---- ratios that need no loan parameters -----------------------------
    existing_ratio = safe_div(existing_emi_paise, income_paise)
    raw["existing_emi_to_income"] = existing_ratio
    metrics["existing_emi_to_income"] = infer(
        round(existing_ratio, 4), unit="ratio",
        method="detected_monthly_emi_over_monthly_income",
        confidence=income_conf * emi_conf,
        note="Both halves are inferred; the confidence is the product, not the "
             "average, so the weaker input governs.",
        evidence={"income_basis": income_basis,
                  "monthly_income": rupees(income_paise),
                  "monthly_emi": rupees(existing_emi_paise)})

    # Disposable income after *everything* the statement shows leaving, not just
    # debt. Average monthly expenses already contain the EMIs, so subtracting
    # both would double-count them.
    disposable_paise = int(income_paise - monthly_expense_paise)
    raw["disposable_income_paise"] = disposable_paise
    metrics["disposable_income"] = infer(
        rupees(disposable_paise), unit="INR",
        method="monthly_income_minus_average_monthly_expenses",
        confidence=income_conf,
        note="Average monthly outgoings already include existing EMIs, so they "
             "are not subtracted twice.")

    if not loan:
        for key in ("proposed_emi", "proposed_dti", "total_dti", "verdict"):
            metrics[key] = unavailable(
                "No loan parameters were supplied; only the ratios that do not "
                "depend on them are reported.")
        return loan_block, metrics, raw

    # ---- the proposed loan ----------------------------------------------
    amount = loan.get("loan_amount")
    rate = loan.get("interest_rate")
    tenure = loan.get("tenure_months")
    loan_block.update(_loan_echo(loan))

    proposed = emi_for(amount, rate, tenure) if (amount and tenure) else None
    if proposed is None:
        reason = ("loan_amount and tenure_months are both required to compute a "
                  "proposed instalment.")
        for key in ("proposed_emi", "proposed_dti", "total_dti", "verdict"):
            metrics[key] = unavailable(reason)
        return loan_block, metrics, raw

    proposed_paise = int(round(proposed * 100))
    raw["proposed_emi_paise"] = proposed_paise

    # Pure arithmetic on numbers the caller gave us: nothing about this figure
    # depends on reading the statement, so it is CALCULATED at full confidence.
    emi_metric = calculated(
        proposed, unit="INR", confidence=1.0,
        method="reducing_balance_amortisation_P_r_pow_over_pow_minus_one",
        note=f"₹{amount:,.0f} at {rate}% a year for {tenure} months, monthly "
             "reducing balance.")
    loan_block["proposed_emi"] = emi_metric
    metrics["proposed_emi"] = emi_metric
    loan_block["total_repayable"] = calculated(
        round(proposed * int(tenure), 2), unit="INR", confidence=1.0,
        method="proposed_emi_times_tenure_months")
    loan_block["total_interest"] = calculated(
        round(proposed * int(tenure) - float(amount), 2), unit="INR",
        confidence=1.0,
        method="total_repayable_minus_principal")

    proposed_ratio = safe_div(proposed_paise, income_paise)
    total_ratio = safe_div(proposed_paise + existing_emi_paise, income_paise)
    raw["proposed_dti"] = proposed_ratio
    raw["total_dti"] = total_ratio

    metrics["proposed_dti"] = infer(
        round(proposed_ratio, 4), unit="ratio",
        method="proposed_emi_over_inferred_monthly_income",
        confidence=income_conf,
        note="The instalment is exact; the income under it is inferred, which is "
             "where all the uncertainty in this ratio lives.")
    metrics["total_dti"] = infer(
        round(total_ratio, 4), unit="ratio",
        method="proposed_plus_existing_emi_over_inferred_monthly_income",
        confidence=income_conf * emi_conf,
        evidence={"existing_emi": rupees(existing_emi_paise),
                  "proposed_emi": proposed,
                  "monthly_income": rupees(income_paise)})

    post_loan_disposable = disposable_paise - proposed_paise
    raw["post_loan_disposable_paise"] = post_loan_disposable
    metrics["disposable_income_after_proposed_loan"] = infer(
        rupees(post_loan_disposable), unit="INR",
        method="disposable_income_minus_proposed_emi",
        confidence=income_conf)

    verdict, why = _verdict(total_ratio, post_loan_disposable)
    raw["verdict"] = verdict
    metrics["verdict"] = infer(
        verdict, unit="verdict",
        method="total_dti_against_documented_foir_bands_plus_disposable_floor",
        confidence=income_conf * emi_conf,
        note=why,
        evidence={
            "total_dti": round(total_ratio, 4),
            "dti_comfortable_below": DTI_COMFORTABLE,
            "dti_stretched_below": DTI_STRETCHED,
            "minimum_disposable": rupees(MIN_DISPOSABLE_PAISE),
            "disposable_after_loan": rupees(post_loan_disposable),
        })
    return loan_block, metrics, raw


def _verdict(total_dti: Optional[float], disposable_paise: int
             ) -> Tuple[str, str]:
    if total_dti is None:
        return VERDICT_INSUFFICIENT_DATA, "No income basis to divide by."
    if disposable_paise < MIN_DISPOSABLE_PAISE:
        return (VERDICT_NOT_AFFORDABLE,
                f"Would leave {rupees(disposable_paise)} a month after all "
                f"outgoings, below the {rupees(MIN_DISPOSABLE_PAISE)} floor.")
    if total_dti < DTI_COMFORTABLE:
        return (VERDICT_AFFORDABLE,
                f"Total obligations would be {total_dti:.1%} of evidenced "
                f"income, inside the {DTI_COMFORTABLE:.0%} band.")
    if total_dti < DTI_STRETCHED:
        return (VERDICT_MARGINAL,
                f"Total obligations would be {total_dti:.1%} of evidenced "
                f"income — between the {DTI_COMFORTABLE:.0%} and "
                f"{DTI_STRETCHED:.0%} bands, so it needs a compensating factor.")
    return (VERDICT_NOT_AFFORDABLE,
            f"Total obligations would be {total_dti:.1%} of evidenced income, "
            f"above the {DTI_STRETCHED:.0%} band.")


def _loan_echo(loan: Dict[str, Any]) -> Dict[str, Metric]:
    """Echo what was asked for, so the answer is self-describing."""
    out: Dict[str, Metric] = {}
    if loan.get("loan_amount") is not None:
        out["requested_amount"] = calculated(
            float(loan["loan_amount"]), unit="INR", confidence=1.0,
            method="supplied_by_caller")
    if loan.get("interest_rate") is not None:
        out["interest_rate"] = calculated(
            float(loan["interest_rate"]), unit="percent_per_year",
            confidence=1.0, method="supplied_by_caller")
    if loan.get("tenure_months") is not None:
        out["tenure_months"] = calculated(
            int(loan["tenure_months"]), unit="months", confidence=1.0,
            method="supplied_by_caller")
    return out


def _confidence_of(metric: Optional[Metric], default: float) -> float:
    if metric is None or metric.confidence is None:
        return default
    return float(metric.confidence)
