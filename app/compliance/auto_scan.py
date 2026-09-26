"""Run anomaly detection and policy evaluation as part of ingestion.

Before this existed, an upload left the dashboard's Anomalies and Policy
Compliance panels showing the PREVIOUS statement's numbers until the user found
and pressed Re-scan. That is worse than showing nothing: the figures looked
current, were stale, and nothing on screen said so.

The scan runs here, in the same background task that writes the ledger, so by
the time the upload reports COMPLETED the compliance picture already matches
what was just ingested.

Deliberately the SAME entry points the /compliance/analyze endpoint calls. If
the button and the upload ran different code they would drift, and the user
would get one answer on upload and a different one on Re-scan.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.compliance.anomaly_engine import detect_anomalies
from app.compliance.policy_engine import evaluate_policies
from app.compliance.rules_seed import seed_policy_rules
from app.models.compliance import AnomalyFinding, PolicyRule, PolicyViolation
from app.models.transaction import Transaction
from app.services.cache import cache

logger = logging.getLogger(__name__)

#: Key suffix for "this user's ledger has already been scanned in this exact
#: state". Built here rather than at each call site so the dashboard's gated
#: read and the Re-scan button's forced run cannot disagree about the key.
_SCAN_STATE_PART = "compliance-scan-state"

#: Deliberately short. The fingerprint is what makes the gate correct — a stale
#: marker cannot survive a change to the data, because the data is in the key's
#: value. The TTL is the second line: it bounds how long a scan input this
#: fingerprint does NOT cover could go unnoticed, at the cost of one extra scan
#: a minute per active user.
_SCAN_STATE_TTL_SECONDS = 60


def _scan_state_key(user_id) -> str:
    return cache.user_key(user_id, _SCAN_STATE_PART)


def ensure_rules(db: Session, user_id) -> None:
    """Seed the statutory defaults the first time this user is scanned."""
    exists = db.query(PolicyRule).filter(PolicyRule.user_id == user_id).first()
    if not exists:
        seed_policy_rules(db, user_id)


def scan_fingerprint(db: Session, user_id) -> str:
    """Everything a full scan's output depends on, as one comparable digest.

    Four cheap aggregates instead of the ~290ms scan they guard. The parts are
    chosen so that anything capable of changing a finding changes this digest:

    * transactions — count, newest write and newest txn_date. The scan reads
      nothing else from the ledger, and a row added, edited or deleted moves at
      least one of the three.
    * rule definitions — the thresholds and filters `evaluate_policies` applies.
      Deliberately NOT `PolicyRule.updated_at`: the scan writes its own
      `last_evaluated_at` back to these rows, so `updated_at` bumps on every run
      and the fingerprint would never settle — the gate would never hit.
    * findings and violations — count, newest, and how many are still open. This
      is what makes "findings absent" a miss rather than a hit, and it also
      keeps a manual resolve behaving exactly as it does today, where the next
      dashboard read re-scans over it.
    * today's date — `_detect_date_inconsistencies` compares txn_date against
      today, so the same ledger can legitimately answer differently tomorrow.

    Hashed rather than returned raw because the cache stores JSON and this tuple
    holds UUIDs, dates and timestamps. `repr` of those types is stable across
    processes, which it has to be: with Redis the marker one worker writes is
    read by another.
    """
    txn_state = db.query(
        func.count(Transaction.id),
        func.max(Transaction.updated_at),
        func.max(Transaction.created_at),
        func.max(Transaction.txn_date),
    ).filter(
        Transaction.user_id == user_id,
        Transaction.superseded_by_id.is_(None),
    ).one()

    rule_state = tuple(db.query(
        PolicyRule.id, PolicyRule.rule_type, PolicyRule.threshold_paise,
        PolicyRule.threshold_count, PolicyRule.window_days,
        PolicyRule.narration_filter, PolicyRule.direction_scope,
        PolicyRule.severity, PolicyRule.is_active,
    ).filter(PolicyRule.user_id == user_id).order_by(PolicyRule.id).all())

    finding_state = db.query(
        func.count(AnomalyFinding.id),
        func.max(AnomalyFinding.created_at),
        func.max(AnomalyFinding.resolved_at),
        func.count(AnomalyFinding.id).filter(AnomalyFinding.status == "open"),
    ).filter(AnomalyFinding.user_id == user_id).one()

    violation_state = db.query(
        func.count(PolicyViolation.id),
        func.max(PolicyViolation.created_at),
        func.max(PolicyViolation.resolved_at),
        func.count(PolicyViolation.id).filter(PolicyViolation.status == "open"),
    ).filter(PolicyViolation.user_id == user_id).one()

    state = (tuple(txn_state), rule_state, tuple(finding_state),
             tuple(violation_state), datetime.date.today())
    return hashlib.sha256(repr(state).encode()).hexdigest()


def run_scan(
    db: Session,
    user_id,
    account_id=None,
    date_from=None,
    date_to=None,
) -> Dict[str, Any]:
    """Detect anomalies and evaluate policies. Returns a summary."""
    ensure_rules(db, user_id)
    findings = detect_anomalies(db, user_id, account_id, date_from, date_to)
    policy = evaluate_policies(db, user_id, account_id, date_from, date_to)

    # An unscoped scan has just brought the whole ledger up to date, so record
    # the state it now reflects and let `run_scan_if_data_changed` skip until
    # something moves. A scoped scan (one account, or a date window) has NOT
    # covered everything, so it must not claim it has — the Re-scan button and
    # the post-upload scan both land here, and only the full ones may vouch.
    if account_id is None and date_from is None and date_to is None:
        cache.set(_scan_state_key(user_id), scan_fingerprint(db, user_id),
                  _SCAN_STATE_TTL_SECONDS)

    return {"anomalies_found": len(findings), "policy": policy}


def invalidate_scan_marker(user_id) -> None:
    """Force the next gated read to re-scan, whatever the data says."""
    cache.invalidate_user(user_id)


def run_scan_safely(
    db: Session,
    user_id,
    account_id=None,
) -> Optional[Dict[str, Any]]:
    """Scan, but never let a scan failure fail the ingestion.

    The transactions are already committed by the time this runs. If a detector
    raises, the correct outcome is "the statement is in, the compliance panels
    are not yet updated" — not "the upload failed and the user re-uploads,
    creating duplicates". The error is logged loudly and reported in the upload
    summary so the staleness is visible rather than silent.
    """
    if not user_id:
        return None
    try:
        result = run_scan(db, user_id, account_id=account_id)
        db.commit()
        logger.info(
            f"[Auto Scan] user={user_id} account={account_id}: "
            f"{result['anomalies_found']} anomaly finding(s), "
            f"{result['policy'].get('violations', 'n/a')} policy violation(s)"
        )
        return result
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        db.rollback()
        logger.exception(
            f"[Auto Scan] FAILED for user={user_id} account={account_id}. "
            f"Transactions were ingested; compliance panels will stay stale "
            f"until Re-scan is pressed. Error: {exc}"
        )
        return {"error": str(exc), "anomalies_found": None, "policy": None}


def run_scan_if_data_changed(db: Session, user_id) -> Optional[Dict[str, Any]]:
    """Scan only when this user's ledger is not already scanned in this state.

    The dashboard used to call `run_scan_safely` on every single GET, so opening
    a page re-derived every anomaly and re-evaluated every rule over the whole
    ledger — ~290ms of the summary's ~245ms-and-up, on a read that changes
    nothing. The scan is idempotent: run twice over unchanged data it produces
    byte-identical findings. So the second run's only effect is the delay.

    This keeps the counts exactly as fresh as before, because the gate opens on
    any change to the inputs (see `scan_fingerprint`) rather than on a timer.
    What it does not do is force a full re-derivation to prove nothing moved.

    The explicit Re-scan button (`POST /compliance/analyze`) does not come
    through here — it calls `run_scan` directly and always scans in full.
    """
    if not user_id:
        return None
    try:
        fingerprint = scan_fingerprint(db, user_id)
    except Exception as exc:  # noqa: BLE001 - the gate must never break the read
        db.rollback()
        logger.warning(
            "[Auto Scan] could not fingerprint user=%s (%s); scanning as before.",
            user_id, exc,
        )
        return run_scan_safely(db, user_id)

    # The marker's VALUE is the fingerprint, so a hit means "already scanned in
    # exactly this state". Anything else — absent, expired, or recorded against
    # different data — falls through to a real scan.
    if cache.get(_scan_state_key(user_id)) == fingerprint:
        return None

    return run_scan_safely(db, user_id)
