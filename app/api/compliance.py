"""Anomaly and policy-compliance API.

Everything here is scoped to the authenticated user. Rules are seeded lazily on
first read, so a new account gets the statutory defaults without a setup step.
"""
import datetime
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.scoping import accounts_in_scope
from app.compliance.anomaly_engine import ANOMALY_TYPES, detect_anomalies
from app.compliance.auto_scan import ensure_rules, run_scan
from app.compliance.policy_engine import compliance_summary, evaluate_policies
from app.compliance.rules_seed import seed_policy_rules
from app.database.session import get_db
from app.models.compliance import (
    AnomalyFinding, PolicyRule, PolicyViolation,
    ANOMALY_STATUSES, RULE_TYPES, SEVERITIES, VIOLATION_STATUSES,
)
from app.models.user import User
from app.utils.security import get_current_user

router = APIRouter(prefix="/compliance", tags=["Compliance"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RuleCreate(BaseModel):
    code: Optional[str] = Field(None, max_length=60)
    name: str = Field(..., max_length=200)
    description: Optional[str] = None
    category: str = "internal"
    statute_ref: Optional[str] = Field(None, max_length=120)
    rule_type: str
    threshold_paise: Optional[int] = None
    threshold_count: Optional[int] = None
    window_days: Optional[int] = None
    narration_filter: Optional[str] = Field(None, max_length=300)
    direction_scope: str = "any"
    severity: str = "medium"
    is_active: bool = True


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    threshold_paise: Optional[int] = None
    threshold_count: Optional[int] = None
    window_days: Optional[int] = None
    narration_filter: Optional[str] = None
    direction_scope: Optional[str] = None
    severity: Optional[str] = None
    is_active: Optional[bool] = None


class StatusUpdate(BaseModel):
    status: str


def _rule_json(r: PolicyRule) -> dict:
    return {
        "id": str(r.id),
        "code": r.code,
        "name": r.name,
        "description": r.description,
        "category": r.category,
        "statute_ref": r.statute_ref,
        "rule_type": r.rule_type,
        "threshold_paise": r.threshold_paise,
        "threshold_rupees": (r.threshold_paise / 100.0) if r.threshold_paise else None,
        "threshold_count": r.threshold_count,
        "window_days": r.window_days,
        "narration_filter": r.narration_filter,
        "direction_scope": r.direction_scope,
        "severity": r.severity,
        "is_system": r.is_system,
        "is_active": r.is_active,
    }


def _ensure_rules(db: Session, user_id) -> None:
    """Seed the statutory defaults the first time a user looks at compliance.

    Delegates to the shared helper so the Re-scan button and the automatic scan
    that runs on upload seed from exactly one place.
    """
    ensure_rules(db, user_id)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@router.get("/rules", summary="List policy rules")
def list_rules(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _ensure_rules(db, current_user.id)
    rules = db.query(PolicyRule).filter(
        PolicyRule.user_id == current_user.id
    ).order_by(PolicyRule.category.desc(), PolicyRule.name).all()
    return {
        "rules": [_rule_json(r) for r in rules],
        "rule_types": list(RULE_TYPES),
        "severities": list(SEVERITIES),
    }


@router.post("/rules", status_code=status.HTTP_201_CREATED, summary="Create a policy rule")
def create_rule(payload: RuleCreate, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    if payload.rule_type not in RULE_TYPES:
        raise HTTPException(400, f"rule_type must be one of {list(RULE_TYPES)}")
    if payload.severity not in SEVERITIES:
        raise HTTPException(400, f"severity must be one of {list(SEVERITIES)}")
    if payload.direction_scope not in ("any", "debit", "credit"):
        raise HTTPException(400, "direction_scope must be any, debit or credit")

    code = (payload.code or payload.name).upper().replace(" ", "_")[:60]
    if db.query(PolicyRule).filter(
        PolicyRule.user_id == current_user.id, PolicyRule.code == code
    ).first():
        raise HTTPException(400, f"A rule with code '{code}' already exists")

    rule = PolicyRule(
        user_id=current_user.id, code=code, is_system=False,
        **payload.model_dump(exclude={"code"}),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return _rule_json(rule)


@router.patch("/rules/{rule_id}", summary="Update a policy rule")
def update_rule(rule_id: uuid.UUID, payload: RuleUpdate, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    rule = db.query(PolicyRule).filter(
        PolicyRule.id == rule_id, PolicyRule.user_id == current_user.id
    ).first()
    if not rule:
        raise HTTPException(404, "Rule not found")

    updates = payload.model_dump(exclude_unset=True)
    if "severity" in updates and updates["severity"] not in SEVERITIES:
        raise HTTPException(400, f"severity must be one of {list(SEVERITIES)}")
    if "direction_scope" in updates and updates["direction_scope"] not in ("any", "debit", "credit"):
        raise HTTPException(400, "direction_scope must be any, debit or credit")

    for key, value in updates.items():
        setattr(rule, key, value)
    db.commit()
    db.refresh(rule)
    return _rule_json(rule)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a compliance rule for current user")
def delete_rule(rule_id: uuid.UUID, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    rule = db.query(PolicyRule).filter(
        PolicyRule.id == rule_id, PolicyRule.user_id == current_user.id
    ).first()
    if not rule:
        raise HTTPException(404, "Rule not found")
    
    # Clean up any violations under this rule for the current user
    db.query(PolicyViolation).filter(
        PolicyViolation.rule_id == rule_id,
        PolicyViolation.user_id == current_user.id
    ).delete(synchronize_session=False)

    db.delete(rule)
    db.commit()


@router.post("/rules/reseed", summary="Restore the statutory default rules")
def reseed_rules(overwrite: bool = Query(False, description="reset thresholds on existing system rules"),
                 db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    created = seed_policy_rules(db, current_user.id, overwrite=overwrite)
    return {"created": len(created), "overwritten": overwrite}


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@router.post("/analyze", summary="Run anomaly detection and policy evaluation")
def analyze(
    account_id: Optional[uuid.UUID] = Query(None),
    date_from: Optional[datetime.date] = Query(None),
    date_to: Optional[datetime.date] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Same code path the upload runs automatically, so the button and the
    # upload can never disagree about what the compliance picture is.
    return run_scan(db, current_user.id, account_id, date_from, date_to)


@router.get("/overview", summary="Headline anomaly and compliance figures")
def overview(
    account_id: Optional[uuid.UUID] = Query(None),
    entity_id: Optional[uuid.UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Headline figures, narrowed by the same filter bar as the rest of the page.

    These panels sit on the dashboard underneath an Entity and Bank Account
    selector and used to ignore both, so choosing one entity narrowed every
    figure on the screen except these three — which carried on reporting the
    whole ledger beside numbers that no longer described it.

    Findings that name no account are excluded once a scope is set. A ledger-wide
    finding cannot be attributed to the account you have selected, and counting
    it here would put it back into a total the user has just narrowed.
    """
    _ensure_rules(db, current_user.id)
    scope_ids = accounts_in_scope(db, current_user, account_id, entity_id)

    # If transactions exist but no findings have been generated yet, scan lazily
    from app.models.transaction import Transaction
    from app.compliance.auto_scan import run_scan_safely
    has_txns = db.query(Transaction.id).filter(
        Transaction.user_id == current_user.id,
        Transaction.superseded_by_id.is_(None)
    ).first() is not None
    has_findings = db.query(AnomalyFinding.id).filter(AnomalyFinding.user_id == current_user.id).first() is not None
    if has_txns and not has_findings:
        run_scan_safely(db, current_user.id)

    def _scoped(q):
        return q if scope_ids is None else q.filter(AnomalyFinding.account_id.in_(scope_ids))

    by_severity = dict(
        _scoped(db.query(AnomalyFinding.severity, func.count())
                .filter(AnomalyFinding.user_id == current_user.id,
                        AnomalyFinding.status == "open"))
        .group_by(AnomalyFinding.severity).all()
    )
    by_type = [
        {"type": t, "label": ANOMALY_TYPES.get(t, t), "count": c}
        for t, c in _scoped(db.query(AnomalyFinding.anomaly_type, func.count())
                            .filter(AnomalyFinding.user_id == current_user.id,
                                    AnomalyFinding.status == "open"))
        .group_by(AnomalyFinding.anomaly_type).order_by(func.count().desc()).all()
    ]

    summary = compliance_summary(db, current_user.id, account_ids=scope_ids)

    return {
        "anomalies": {
            "open": sum(by_severity.values()),
            "critical": by_severity.get("critical", 0),
            "high": by_severity.get("high", 0),
            "medium": by_severity.get("medium", 0),
            "low": by_severity.get("low", 0),
            "by_type": by_type,
        },
        "compliance": summary,
    }


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@router.get("/anomalies", summary="List anomaly findings")
def list_anomalies(
    status_filter: str = Query("open", alias="status"),
    anomaly_type: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    account_id: Optional[uuid.UUID] = Query(None),
    entity_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(AnomalyFinding).filter(AnomalyFinding.user_id == current_user.id)
    scope_ids = accounts_in_scope(db, current_user, account_id, entity_id)
    if scope_ids is not None:
        q = q.filter(AnomalyFinding.account_id.in_(scope_ids))
    if status_filter and status_filter != "all":
        q = q.filter(AnomalyFinding.status == status_filter)
    if anomaly_type:
        q = q.filter(AnomalyFinding.anomaly_type == anomaly_type)
    if severity:
        q = q.filter(AnomalyFinding.severity == severity)

    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    rows = q.limit(limit * 3).all()
    rows.sort(key=lambda a: (severity_rank.get(a.severity, 9),
                             -(a.amount_paise or 0)))

    # Deduplicate anomaly findings
    seen_anomalies = set()
    deduped_anomalies = []
    for a in rows:
        key = (a.anomaly_type, str(a.transaction_id) if a.transaction_id else None, str(a.occurred_on), a.amount_paise, a.title)
        if key in seen_anomalies:
            continue
        seen_anomalies.add(key)
        deduped_anomalies.append(a)
        if len(deduped_anomalies) >= limit:
            break

    return [{
        "id": str(a.id),
        "type": a.anomaly_type,
        "type_label": ANOMALY_TYPES.get(a.anomaly_type, a.anomaly_type),
        "severity": a.severity,
        "status": a.status,
        "title": a.title,
        "detail": a.detail,
        "amount": (a.amount_paise / 100.0) if a.amount_paise is not None else None,
        "occurred_on": a.occurred_on.isoformat() if a.occurred_on else None,
        "transaction_id": str(a.transaction_id) if a.transaction_id else None,
        "evidence": a.evidence,
    } for a in deduped_anomalies]


@router.patch("/anomalies/{anomaly_id}", summary="Update an anomaly's status")
def update_anomaly(anomaly_id: uuid.UUID, payload: StatusUpdate, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    if payload.status not in ANOMALY_STATUSES:
        raise HTTPException(400, f"status must be one of {list(ANOMALY_STATUSES)}")
    row = db.query(AnomalyFinding).filter(
        AnomalyFinding.id == anomaly_id, AnomalyFinding.user_id == current_user.id
    ).first()
    if not row:
        raise HTTPException(404, "Anomaly not found")
    row.status = payload.status
    row.resolved_at = datetime.datetime.utcnow() if payload.status in ("resolved", "false_positive") else None
    db.commit()
    return {"id": str(row.id), "status": row.status}


@router.get("/violations", summary="List policy violations")
def list_violations(
    status_filter: str = Query("open", alias="status"),
    rule_id: Optional[uuid.UUID] = Query(None),
    account_id: Optional[uuid.UUID] = Query(None),
    entity_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = (db.query(PolicyViolation, PolicyRule)
         .join(PolicyRule, PolicyViolation.rule_id == PolicyRule.id)
         .filter(PolicyViolation.user_id == current_user.id))
    scope_ids = accounts_in_scope(db, current_user, account_id, entity_id)
    if scope_ids is not None:
        q = q.filter(PolicyViolation.account_id.in_(scope_ids))
    if status_filter and status_filter != "all":
        q = q.filter(PolicyViolation.status == status_filter)
    if rule_id:
        q = q.filter(PolicyViolation.rule_id == rule_id)

    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    rows = q.limit(limit * 3).all()
    rows.sort(key=lambda pair: (severity_rank.get(pair[0].severity, 9),
                                -(pair[0].amount_paise or 0)))

    # Deduplicate policy violations
    seen_violations = set()
    deduped_violations = []
    for v, r in rows:
        key = (str(r.id), str(v.transaction_id) if v.transaction_id else None, str(v.occurred_on), v.amount_paise, v.detail)
        if key in seen_violations:
            continue
        seen_violations.add(key)
        deduped_violations.append((v, r))
        if len(deduped_violations) >= limit:
            break

    return [{
        "id": str(v.id),
        "rule_id": str(r.id),
        "rule_code": r.code,
        "rule_name": r.name,
        "category": r.category,
        "statute_ref": r.statute_ref,
        "severity": v.severity,
        "status": v.status,
        "detail": v.detail,
        "amount": (v.amount_paise / 100.0) if v.amount_paise is not None else None,
        "occurred_on": v.occurred_on.isoformat() if v.occurred_on else None,
        "transaction_id": str(v.transaction_id) if v.transaction_id else None,
        "evidence": v.evidence,
    } for v, r in deduped_violations]


@router.patch("/violations/{violation_id}", summary="Update a violation's status")
def update_violation(violation_id: uuid.UUID, payload: StatusUpdate, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    if payload.status not in VIOLATION_STATUSES:
        raise HTTPException(400, f"status must be one of {list(VIOLATION_STATUSES)}")
    row = db.query(PolicyViolation).filter(
        PolicyViolation.id == violation_id, PolicyViolation.user_id == current_user.id
    ).first()
    if not row:
        raise HTTPException(404, "Violation not found")
    row.status = payload.status
    row.resolved_at = datetime.datetime.utcnow() if payload.status in ("resolved", "waived") else None
    db.commit()
    return {"id": str(row.id), "status": row.status}
