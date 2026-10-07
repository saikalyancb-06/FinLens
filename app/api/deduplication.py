import logging
from typing import List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.user import User
from app.models.account import Account
from app.models.duplicate_match import DuplicateMatch, MatchStatus, DuplicateTier
from app.api.review_rows import bank_row
from app.models.transaction import Transaction
from app.services.deduplication_engine import DeduplicationEngine
from app.utils.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/deduplication", tags=["Cross-Source Deduplication"])


class RunDeduplicationRequest(BaseModel):
    account_id: UUID


@router.post("/run")
def run_deduplication_matcher(
    payload: RunDeduplicationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Triggers the cross-source deduplication matcher for an account."""
    account = db.query(Account).filter(
        Account.id == payload.account_id,
        Account.user_id == current_user.id
    ).first()

    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    engine = DeduplicationEngine(db=db, user_id=current_user.id, account_id=account.id)
    summary = engine.run_deduplication()
    return summary


@router.get("/matches")
def list_duplicate_matches(
    account_id: Optional[UUID] = Query(None),
    status_filter: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Lists duplicate matches for the current user."""
    query = db.query(DuplicateMatch).filter(DuplicateMatch.user_id == current_user.id)

    if status_filter:
        query = query.filter(DuplicateMatch.status == status_filter)

    matches = query.order_by(DuplicateMatch.created_at.desc()).all()

    results = []
    for m in matches:
        dup_tx = db.query(Transaction).filter(Transaction.id == m.duplicate_txn_id).first()
        kept_tx = db.query(Transaction).filter(Transaction.id == m.kept_txn_id).first()

        if account_id:
            if not dup_tx or dup_tx.account_id != account_id:
                continue

        results.append({
            "id": m.id,
            "duplicate_txn_id": m.duplicate_txn_id,
            "kept_txn_id": m.kept_txn_id,
            "tier": m.tier.value if hasattr(m.tier, 'value') else str(m.tier),
            "confidence": m.confidence,
            "status": m.status.value if hasattr(m.status, 'value') else str(m.status),
            "created_at": m.created_at,
            "duplicate_txn": {
                "id": dup_tx.id,
                "source_type": dup_tx.source_type.value if hasattr(dup_tx.source_type, 'value') else str(dup_tx.source_type),
                "txn_date": dup_tx.txn_date,
                "amount_paise": dup_tx.debit_paise or dup_tx.credit_paise,
                "direction": dup_tx.direction.value if hasattr(dup_tx.direction, 'value') else str(dup_tx.direction),
                "narration": dup_tx.narration_clean or dup_tx.narration_raw,
                "reference_no": dup_tx.reference_no,
                "superseded_by_id": dup_tx.superseded_by_id
            } if dup_tx else None,
            "kept_txn": {
                "id": kept_tx.id,
                "source_type": kept_tx.source_type.value if hasattr(kept_tx.source_type, 'value') else str(kept_tx.source_type),
                "txn_date": kept_tx.txn_date,
                "amount_paise": kept_tx.debit_paise or kept_tx.credit_paise,
                "direction": kept_tx.direction.value if hasattr(kept_tx.direction, 'value') else str(kept_tx.direction),
                "narration": kept_tx.narration_clean or kept_tx.narration_raw,
                "reference_no": kept_tx.reference_no
            } if kept_tx else None,
            # The same row shape the Manual Review tab renders, so the
            # duplicates tab shows each transaction exactly as that tab does.
            "rows": [r for r in (
                dict(bank_row(dup_tx), role="duplicate") if dup_tx else None,
                dict(bank_row(kept_tx), role="kept") if kept_tx else None,
            ) if r],
        })

    return results


@router.post("/matches/{match_id}/confirm")
def confirm_duplicate_match(
    match_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Confirms a pending duplicate match, marking duplicate_txn superseded by kept_txn."""
    match_rec = db.query(DuplicateMatch).filter(
        DuplicateMatch.id == match_id,
        DuplicateMatch.user_id == current_user.id
    ).first()

    if not match_rec:
        raise HTTPException(status_code=404, detail="Duplicate match record not found")

    dup_tx = db.query(Transaction).filter(Transaction.id == match_rec.duplicate_txn_id).first()
    if not dup_tx:
        raise HTTPException(status_code=404, detail="Duplicate transaction not found")

    match_rec.status = MatchStatus.CONFIRMED
    dup_tx.superseded_by_id = match_rec.kept_txn_id

    db.commit()
    return {"status": "success", "message": f"Duplicate match {match_id} confirmed"}


@router.post("/matches/{match_id}/reject")
def reject_duplicate_match(
    match_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Rejects a duplicate match, restoring duplicate_txn superseded_by_id to NULL."""
    match_rec = db.query(DuplicateMatch).filter(
        DuplicateMatch.id == match_id,
        DuplicateMatch.user_id == current_user.id
    ).first()

    if not match_rec:
        raise HTTPException(status_code=404, detail="Duplicate match record not found")

    dup_tx = db.query(Transaction).filter(Transaction.id == match_rec.duplicate_txn_id).first()
    if dup_tx:
        dup_tx.superseded_by_id = None

    match_rec.status = MatchStatus.REJECTED

    db.commit()
    return {"status": "success", "message": f"Duplicate match {match_id} rejected"}
