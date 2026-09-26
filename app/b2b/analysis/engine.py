"""Orchestration: canonical rows in, one assembled analysis out.

Order matters and is not arbitrary:

1. `enrich` first, because every category total, the essentiality split, the
   EMI guard and the classification-coverage term all read fields it writes.
2. `detect_recurring_series` once, on the enriched rows, and the single result
   is handed to income, expenses and debt. Running it three times would be three
   times the work for identical output, and — worse — would let the three
   modules disagree about what a series is.
3. Balances before cashflow and risk, because both need the day-by-day balance
   series and the closing balance.
4. Affordability last, because it divides the outputs of income and debt.

Nothing here opens a session, makes a request, or writes a file.
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.b2b.canonical import CanonicalTxn
from app.b2b.metrics import (
    Metric,
    W_BALANCE_CONTINUITY_FAILED,
    W_CONTINUITY_UNVERIFIABLE,
    W_LOW_PARSE_CONFIDENCE,
    W_MULTI_CURRENCY,
    W_SHORT_HISTORY,
    W_SPARSE_HISTORY,
    WarningCollector,
    block,
    unavailable,
)
from app.b2b.analysis import affordability as affordability_mod
from app.b2b.analysis import balances as balances_mod
from app.b2b.analysis import cashflow as cashflow_mod
from app.b2b.analysis import debt as debt_mod
from app.b2b.analysis import expenses as expenses_mod
from app.b2b.analysis import income as income_mod
from app.b2b.analysis import risk as risk_mod
from app.b2b.analysis.enrich import classification_coverage, enrich
from app.b2b.analysis.util import (
    months_covered,
    period_bounds,
    period_days,
    sorted_rows,
)

logger = logging.getLogger(__name__)

PASSED = "PASSED"
FAILED = "FAILED"
NOT_VERIFIABLE = "NOT_VERIFIABLE"

# ---------------------------------------------------------- overall_confidence
#
#   overall_confidence = 0.50 * parse_confidence
#                      + 0.25 * continuity_score
#                      + 0.25 * classification_coverage
#
# parse_confidence (0.50)
#     How much of the document was read correctly, from the parser and
#     validator. It dominates because every other term is computed FROM the
#     parsed rows: if the rows are wrong, a clean continuity check and a high
#     coverage figure are both measuring the wrong thing. A confident answer
#     over a bad parse is the specific failure this weighting exists to prevent.
#
# continuity_score (0.25)
#     The share of consecutive stated balances whose difference equals the
#     transaction between them, within one rupee. This is the statement's own
#     internal proof and the only evidence available that no row was dropped or
#     duplicated. Independent of the parser's opinion of itself, which is why it
#     is worth as much as coverage despite being one arithmetic identity.
#
# classification_coverage (0.25)
#     The share of rows the category tree placed at or above its own review
#     threshold (0.60). Every income, expense and debt figure is a sum over
#     categories, so a response whose rows are mostly unplaced is a response
#     whose totals are mostly empty, however cleanly it parsed.
#
# When there is no balance column, continuity is unverifiable. Its term
# contributes ZERO and the remaining weights are NOT renormalised, which caps
# such a statement at 0.75. Renormalising would hand a statement with less
# evidence the same score as one with more — the exact upward fudge this formula
# is written to avoid. A caller who wants the balance-free figure can read
# reconciliation_status and the three components, all of which are returned.
_W_PARSE = 0.50
_W_CONTINUITY = 0.25
_W_COVERAGE = 0.25

# Below this, the parse is too poor for the numbers to be worth reading.
LOW_PARSE_CONFIDENCE = 0.70
# Lenders ask for six months; below three the monthly averages are noise.
SHORT_HISTORY_DAYS = 90
# Fewer rows than this per month is not a working account.
SPARSE_ROWS_PER_MONTH = 5


@dataclass
class AnalysisResult:
    """What the router serialises, plus what the tests assert against.

    `data` and `quality` are the response. `raw` is paise-precise internal state
    — the exact integers every figure was computed from — kept so a test can
    assert an exact minor-unit total without re-deriving it from a rounded
    rupee float, and so a future endpoint can expose an audit view without this
    layer having to recompute anything.
    """
    data: Dict[str, Any]
    quality: Dict[str, Any]
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_api(self) -> Dict[str, Any]:
        return {"data": self.data, "quality": self.quality}


def analyze(txns: Sequence[CanonicalTxn],
            *,
            statement_meta: Optional[Dict[str, Any]] = None,
            parse_quality: Optional[Dict[str, Any]] = None,
            country: str = "IN",
            currency: str = "INR",
            loan: Optional[Dict[str, Any]] = None,
            config: Optional[Dict[str, Any]] = None) -> AnalysisResult:
    statement_meta = dict(statement_meta or {})
    parse_quality = dict(parse_quality or {})
    config = dict(config or {})
    warnings = WarningCollector()

    rows = sorted_rows(txns)
    dropped = len(txns) - len(rows)
    if dropped:
        warnings.add(
            "ROWS_WITHOUT_DATE",
            f"{dropped} row(s) carried no transaction date and were excluded "
            "from every period-based figure.",
            severity="warning", detail={"count": dropped})

    parse_confidence = _parse_confidence(rows, parse_quality)
    if parse_confidence < LOW_PARSE_CONFIDENCE:
        warnings.add(
            W_LOW_PARSE_CONFIDENCE,
            f"The document parsed at {parse_confidence:.0%} confidence. Figures "
            "below inherit that uncertainty.",
            severity="critical", detail={"parse_confidence": parse_confidence})

    _currency_check(rows, currency, warnings)

    if not rows:
        return _empty_result(statement_meta, parse_quality, country, currency,
                             warnings, parse_confidence)

    # ---- 1. classify ------------------------------------------------------
    enrich(list(rows), warnings)
    coverage = classification_coverage(rows) or 0.0

    # ---- 2. recurring series, once ---------------------------------------
    ref_date = config.get("ref_date") or max(t.txn_date for t in rows)
    recurring = _detect_recurring(rows, ref_date)

    # ---- 3. the blocks ---------------------------------------------------
    bal_metrics, bal_raw = balances_mod.compute_balances(
        rows, warnings, parse_confidence)
    continuity = balances_mod.continuity_check(rows)

    inc_metrics, inc_raw = income_mod.compute_income(
        rows, recurring, warnings, parse_confidence)
    exp_metrics, exp_raw = expenses_mod.compute_expenses(
        rows, recurring, warnings, parse_confidence, config)
    debt_metrics, debt_raw = debt_mod.compute_debt(
        rows, recurring, warnings, parse_confidence)
    cf_metrics, cf_raw = cashflow_mod.compute_cashflow(
        rows, bal_raw, inc_raw, exp_raw, warnings, parse_confidence, ref_date)
    risk_metrics, risk_raw = risk_mod.compute_risk(
        rows, bal_raw, warnings, parse_confidence, config)
    loan_metrics, aff_metrics, aff_raw = affordability_mod.compute_affordability(
        inc_raw, exp_raw, debt_raw, inc_metrics, debt_metrics, warnings, loan)

    _history_warnings(rows, warnings)

    # ---- 4. quality -------------------------------------------------------
    if not continuity["verifiable"]:
        reconciliation = NOT_VERIFIABLE
        continuity_score = 0.0
        warnings.add(
            W_CONTINUITY_UNVERIFIABLE,
            "Fewer than two rows state a running balance, so the statement's "
            "internal arithmetic cannot be checked. Overall confidence is "
            "capped accordingly.",
            severity="warning")
    elif continuity["breaks"]:
        reconciliation = FAILED
        continuity_score = continuity["score"]
        warnings.add(
            W_BALANCE_CONTINUITY_FAILED,
            f"{len(continuity['breaks'])} of {continuity['pairs']} balance "
            "steps do not equal the transaction between them. Rows are probably "
            "missing, duplicated or misread.",
            severity="critical",
            detail={"breaks": continuity["breaks"][:20]})
    else:
        reconciliation = PASSED
        continuity_score = continuity["score"]

    overall = (_W_PARSE * parse_confidence
               + _W_CONTINUITY * continuity_score
               + _W_COVERAGE * coverage)

    quality = {
        "overall_confidence": round(overall, 4),
        "reconciliation_status": reconciliation,
        "confidence_components": {
            "parse_confidence": round(parse_confidence, 4),
            "balance_continuity": round(continuity_score, 4),
            "classification_coverage": round(coverage, 4),
            "weights": {"parse_confidence": _W_PARSE,
                        "balance_continuity": _W_CONTINUITY,
                        "classification_coverage": _W_COVERAGE},
            "note": "Weighted sum. When continuity is NOT_VERIFIABLE its term "
                    "is zero and the weights are not renormalised, so such a "
                    "statement cannot exceed 0.75.",
        },
        "continuity": {
            "verifiable": continuity["verifiable"],
            "checked_pairs": continuity["pairs"],
            "matched_pairs": continuity["matched"],
            "breaks": continuity["breaks"][:20],
        },
        "warnings": warnings.to_api(),
    }

    # ---- 5. assemble ------------------------------------------------------
    data = {
        "statement": _statement_block(rows, statement_meta, parse_quality,
                                      country, currency, parse_confidence),
        "transactions": _transactions_block(rows, config),
        "income": block(inc_metrics),
        "expenses": block(exp_metrics),
        "balances": block(bal_metrics),
        "cashflow": block(cf_metrics),
        "debt": block(debt_metrics),
        "loan": block(loan_metrics) if loan_metrics else None,
        "affordability": block(aff_metrics),
        "risk": block(risk_metrics),
        "financial_metrics": block(_financial_metrics(
            bal_metrics, inc_metrics, exp_metrics, cf_metrics, debt_metrics,
            aff_metrics, risk_metrics)),
    }

    return AnalysisResult(
        data=data,
        quality=quality,
        warnings=warnings.to_api(),
        raw={
            "balances": bal_raw, "income": inc_raw, "expenses": exp_raw,
            "debt": debt_raw, "cashflow": cf_raw, "risk": risk_raw,
            "affordability": aff_raw, "recurring": recurring,
            "continuity": continuity,
            "parse_confidence": parse_confidence,
            "classification_coverage": coverage,
        },
    )


# ------------------------------------------------------------------- helpers

def _detect_recurring(rows: Sequence[CanonicalTxn],
                      ref_date: datetime.date) -> List[Dict[str, Any]]:
    try:
        from app.treasury.recurring_detector import detect_recurring_series
        return detect_recurring_series(list(rows), ref_date=ref_date) or []
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[b2b] recurring detection failed: %s", exc)
        return []


def _parse_confidence(rows: Sequence[CanonicalTxn],
                      parse_quality: Dict[str, Any]) -> float:
    """The parser's own confidence, or the mean of the rows' if it gave none.

    The mean and not the minimum: one badly-read row out of four hundred is a
    row-level problem the response already flags per row, not a reason to
    discount every figure. A parser that reports a document-level confidence
    overrides this, because it can see things a row cannot — OCR fallbacks,
    missing pages, a table that changed shape halfway down.
    """
    for key in ("confidence", "overall_confidence", "parse_confidence"):
        value = parse_quality.get(key)
        if value is not None:
            return max(0.0, min(1.0, float(value)))
    if not rows:
        return 1.0
    return max(0.0, min(1.0, sum(float(t.parse_confidence or 0.0)
                                 for t in rows) / len(rows)))


def _currency_check(rows: Sequence[CanonicalTxn], currency: str,
                    warnings: WarningCollector) -> None:
    seen = {(t.currency or currency) for t in rows}
    if len(seen) > 1:
        warnings.add(
            W_MULTI_CURRENCY,
            "Rows in more than one currency were found. Totals add unlike "
            "units and should not be read as a single figure.",
            severity="critical", detail={"currencies": sorted(seen)})


def _history_warnings(rows: Sequence[CanonicalTxn],
                      warnings: WarningCollector) -> None:
    days = period_days(rows)
    if days < SHORT_HISTORY_DAYS:
        warnings.add(
            W_SHORT_HISTORY,
            f"The statement covers {days} days. Monthly averages, stability "
            "scores and any projection are weak below "
            f"{SHORT_HISTORY_DAYS} days.",
            severity="warning", detail={"days": days})
    months = months_covered(rows)
    if months and len(rows) / months < SPARSE_ROWS_PER_MONTH:
        warnings.add(
            W_SPARSE_HISTORY,
            f"{len(rows)} rows across {months} months. This does not look like "
            "a primary operating account, and figures derived from it describe "
            "only part of the picture.",
            severity="warning",
            detail={"rows": len(rows), "months": months})


def _statement_block(rows: Sequence[CanonicalTxn],
                     statement_meta: Dict[str, Any],
                     parse_quality: Dict[str, Any],
                     country: str, currency: str,
                     parse_confidence: float) -> Dict[str, Any]:
    start, end = period_bounds(rows)
    months = months_covered(rows)
    return {
        "period_start": start.isoformat() if start else None,
        "period_end": end.isoformat() if end else None,
        "days_covered": period_days(rows),
        "months_covered": months,
        "first_month_partial": bool(start and start.day > 1),
        "last_month_partial": bool(end and end.day < _days_in_month(end)),
        "transaction_count": len(rows),
        "country": country,
        "currency": currency,
        "parse_confidence": round(parse_confidence, 4),
        "source_format": (rows[0].source_format if rows else None),
        "bank": statement_meta.get("bank") or statement_meta.get("bank_name"),
        "account_number_masked": statement_meta.get("account_number_masked"),
        "account_type": statement_meta.get("account_type"),
        "account_holder": statement_meta.get("account_holder"),
        "stated_opening_balance": statement_meta.get("opening_balance"),
        "stated_closing_balance": statement_meta.get("closing_balance"),
        "parse_quality": parse_quality or None,
    }


def _transactions_block(rows: Sequence[CanonicalTxn],
                        config: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    if config.get("include_transactions") is False:
        return None
    include_narration = config.get("include_narration", True)
    limit = config.get("max_transactions")
    out = [t.to_api(include_narration=include_narration) for t in rows]
    return out[:int(limit)] if limit else out


def _financial_metrics(bal, inc, exp, cf, debt, aff, risk) -> Dict[str, Metric]:
    """The dozen figures a credit screen shows first, gathered in one place.

    Every entry is the same `Metric` object that appears in its own block, not a
    recomputation — so the headline and the detail can never drift apart.
    """
    picks = [
        ("average_daily_balance", bal.get("average_daily_balance")),
        ("closing_balance", bal.get("closing_balance")),
        ("monthly_income", inc.get("monthly_income")),
        ("salary", inc.get("salary")),
        ("income_stability", inc.get("income_stability")),
        ("monthly_expenses", exp.get("monthly_expenses")),
        ("essential_expenses", exp.get("essential_expenses")),
        ("monthly_surplus", cf.get("monthly_surplus")),
        ("savings_rate", cf.get("savings_rate")),
        ("cashflow_stability", cf.get("cashflow_stability")),
        ("total_monthly_emi", debt.get("total_monthly_emi")),
        ("existing_emi_to_income", aff.get("existing_emi_to_income")),
        ("total_dti", aff.get("total_dti")),
        ("financial_stress_score", risk.get("financial_stress_score")),
    ]
    return {name: metric for name, metric in picks if metric is not None}


def _days_in_month(d: datetime.date) -> int:
    import calendar
    return calendar.monthrange(d.year, d.month)[1]


def _empty_result(statement_meta, parse_quality, country, currency,
                  warnings, parse_confidence) -> AnalysisResult:
    """No dated rows: say so once, rather than returning zeros everywhere."""
    warnings.add(
        "NO_TRANSACTIONS",
        "The statement contained no dated transaction rows, so nothing could "
        "be analysed.",
        severity="critical")
    reason = "There are no transactions to compute this from."
    empty = {"__none__": unavailable(reason)}
    return AnalysisResult(
        data={
            "statement": _statement_block([], statement_meta, parse_quality,
                                          country, currency, parse_confidence),
            "transactions": [],
            "income": block(empty), "expenses": block(empty),
            "balances": block(empty), "cashflow": block(empty),
            "debt": block(empty), "loan": None,
            "affordability": block(empty), "risk": block(empty),
            "financial_metrics": block(empty),
        },
        quality={
            "overall_confidence": 0.0,
            "reconciliation_status": NOT_VERIFIABLE,
            "confidence_components": {
                "parse_confidence": round(parse_confidence, 4),
                "balance_continuity": 0.0,
                "classification_coverage": 0.0,
            },
            "warnings": warnings.to_api(),
        },
        warnings=warnings.to_api(),
        raw={},
    )
