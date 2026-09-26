"""Financial stress, built only from what a bank statement can actually show.

The temptation in a risk block is to invent indicators. This one is restricted
to five things a statement genuinely evidences, plus the two engines the product
already runs against stored ledgers:

1. **Minimum-balance breaches.** Days the end-of-day balance fell below a
   threshold. Real, and cheap to check, but only meaningful against a threshold
   somebody chose — see `DEFAULT_MIN_BALANCE_PAISE`.
2. **Penal and bounce charges.** The strongest single distress signal on a
   statement, because the bank itself is asserting that a payment failed.
   Detected by `anomaly_engine._detect_unexpected_bank_charges`, which already
   keyword-matches PENAL / BOUNCE CHG / MIN BAL / OD INT and friends.
3. **Negative-balance days.** Days the account was overdrawn, from the same
   time-weighted daily series the average daily balance is built from.
4. **Overdraft usage.** How deep and how long, plus interest and fee narrations.
5. **The anomaly findings.** All sixteen detectors, run over the canonical rows.

Plus compliance: the eight policy evaluators, driven by lightweight stand-ins
for the seeded default rules.

Both engines are reused rather than reimplemented, and both are called through
their *private per-detector* entry points — `detect_anomalies(db, user_id)` and
`evaluate_policies(db, ...)` open a session, write findings and commit. The
detectors themselves take a plain list and return plain dicts, which is exactly
what a stateless API needs.

Imports are lazy. `app.compliance.*` reaches the ORM at module scope, and this
package must stay importable with no database configured; when that import
fails, the affected block degrades to `unavailable` with the reason rather than
taking the analysis down.
"""
from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.b2b.canonical import CanonicalTxn
from app.b2b.metrics import Metric, WarningCollector, calculated, unavailable
from app.b2b.analysis.util import infer, narration_of, rupees, safe_div

logger = logging.getLogger(__name__)

# The minimum-balance threshold to test against when the caller does not name
# one. ₹1,000 is the lowest average-monthly-balance requirement in common use on
# an Indian savings account (basic/BSBDA products aside, which require none).
# Choosing the *lowest* common requirement means this under-reports breaches on
# an account with a higher requirement rather than inventing breaches on an
# account with none — the conservative direction for a distress indicator.
# Callers who know the product pass config={"min_balance_paise": ...}.
DEFAULT_MIN_BALANCE_PAISE = 1_000_00

# Narrations that say a payment failed or a penalty was levied. Overlaps the
# anomaly detector's list on purpose: the detector answers "is this a finding",
# this answers "how much did distress cost", and they must agree on the words.
_PENAL_NARRATION = re.compile(
    r"PENAL|BOUNCE|BOUNCED|CHQ\s*RTN|CHEQUE\s*RETURN|RETURN\s*CHG|DISHONOUR|"
    r"MIN\s*BAL|AMB\s*CHG|NON\s*MAINT|OD\s*INT|OVERDRAFT|ECS\s*RETURN|"
    r"ECS\s*REJECT|NACH\s*RETURN|INSUFFICIENT\s*FUND|LATE\s*FEE|LATE\s*PAYMENT",
    re.IGNORECASE,
)

# Weights for the composite stress score. They sum to 1.0 and each term is a
# 0-1 sub-score, so the composite is directly readable as "how much of the
# available distress evidence is present".
#
# Bounce and penal charges carry the most weight because they are the only
# indicator where the bank itself has asserted a failure; the rest are
# thresholds we chose. Overdraft depth is weighted above breach *count* because
# being ₹50,000 overdrawn for a day is a different fact from touching ₹900 for
# an afternoon.
_STRESS_WEIGHTS = {
    "penal_charges": 0.35,
    "negative_days": 0.25,
    "min_balance_breaches": 0.20,
    "anomalies": 0.20,
}


@dataclass
class _RuleStandIn:
    """Attribute-compatible stand-in for a `PolicyRule` row.

    The policy evaluators only ever read attributes off `rule` — id, code, name,
    severity, threshold_paise, threshold_count, direction_scope,
    narration_filter — and never query through it. Building these from
    `rules_seed.DEFAULT_RULES` means a stateless API request is checked against
    exactly the rule set a seeded account gets, with no database and no user.
    """
    code: str
    name: str
    rule_type: str
    severity: str
    category: Optional[str] = None
    description: Optional[str] = None
    statute_ref: Optional[str] = None
    threshold_paise: Optional[int] = None
    threshold_count: Optional[int] = None
    direction_scope: Optional[str] = None
    narration_filter: Optional[str] = None
    id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.id is None:
            self.id = self.code


def compute_risk(txns: Sequence[CanonicalTxn],
                 balances_raw: Dict[str, Any],
                 warnings: WarningCollector,
                 parse_confidence: float = 1.0,
                 config: Optional[Dict[str, Any]] = None
                 ) -> Tuple[Dict[str, Metric], Dict[str, Any]]:
    config = config or {}
    min_balance_paise = int(config.get("min_balance_paise",
                                       DEFAULT_MIN_BALANCE_PAISE))
    daily = balances_raw.get("daily_series") or []
    metrics: Dict[str, Metric] = {}
    raw: Dict[str, Any] = {"min_balance_threshold_paise": min_balance_paise}

    # ---- 1 & 3 & 4: what the balance series says -------------------------
    if not daily:
        for key in ("min_balance_breaches", "negative_balance_days",
                    "overdraft_usage"):
            metrics[key] = unavailable(
                "No usable balance column, so day-by-day balance behaviour "
                "cannot be reconstructed.")
        breach_days = negative_days = 0
        worst_overdraft = 0
        breach_score = negative_score = 0.0
    else:
        breaches = [(d, b) for d, b in daily if b < min_balance_paise]
        negatives = [(d, b) for d, b in daily if b < 0]
        breach_days, negative_days = len(breaches), len(negatives)
        worst_overdraft = min([b for _, b in negatives], default=0)
        total_days = len(daily)
        raw.update({
            "days": total_days,
            "min_balance_breach_days": breach_days,
            "negative_balance_days": negative_days,
            "worst_overdraft_paise": worst_overdraft,
        })
        breach_score = safe_div(breach_days, total_days) or 0.0
        negative_score = safe_div(negative_days, total_days) or 0.0

        metrics["min_balance_breaches"] = calculated(
            breach_days, unit="days", confidence=parse_confidence,
            method="days_with_end_of_day_balance_below_configured_minimum",
            note=f"Against a threshold of {rupees(min_balance_paise)}, which is "
                 "a default rather than this account's actual product "
                 "requirement — pass min_balance_paise to test the real one.",
            basis=balances_raw.get("basis"))
        metrics["negative_balance_days"] = calculated(
            negative_days, unit="days", confidence=parse_confidence,
            method="days_with_end_of_day_balance_below_zero")
        metrics["overdraft_usage"] = calculated(
            {"days_overdrawn": negative_days,
             "deepest_overdraft": rupees(abs(worst_overdraft)) if worst_overdraft else 0.0,
             "share_of_period": round(negative_score, 4)},
            unit="summary", confidence=parse_confidence,
            method="negative_end_of_day_balances_over_the_period")

    # ---- 2: penal and bounce charges -------------------------------------
    charge_rows, charges_paise = _penal_charges(txns)
    raw["penal_charges_paise"] = charges_paise
    raw["penal_charge_count"] = len(charge_rows)
    # A charge narration is the bank's own assertion, so this is close to fact —
    # but it is still a keyword read of free text, which is why it stops short
    # of certainty.
    metrics["penalty_and_bounce_charges"] = infer(
        rupees(charges_paise), unit="INR",
        method="penal_bounce_and_minimum_balance_charge_narrations",
        confidence=0.85 if charge_rows else 0.70,
        note="Fees the bank levied for a failed payment, a returned instrument "
             "or an unmet balance requirement.",
        evidence={"count": len(charge_rows), "rows": charge_rows[:25]})
    metrics["penalty_charge_count"] = infer(
        len(charge_rows), unit="count",
        method="penal_bounce_and_minimum_balance_charge_narrations",
        confidence=0.85 if charge_rows else 0.70)

    # Six or more charge events in a statement is chronic rather than
    # occasional — roughly one a month on a six-month file — so the sub-score
    # saturates there instead of growing without bound.
    penal_score = min(1.0, len(charge_rows) / 6.0)

    # ---- 5: the anomaly detectors ----------------------------------------
    findings, anomaly_error = _run_anomaly_detectors(txns)
    raw["anomalies"] = findings
    if anomaly_error:
        metrics["anomalies"] = unavailable(anomaly_error)
        anomaly_score = 0.0
    else:
        by_severity: Dict[str, int] = {}
        for f in findings:
            by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        metrics["anomalies"] = calculated(
            {"count": len(findings), "by_severity": by_severity,
             "findings": findings[:100]},
            unit="summary", confidence=parse_confidence,
            method="sixteen_banking_anomaly_detectors_over_the_canonical_rows",
            note="Each detector is a documented rule, not a model. A finding is "
                 "something to look at, not a conclusion.")
        # Weighted by severity: a critical finding is worth four low ones. The
        # sub-score saturates at the equivalent of two critical findings.
        weight = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        weighted = sum(weight.get(f["severity"], 1) for f in findings)
        anomaly_score = min(1.0, weighted / 8.0)

    # ---- compliance -------------------------------------------------------
    violations, per_rule, policy_error = _run_policy_rules(txns)
    raw["policy_violations"] = violations
    if policy_error:
        metrics["compliance"] = unavailable(policy_error)
    else:
        # The denominator is checks applied, not transactions: one row can be in
        # scope for several rules and each is a separate pass or fail. Rules
        # that applied to nothing are excluded entirely — scoring a ₹5,000 UPI
        # payment against a ₹10,00,000 cash rule passes trivially and inflates
        # the percentage until it means nothing.
        scored = [r for r in per_rule if r["applicable"]]
        applicable = sum(r["applicable"] for r in scored)
        failed = sum(r["violations"] for r in scored)
        metrics["compliance"] = calculated(
            {"checks_applied": applicable,
             "checks_failed": failed,
             "pass_rate": (round(100.0 * max(0, applicable - failed) / applicable, 1)
                           if applicable else None),
             "violations": violations[:100],
             "per_rule": per_rule},
            unit="summary", confidence=parse_confidence,
            method="default_policy_rule_set_evaluated_over_the_canonical_rows",
            note="Evaluated against the product's seeded default rule set. A "
                 "client operating a different policy should read per_rule and "
                 "apply their own.")

    # ---- composite --------------------------------------------------------
    components = {
        "penal_charges": round(penal_score, 4),
        "negative_days": round(negative_score, 4),
        "min_balance_breaches": round(breach_score, 4),
        "anomalies": round(anomaly_score, 4),
    }
    score = sum(_STRESS_WEIGHTS[k] * v for k, v in components.items())
    raw["stress_components"] = components
    raw["stress_score"] = score

    # The composite rests on four inferred sub-scores and on thresholds this
    # module chose, so it is never more than moderately confident. When the
    # balance column is missing, two of the four terms are structurally zero and
    # the score is a floor rather than a measurement — said out loud.
    stress_confidence = 0.70 if daily else 0.45
    metrics["financial_stress_score"] = infer(
        round(score, 4), unit="score_0_1",
        method="weighted_composite_of_charge_overdraft_and_anomaly_evidence",
        confidence=stress_confidence,
        note=("0 is no distress evidence, 1 is every indicator saturated. "
              + ("" if daily else
                 "Two of the four terms need a balance column and are zero "
                 "here, so this is a floor, not a measurement.")),
        evidence={"weights": _STRESS_WEIGHTS, "components": components})

    return metrics, raw


# ---------------------------------------------------------------- components

def _penal_charges(txns: Sequence[CanonicalTxn]
                   ) -> Tuple[List[Dict[str, Any]], int]:
    """Charge rows, using the anomaly detector where it is available."""
    detected_ids = set()
    try:
        from app.compliance.anomaly_engine import _detect_unexpected_bank_charges
        for finding in _detect_unexpected_bank_charges(list(txns), None):
            if finding.get("transaction_id") is not None:
                detected_ids.add(finding["transaction_id"])
    except Exception as exc:  # pragma: no cover - ORM import unavailable
        logger.warning("[b2b] bank-charge detector unavailable: %s", exc)

    rows: List[Dict[str, Any]] = []
    total = 0
    for t in txns:
        amount = int(t.debit_paise or 0)
        if amount <= 0:
            continue
        if t.id not in detected_ids and not _PENAL_NARRATION.search(narration_of(t)):
            continue
        total += amount
        rows.append({
            "date": t.txn_date.isoformat() if t.txn_date else None,
            "amount": rupees(amount),
            "narration": narration_of(t)[:160],
        })
    return rows, total


def _run_anomaly_detectors(txns: Sequence[CanonicalTxn]
                           ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """All sixteen detectors, called individually with user_id=None."""
    try:
        from app.compliance import anomaly_engine as ae
    except Exception as exc:  # pragma: no cover
        logger.warning("[b2b] anomaly engine unavailable: %s", exc)
        return [], f"The anomaly detectors could not be loaded: {type(exc).__name__}."

    detectors = [getattr(ae, name) for name in dir(ae)
                 if name.startswith("_detect_") and callable(getattr(ae, name))]
    rows = list(txns)
    findings: List[Dict[str, Any]] = []
    for detector in detectors:
        try:
            findings.extend(detector(rows, None) or [])
        except Exception as exc:  # pragma: no cover - one detector must not
            # take the other fifteen with it.
            logger.warning("[b2b] anomaly detector %s failed: %s",
                           getattr(detector, "__name__", "?"), exc)

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (order.get(f.get("severity"), 4),
                                 -(f.get("amount_paise") or 0)))
    return [_finding_api(f) for f in findings], None


def _run_policy_rules(txns: Sequence[CanonicalTxn]
                      ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    """The eight policy evaluators against stand-ins for the default rules."""
    try:
        from app.compliance import policy_engine as pe
        from app.compliance.rules_seed import DEFAULT_RULES
    except Exception as exc:  # pragma: no cover
        logger.warning("[b2b] policy engine unavailable: %s", exc)
        return [], [], f"The policy evaluators could not be loaded: {type(exc).__name__}."

    rows = list(txns)
    violations: List[Dict[str, Any]] = []
    per_rule: List[Dict[str, Any]] = []

    for spec in DEFAULT_RULES:
        rule = _RuleStandIn(
            code=spec["code"], name=spec["name"], rule_type=spec["rule_type"],
            severity=spec.get("severity", "medium"),
            category=spec.get("category"), description=spec.get("description"),
            statute_ref=spec.get("statute_ref"),
            threshold_paise=spec.get("threshold_paise"),
            threshold_count=spec.get("threshold_count"),
            direction_scope=spec.get("direction_scope"),
            narration_filter=spec.get("narration_filter"),
        )
        evaluator = pe._EVALUATORS.get(rule.rule_type)
        if evaluator is None:
            continue
        try:
            found, applicable = evaluator(rule, rows, None)
        except Exception as exc:  # pragma: no cover
            logger.warning("[b2b] policy rule %s failed: %s", rule.code, exc)
            continue
        per_rule.append({
            "code": rule.code, "name": rule.name, "category": rule.category,
            "severity": rule.severity, "statute_ref": rule.statute_ref,
            "applicable": applicable, "violations": len(found),
            "pass_pct": (round(100.0 * (applicable - len(found)) / applicable, 1)
                         if applicable else None),
        })
        for v in found:
            violations.append(_violation_api(rule, v))

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    violations.sort(key=lambda v: (order.get(v.get("severity"), 4),
                                   -(v.get("amount") or 0)))
    per_rule.sort(key=lambda r: -r["violations"])
    return violations, per_rule, None


# ------------------------------------------------------------- API rendering
# The engines return internal dicts carrying date objects, user ids and
# fingerprints. None of that belongs in a response, and dates are not JSON.

def _finding_api(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": f.get("anomaly_type"),
        "severity": f.get("severity"),
        "title": f.get("title"),
        "detail": f.get("detail"),
        "date": _iso(f.get("occurred_on")),
        "amount": rupees(f.get("amount_paise")) if f.get("amount_paise") is not None else None,
        "evidence": f.get("evidence") or {},
    }


def _violation_api(rule: _RuleStandIn, v: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rule_code": rule.code,
        "rule_name": rule.name,
        "category": rule.category,
        "statute_ref": rule.statute_ref,
        "severity": v.get("severity"),
        "detail": v.get("detail"),
        "date": _iso(v.get("occurred_on")),
        "amount": rupees(v.get("amount_paise")) if v.get("amount_paise") is not None else None,
        "evidence": v.get("evidence") or {},
    }


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value if value is None or isinstance(value, str) else str(value)
