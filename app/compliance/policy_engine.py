"""Evaluate stored transactions against the user's policy rules.

Each rule type has one evaluator. Evaluators return violation dicts, which are
upserted on (user_id, fingerprint) so a re-run refreshes detail without
duplicating findings or clobbering a status somebody already set.

The compliance percentage is deliberately computed over *applicable*
transactions rather than all of them. Scoring a Rs 5,000 UPI payment against the
Rs 10,00,000 cash reporting rule would pass trivially and inflate the number
until it means nothing — which is exactly how a hardcoded 100% happens by
accident.
"""
import datetime
import hashlib
import logging
import re
from collections import defaultdict

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.compliance.cash_inference import classify_cash
from app.models.compliance import (
    PolicyRule,
    PolicyViolation,
    RULE_AMOUNT_LIMIT,
    RULE_CASH_DAILY_AGGREGATE,
    RULE_CASH_PAYMENT_LIMIT,
    RULE_CASH_RECEIPT_LIMIT,
    RULE_DAILY_OUTFLOW_LIMIT,
    RULE_MIN_BALANCE,
    RULE_VELOCITY_LIMIT,
    RULE_WEEKEND_PAYMENT,
)
from app.models.transaction import Transaction

logger = logging.getLogger(__name__)

# Below this confidence a cash inference is a hint, not a finding.
CASH_MIN_CONFIDENCE = 0.6

_CASH_LABELS = {
    "cash_deposit": "Cash deposit",
    "cash_withdrawal": "Cash withdrawal",
    "cash_generic": "Cash transaction",
}


def _fp(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:64]


def _amount(t):
    return (t.debit_paise or 0) + (t.credit_paise or 0)


def _narration(t):
    return t.narration_clean or t.narration_raw or ""


def _matches_filter(rule, t) -> bool:
    """A rule with a narration_filter only applies to matching narrations."""
    pattern = getattr(rule, "narration_filter", None)
    if not pattern:
        return True
    try:
        return re.search(pattern, _narration(t), re.IGNORECASE) is not None
    except re.error:
        logger.warning("[Policy] rule %s has an invalid narration_filter regex", rule.code)
        return True


def _violation(rule, user_id, detail, *, txn=None, amount_paise=None,
               occurred_on=None, evidence=None, account_id=None, fp_parts=None):
    return {
        "user_id": user_id,
        "rule_id": rule.id,
        "transaction_id": txn.id if txn is not None else None,
        "account_id": account_id or (txn.account_id if txn is not None else None),
        "occurred_on": occurred_on or (txn.txn_date if txn is not None else None),
        "amount_paise": amount_paise if amount_paise is not None else (_amount(txn) if txn is not None else None),
        "severity": rule.severity,
        "detail": detail,
        "evidence": evidence or {},
        "fingerprint": _fp(rule.code, *(fp_parts or [txn.id if txn is not None else detail])),
    }


# ---------------------------------------------------------------------------
# Evaluators — each returns (violations, applicable_count)
# ---------------------------------------------------------------------------

def _eval_cash_single(rule, txns, user_id, want_direction):
    """One cash transaction at or above a threshold (269ST, 269SS/T, 40A(3))."""
    violations, applicable = [], 0
    for t in txns:
        if want_direction == "credit" and not (t.credit_paise or 0):
            continue
        if want_direction == "debit" and not (t.debit_paise or 0):
            continue

        if not _matches_filter(rule, t):
            continue
        cash, kind, confidence = classify_cash(_narration(t))
        if not cash or confidence < CASH_MIN_CONFIDENCE:
            continue
        applicable += 1

        amount = _amount(t)
        if amount >= (rule.threshold_paise or 0):
            violations.append(_violation(
                rule, user_id,
                f"{_CASH_LABELS.get(kind, 'Cash transaction')} of {amount/100:,.2f} is at or above the "
                f"{(rule.threshold_paise or 0)/100:,.0f} limit. "
                f"Cash inferred from narration at {int(confidence*100)}% confidence — verify.",
                txn=t,
                evidence={"cash_kind": kind, "cash_confidence": confidence,
                          "narration": _narration(t)[:200]},
            ))
    return violations, applicable


def _eval_cash_daily_aggregate(rule, txns, user_id):
    """All cash on one account on one day, summed (CTR)."""
    violations = []
    buckets = defaultdict(lambda: {"total": 0, "txns": []})
    for t in txns:
        if not _matches_filter(rule, t):
            continue
        cash, kind, confidence = classify_cash(_narration(t))
        if not cash or confidence < CASH_MIN_CONFIDENCE:
            continue
        if not t.txn_date:
            continue
        b = buckets[(t.account_id, t.txn_date)]
        b["total"] += _amount(t)
        b["txns"].append(t)

    applicable = len(buckets)
    for (account_id, day), b in buckets.items():
        if b["total"] >= (rule.threshold_paise or 0):
            violations.append(_violation(
                rule, user_id,
                f"Cash on this account totalled {b['total']/100:,.2f} on {day} across "
                f"{len(b['txns'])} transactions, at or above the "
                f"{(rule.threshold_paise or 0)/100:,.0f} reporting threshold.",
                amount_paise=b["total"],
                occurred_on=day,
                account_id=account_id,
                evidence={"transaction_ids": [str(x.id) for x in b["txns"][:50]],
                          "count": len(b["txns"])},
                fp_parts=[account_id, day],
            ))
    return violations, applicable


def _eval_amount_limit(rule, txns, user_id):
    violations, applicable = [], 0
    for t in txns:
        if rule.direction_scope == "debit" and not (t.debit_paise or 0):
            continue
        if rule.direction_scope == "credit" and not (t.credit_paise or 0):
            continue
        if not _matches_filter(rule, t):
            continue
        applicable += 1
        amount = (t.debit_paise or 0) if rule.direction_scope == "debit" else (
            (t.credit_paise or 0) if rule.direction_scope == "credit" else _amount(t)
        )
        if amount >= (rule.threshold_paise or 0):
            violations.append(_violation(
                rule, user_id,
                f"₹{amount/100:,.2f} exceeds the ₹{(rule.threshold_paise or 0)/100:,.0f} limit for '{rule.name}'.",
                txn=t,
            ))
    return violations, applicable


def _eval_daily_outflow(rule, txns, user_id):
    violations = []
    buckets = defaultdict(lambda: {"total": 0, "count": 0})
    for t in txns:
        if not t.txn_date or not (t.debit_paise or 0):
            continue
        b = buckets[(t.account_id, t.txn_date)]
        b["total"] += t.debit_paise or 0
        b["count"] += 1
    applicable = len(buckets)
    for (account_id, day), b in buckets.items():
        if b["total"] >= (rule.threshold_paise or 0):
            violations.append(_violation(
                rule, user_id,
                f"Total outflow on {day} was {b['total']/100:,.2f} across {b['count']} "
                f"payments, above the {(rule.threshold_paise or 0)/100:,.0f} daily cap.",
                amount_paise=b["total"], occurred_on=day, account_id=account_id,
                evidence={"count": b["count"]},
                fp_parts=[account_id, day],
            ))
    return violations, applicable


def _eval_velocity(rule, txns, user_id):
    violations = []
    buckets = defaultdict(int)
    for t in txns:
        if t.txn_date:
            buckets[(t.account_id, t.txn_date)] += 1
    applicable = len(buckets)
    limit = rule.threshold_count or 0
    for (account_id, day), count in buckets.items():
        if limit and count > limit:
            violations.append(_violation(
                rule, user_id,
                f"{count} transactions on {day}, above the limit of {limit} per day.",
                occurred_on=day, account_id=account_id,
                evidence={"count": count, "limit": limit},
                fp_parts=[account_id, day],
            ))
    return violations, applicable


def _eval_min_balance(rule, txns, user_id):
    """Lowest closing balance seen per account per day."""
    violations = []
    lowest = {}
    for t in txns:
        if t.balance_paise is None or not t.txn_date:
            continue
        key = (t.account_id, t.txn_date)
        if key not in lowest or t.balance_paise < lowest[key][0]:
            lowest[key] = (t.balance_paise, t)
    applicable = len(lowest)
    for (account_id, day), (balance, t) in lowest.items():
        if balance < (rule.threshold_paise or 0):
            violations.append(_violation(
                rule, user_id,
                f"Balance fell to {balance/100:,.2f} on {day}, below the "
                f"{(rule.threshold_paise or 0)/100:,.0f} minimum.",
                txn=t, amount_paise=balance, occurred_on=day, account_id=account_id,
                fp_parts=[account_id, day],
            ))
    return violations, applicable


def _eval_weekend_payment(rule, txns, user_id):
    violations, applicable = [], 0
    for t in txns:
        if not t.txn_date or not (t.debit_paise or 0):
            continue
        applicable += 1
        if t.txn_date.weekday() >= 5:  # Saturday=5, Sunday=6
            day_name = t.txn_date.strftime("%A")
            violations.append(_violation(
                rule, user_id,
                f"Payment of {(t.debit_paise or 0)/100:,.2f} posted on {day_name}, "
                "outside normal approval hours.",
                txn=t, evidence={"weekday": day_name},
            ))
    return violations, applicable


_EVALUATORS = {
    RULE_CASH_RECEIPT_LIMIT: lambda r, t, u: _eval_cash_single(r, t, u, r.direction_scope),
    RULE_CASH_PAYMENT_LIMIT: lambda r, t, u: _eval_cash_single(r, t, u, "debit"),
    RULE_CASH_DAILY_AGGREGATE: _eval_cash_daily_aggregate,
    RULE_AMOUNT_LIMIT: _eval_amount_limit,
    RULE_DAILY_OUTFLOW_LIMIT: _eval_daily_outflow,
    RULE_VELOCITY_LIMIT: _eval_velocity,
    RULE_MIN_BALANCE: _eval_min_balance,
    RULE_WEEKEND_PAYMENT: _eval_weekend_payment,
}


def evaluate_policies(db, user_id, account_id=None, date_from=None, date_to=None):
    """Run every active rule and upsert violations. Returns a per-rule report."""
    rules = db.query(PolicyRule).filter(
        PolicyRule.user_id == user_id, PolicyRule.is_active.is_(True)
    ).all()
    if not rules:
        return {"rules_evaluated": 0, "violations": 0, "per_rule": []}

    q = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.superseded_by_id.is_(None),
    )
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    if date_from:
        q = q.filter(Transaction.txn_date >= date_from)
    if date_to:
        q = q.filter(Transaction.txn_date <= date_to)
    txns = q.all()

    all_violations, per_rule = [], []
    for rule in rules:
        evaluator = _EVALUATORS.get(rule.rule_type)
        if evaluator is None:
            logger.warning("[Policy] no evaluator for rule_type=%s (%s)", rule.rule_type, rule.code)
            continue
        try:
            violations, applicable = evaluator(rule, txns, user_id)
        except Exception as exc:
            logger.exception("[Policy] rule %s failed: %s", rule.code, exc)
            continue

        all_violations.extend(violations)
        rule.last_applicable = applicable
        rule.last_violations = len(violations)
        rule.last_evaluated_at = datetime.datetime.now(datetime.timezone.utc)
        per_rule.append({
            "rule_id": str(rule.id),
            "code": rule.code,
            "name": rule.name,
            "category": rule.category,
            "severity": rule.severity,
            "statute_ref": rule.statute_ref,
            "applicable": applicable,
            "violations": len(violations),
            "pass_pct": round(100.0 * (applicable - len(violations)) / applicable, 1) if applicable else None,
        })

    if all_violations:
        stmt = pg_insert(PolicyViolation.__table__).values(all_violations)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_policy_violation_fingerprint",
            set_={
                "detail": stmt.excluded.detail,
                "severity": stmt.excluded.severity,
                "amount_paise": stmt.excluded.amount_paise,
                "evidence": stmt.excluded.evidence,
            },
        )
        db.execute(stmt)

    # Retire what this evaluation no longer reproduces — same reasoning as the
    # anomaly engine. An aggregate violation ("cash receipts over 2 lakh on this
    # day") names no transaction, so nothing removed it when the day's rows were
    # corrected, and it stayed open against data that no longer existed.
    _retire_superseded(db, user_id, {v["fingerprint"] for v in all_violations},
                       account_id, date_from, date_to)
    db.commit()

    return {
        "rules_evaluated": len(per_rule),
        "violations": len(all_violations),
        "transactions_scanned": len(txns),
        "per_rule": sorted(per_rule, key=lambda r: -r["violations"]),
    }


def _retire_superseded(db, user_id, live_fingerprints, account_id, date_from, date_to):
    """Mark violations this evaluation did not reproduce as resolved.

    Resolved rather than deleted, and scoped to what was actually evaluated —
    see the matching note in `anomaly_engine._retire_superseded`. A user's own
    `waived` or `false_positive` judgement is never overwritten by a scan.
    """
    q = db.query(PolicyViolation).filter(
        PolicyViolation.user_id == user_id,
        PolicyViolation.status.in_(("open", "acknowledged")),
    )
    if account_id:
        q = q.filter(PolicyViolation.account_id == account_id)
    if date_from:
        q = q.filter(PolicyViolation.occurred_on >= date_from)
    if date_to:
        q = q.filter(PolicyViolation.occurred_on <= date_to)
    if live_fingerprints:
        q = q.filter(PolicyViolation.fingerprint.notin_(tuple(live_fingerprints)))

    q.update(
        {
            PolicyViolation.status: "resolved",
            PolicyViolation.resolved_at: datetime.datetime.now(datetime.timezone.utc),
        },
        synchronize_session=False,
    )


def compliance_summary(db, user_id, account_ids=None):
    """Headline compliance percentage, read from the last scan's cached stats.

    This is a pure read. It used to call evaluate_policies(), which meant every
    dashboard load re-ran every rule over every transaction and wrote violation
    rows — a write side effect on a GET, and O(rules x transactions) per page
    view. The numbers now come from what the last scan stored, and go stale on
    purpose until someone re-scans.

    The denominator is *checks applied*, not transactions: one transaction can be
    in scope for several rules, and each of those is a separate pass or fail.

    `account_ids` narrows the parts of this that CAN be narrowed, and the split
    matters because it is reported rather than hidden. `open_violations` is a
    live count of violation rows, so it filters exactly. The percentage and the
    per-rule figures are not live — they are aggregates the last scan wrote onto
    each PolicyRule row, computed across the whole ledger, and there is no
    account breakdown stored to filter them by. Recomputing them per selection
    would mean re-running every rule over every transaction on a GET, which is
    the write-on-read this function was written to remove.

    So when a scope is applied the response says `pct_is_scoped: False`, and the
    caller can label the figure as ledger-wide. Silently returning a whole-ledger
    percentage next to a filtered violation count would be the more comfortable
    option and the dishonest one.
    """
    rules = db.query(PolicyRule).filter(
        PolicyRule.user_id == user_id, PolicyRule.is_active.is_(True)
    ).all()

    per_rule = [{
        "rule_id": str(r.id),
        "code": r.code,
        "name": r.name,
        "category": r.category,
        "severity": r.severity,
        "statute_ref": r.statute_ref,
        "applicable": r.last_applicable or 0,
        "violations": r.last_violations or 0,
        "pass_pct": round(
            100.0 * ((r.last_applicable or 0) - (r.last_violations or 0)) / r.last_applicable, 1
        ) if r.last_applicable else None,
    } for r in rules]

    scored = [r for r in per_rule if r["applicable"]]
    open_q = db.query(PolicyViolation).filter(
        PolicyViolation.user_id == user_id,
        PolicyViolation.status == "open",
    )
    if account_ids is not None:
        open_q = open_q.filter(PolicyViolation.account_id.in_(account_ids))
    open_count = open_q.count()

    if not scored:
        # No scan has run, or no rule applied to anything. Reporting 100% here
        # would be the same lie the hardcoded value used to tell.
        return {
            "compliance_pct": None,
            "open_violations": open_count,
            "rules_active": len(rules),
            "never_scanned": not any(r.last_evaluated_at for r in rules),
            "pct_is_scoped": account_ids is None,
            "per_rule": sorted(per_rule, key=lambda r: -r["violations"]),
        }

    total_applicable = sum(r["applicable"] for r in scored)
    total_violations = sum(r["violations"] for r in scored)
    last_run = max((r.last_evaluated_at for r in rules if r.last_evaluated_at), default=None)

    return {
        "compliance_pct": round(100.0 * max(0, total_applicable - total_violations) / total_applicable, 1) if total_applicable else None,
        "open_violations": open_count,
        "evaluated_checks": total_applicable,
        "failed_checks": total_violations,
        "rules_active": len(rules),
        "never_scanned": False,
        "pct_is_scoped": account_ids is None,
        "last_scan_at": last_run.isoformat() if last_run else None,
        "per_rule": sorted(per_rule, key=lambda r: -r["violations"]),
    }
