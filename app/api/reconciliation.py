import logging
import os
import hashlib
from datetime import datetime, date

logger = logging.getLogger(__name__)
import uuid
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, status, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.user import User
from app.models.account import Account
from app.models.reconciliation import (
    ImportTemplate, ImportBatch, BookEntry, ReconciliationRun,
    ReconciliationMatch, ReconciliationItem, MatchStatusEnum, RunVerdictEnum
)
from app.services.books_importer import BooksImporterService, parse_amount_to_paise, compute_row_hash
from app.services.reconciliation_engine import ReconciliationMatchingEngine
from app.utils.security import get_current_user

router = APIRouter(prefix="/v1/reconciliation", tags=["Bank Reconciliation"])


class PreviewResponse(BaseModel):
    columns: List[str]
    detected_mapping: dict
    column_samples: Optional[dict] = {}
    is_bank_statement: Optional[bool] = False
    sample_rows: List[dict]


class ConfirmMappingRequest(BaseModel):
    account_id: Optional[UUID] = None
    column_mapping: dict
    date_format: Optional[str] = "%Y-%m-%d"
    template_name: Optional[str] = None
    save_as_template: bool = False
    ledger_convention: Optional[str] = "DEBIT_IN"  # "DEBIT_IN" (Tally style: Debit->In, Credit->Out) or "STANDARD" (Normal CSV: Debit->Out, Credit->In)
    rows: List[dict]




class CreateRunRequest(BaseModel):
    account_id: Optional[UUID] = None
    period_from: date
    period_to: date
    import_batch_id: Optional[UUID] = None
    force: bool = False



class ClassifyItemRequest(BaseModel):
    brs_category: str


@router.post("/imports", response_model=PreviewResponse)
def upload_books_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    """Part 1: Step 1 & 2 — Upload books file (.xlsx, .xls, .csv), parse headers and 20 sample rows."""
    contents = file.file.read()
    filename = file.filename or "ledger.csv"
    try:
        preview = BooksImporterService.preview_file(contents, filename)
        return preview
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse books file: {str(e)}")


@router.post("/imports/confirm")
def confirm_books_import(
    payload: ConfirmMappingRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Part 1: Step 3 & 4 — Confirm mapping, ingest all rows with money_in_paise / money_out_paise."""
    account = None
    if getattr(payload, "account_id", None):
        account = db.query(Account).filter(
            Account.id == payload.account_id,
            Account.user_id == current_user.id
        ).first()
        if not account:
            raise HTTPException(status_code=404, detail="Specified bank account not found or does not belong to user")
    else:
        raise HTTPException(status_code=400, detail="account_id is required for books import")




    template_id = None
    if payload.save_as_template and payload.template_name:
        template = ImportTemplate(
            user_id=current_user.id,
            name=payload.template_name,
            column_mapping_json=payload.column_mapping,
            date_format=payload.date_format
        )
        db.add(template)
        db.flush()
        template_id = template.id

    batch = ImportBatch(
        user_id=current_user.id,
        account_id=account.id,
        filename="ledger_import",
        file_sha256="hash",
        template_id=template_id,
        column_mapping_json=payload.column_mapping,
        row_count=len(payload.rows)
    )
    db.add(batch)
    db.flush()

    mapping = payload.column_mapping
    stated_closing_paise = None
    min_date, max_date = None, None
    tot_in, tot_out = 0, 0

    for idx, row in enumerate(payload.rows, start=1):
        row_str = " ".join(str(v) for v in row.values()).lower()
        val_in = parse_amount_to_paise(row.get(mapping.get("money_in", "")))
        val_out = parse_amount_to_paise(row.get(mapping.get("money_out", "")))

        # Handle convention:
        # DEBIT_IN (Tally style): Debit column mapped to money_in is Money In, Credit column mapped to money_out is Money Out
        # STANDARD (Normal CSV): Debit column mapped to money_in is Money Out, Credit column mapped to money_out is Money In
        if payload.ledger_convention == "STANDARD":
            m_in = val_out
            m_out = val_in
        else:
            m_in = val_in
            m_out = val_out

        if "closing balance" in row_str or "closing" in row_str:
            raw_closing_val = row.get(mapping.get("money_in", "")) or row.get(mapping.get("money_out", "")) or row.get(mapping.get("instrument_no", ""))
            parsed_closing = parse_amount_to_paise(raw_closing_val)
            if parsed_closing > 0:
                stated_closing_paise = parsed_closing
            elif m_in > 0:
                stated_closing_paise = m_in
            elif m_out > 0:
                stated_closing_paise = m_out
            continue
        if m_in > 0 and m_out > 0:
            m_out = 0  # Enforce check single direction constraint

        if m_in == 0 and m_out == 0:
            continue

        raw_date = str(row.get(mapping.get("entry_date", ""), "")).strip()
        e_date = None
        for fmt in ["%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y"]:
            try:
                e_date = datetime.strptime(raw_date, fmt).date()
                break
            except Exception:
                pass
        if not e_date:
            continue

        if min_date is None or e_date < min_date:
            min_date = e_date
        if max_date is None or e_date > max_date:
            max_date = e_date

        tot_in += m_in
        tot_out += m_out

        b_entry = BookEntry(
            user_id=current_user.id,
            account_id=account.id,
            import_batch_id=batch.id,
            entry_date=e_date,
            voucher_no=str(row.get(mapping.get("voucher_no", ""), "")),
            voucher_type=str(row.get(mapping.get("voucher_type", ""), "")),
            narration=str(row.get(mapping.get("narration", ""), "")),
            party_name=str(row.get(mapping.get("party_name", ""), "")),
            instrument_no=str(row.get(mapping.get("instrument_no", ""), "")),
            money_in_paise=m_in,
            money_out_paise=m_out,
            row_index=idx,
            source_row_hash=compute_row_hash(row)
        )
        db.add(b_entry)

    batch.period_from = min_date
    batch.period_to = max_date
    batch.book_closing_paise = stated_closing_paise if stated_closing_paise is not None else (tot_in - tot_out)
    db.commit()
    return {
        "status": "success",
        "batch_id": batch.id,
        "row_count": batch.row_count,
        "period_from": min_date.isoformat() if min_date else None,
        "period_to": max_date.isoformat() if max_date else None
    }


@router.get("/templates")
def get_import_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Part 7: GET /v1/reconciliation/templates"""
    templates = db.query(ImportTemplate).filter(ImportTemplate.user_id == current_user.id).all()
    return [{"id": t.id, "name": t.name, "column_mapping": t.column_mapping_json} for t in templates]


class ReconciliationRunResponse(BaseModel):
    id: UUID
    account_id: UUID
    version: Optional[int] = 1
    supersedes_run_id: Optional[UUID] = None
    period_from: date
    period_to: date
    book_closing_paise: int
    bank_closing_paise: int
    computed_bank_closing_paise: int
    residual_paise: int
    verdict: str
    status: str
    forced: bool
    matched_count: int
    unmatched_bank_count: int
    unmatched_book_count: int
    pending_review_count: int
    created_at: datetime
    completed_at: Optional[datetime] = None


@router.get("/runs", response_model=List[ReconciliationRunResponse])
def get_reconciliation_runs(
    account_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    verdict: Optional[str] = Query(None),
    period_from: Optional[date] = Query(None),
    period_to: Optional[date] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Part 7: GET /v1/reconciliation/runs — list authenticated user's reconciliation runs."""
    query = db.query(ReconciliationRun).filter(
        ReconciliationRun.user_id == current_user.id
    )

    if account_id:
        query = query.filter(ReconciliationRun.account_id == account_id)
    if status:
        query = query.filter(ReconciliationRun.status == status)
    if verdict:
        query = query.filter(ReconciliationRun.verdict == verdict)
    if period_from:
        query = query.filter(ReconciliationRun.period_from >= period_from)
    if period_to:
        query = query.filter(ReconciliationRun.period_to <= period_to)

    runs = query.order_by(ReconciliationRun.created_at.desc()).offset(offset).limit(limit).all()
    return runs


@router.post("/runs")

def create_reconciliation_run(
    payload: CreateRunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Part 7: POST /v1/reconciliation/runs — start a run with pre-flight check and optional force."""
    account = None
    if getattr(payload, "account_id", None):
        account = db.query(Account).filter(
            Account.id == payload.account_id,
            Account.user_id == current_user.id
        ).first()
        if not account:
            raise HTTPException(status_code=404, detail="Specified bank account not found or does not belong to user")
    else:
        account = db.query(Account).filter(Account.user_id == current_user.id).first()
        if not account:
            account = Account(
                id=uuid.uuid4(),
                user_id=current_user.id,
                bank_code="HDFC",
                account_number_masked="****1234",
                account_type="CURRENT"
            )
            db.add(account)
            db.flush()



    batch_id = payload.import_batch_id
    if batch_id:
        # Validate that the provided batch_id actually exists in the DB.
        # The UI may carry a stale batch_id from a previous session; if it doesn't
        # resolve to a real ImportBatch, fall back to the latest one.
        exists = db.query(ImportBatch).filter(
            ImportBatch.id == batch_id,
            ImportBatch.user_id == current_user.id
        ).first()
        if not exists:
            batch_id = None  # let the fallback below resolve the latest

    if not batch_id:
        latest_batch = db.query(ImportBatch).filter(
            ImportBatch.user_id == current_user.id,
            ImportBatch.account_id == account.id
        ).order_by(ImportBatch.imported_at.desc()).first()
        if latest_batch:
            batch_id = latest_batch.id


    engine = ReconciliationMatchingEngine(
        db=db,
        user_id=current_user.id,
        account_id=account.id,
        period_from=payload.period_from,
        period_to=payload.period_to
    )
    try:
        run = engine.execute_run(import_batch_id=batch_id, force=payload.force)
        return {
            "run_id": run.id,
            "verdict": run.verdict,
            "forced": run.forced,
            "residual_paise": run.residual_paise,
            "matched_count": run.matched_count,
            "unmatched_bank_count": run.unmatched_bank_count,
            "unmatched_book_count": run.unmatched_book_count
        }
    except ValueError as ve:
        logger.warning(f"Validation error in reconciliation run: {ve}")
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.exception("Reconciliation run failed with unhandled exception")
        raise HTTPException(status_code=500, detail=f"Reconciliation engine failed: {str(e)}")


@router.get("/runs/{run_id}")
def get_reconciliation_run(
    run_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Part 7: GET /v1/reconciliation/runs/{id} — full BRS bridge report."""
    run = db.query(ReconciliationRun).filter(
        ReconciliationRun.id == run_id,
        ReconciliationRun.user_id == current_user.id
    ).first()

    if not run:
        raise HTTPException(status_code=404, detail="Reconciliation run not found")

    if getattr(run, "engine_version", None) != "v2.0":
        raise HTTPException(status_code=400, detail="Outdated engine version for reconciliation run. Please re-run matching.")

    items = db.query(ReconciliationItem).filter(ReconciliationItem.run_id == run.id).all()
    matches = db.query(ReconciliationMatch).filter(ReconciliationMatch.run_id == run.id).all()

    formatted_matches = []
    for m in matches:
        formatted_matches.append({
            "match_id": m.id,
            "tier": m.tier,
            "confidence": m.confidence,
            "status": m.status,
            "reason": m.reason,
            "lines": [
                {
                    "id": line.id,
                    "bank_txn_id": line.bank_txn_id,
                    "book_entry_id": line.book_entry_id
                } for line in m.lines
            ]
        })

    debug_log = run.debug_log_json or getattr(run, "debug_log", None)

    timing_count = len([it for it in items if getattr(it.side, "value", it.side) == "book"])
    bank_only_cnt = len([it for it in items if getattr(it.side, "value", it.side) == "bank"])
    amount_mismatch_cnt = len([m for m in matches if m.status == "AMOUNT_MISMATCH" or m.tier == "priority_5_amount_mismatch"])
    date_mismatch_cnt = len([m for m in matches if m.status == "DATE_MISMATCH" or m.tier == "priority_4_date_mismatch"])
    dup_cnt = len([m for m in matches if m.status == "DUPLICATE"])

    # Calculate opening balance and net movement for report display
    has_explicit = bool(run.import_batch and run.import_batch.book_opening_paise is not None and run.import_batch.book_opening_paise != 0)
    
    # Opening balance: use explicit book opening if present, else run's stored book_opening_paise (bank opening fallback)
    opening_bal_paise = run.import_batch.book_opening_paise if has_explicit else run.book_opening_paise
    
    # Net movement: book_closing_paise - opening_bal_paise
    net_mov_paise = run.book_closing_paise - opening_bal_paise

    return {
        "id": run.id,
        "period_from": run.period_from,
        "period_to": run.period_to,
        "book_closing_paise": run.book_closing_paise,
        "opening_balance_paise": opening_bal_paise,
        "net_movement_paise": net_mov_paise,
        "has_book_opening": has_explicit,
        "bank_closing_paise": run.bank_closing_paise,
        "computed_bank_closing_paise": run.computed_bank_closing_paise,
        "residual_paise": run.residual_paise,
        "verdict": run.verdict,
        "forced": run.forced,
        "matched_count": run.matched_count,
        "timing_differences": timing_count,
        "bank_only_count": bank_only_cnt,
        "ledger_only_count": timing_count,
        "amount_mismatch_count": amount_mismatch_cnt,
        "date_mismatch_count": date_mismatch_cnt,
        "duplicate_count": dup_cnt,
        "unexplained_count": bank_only_cnt,
        "unmatched_bank_count": run.unmatched_bank_count,
        "unmatched_book_count": run.unmatched_book_count,
        "pending_review_count": run.pending_review_count,
        "items": [
            {
                "id": it.id,
                "side": getattr(it.side, "value", it.side),
                "category": it.brs_category,
                "amount_paise": it.amount_paise,
                "direction": getattr(it.direction, "value", it.direction),
                "age_days": it.age_days,
                "exception_flag": it.exception_flag,
                "exception_reason": it.exception_reason,
                "book_entry_id": it.book_entry_id,
                "bank_txn_id": it.bank_txn_id
            }
            for it in items
        ],
        "matches": formatted_matches,
        "debug_log": debug_log
    }


@router.get("/runs/{run_id}/matches")
def get_run_matches(
    run_id: UUID,
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Part 7: GET /v1/reconciliation/runs/{id}/matches?status=pending_review"""
    run = db.query(ReconciliationRun).filter(
        ReconciliationRun.id == run_id,
        ReconciliationRun.user_id == current_user.id
    ).first()

    if not run:
        raise HTTPException(status_code=404, detail="Reconciliation run not found")

    query = db.query(ReconciliationMatch).filter(
        ReconciliationMatch.run_id == run_id,
        ReconciliationMatch.user_id == current_user.id
    )
    if status:
        query = query.filter(ReconciliationMatch.status == status)

    matches = query.all()

    res = []
    for m in matches:
        res.append({
            "match_id": m.id,
            "tier": m.tier,
            "confidence": m.confidence,
            "status": m.status,
            "reason": m.reason,
            "lines": [
                {
                    "id": line.id,
                    "bank_txn_id": line.bank_txn_id,
                    "book_entry_id": line.book_entry_id
                } for line in m.lines
            ]
        })
    return res


@router.post("/matches/{match_id}/confirm")
def confirm_match(
    match_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    match = db.query(ReconciliationMatch).filter(
        ReconciliationMatch.id == match_id,
        ReconciliationMatch.user_id == current_user.id
    ).first()

    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    match.status = MatchStatusEnum.CONFIRMED.value
    match.reviewed_by = current_user.id
    match.reviewed_at = datetime.utcnow()
    db.commit()
    return {"status": "success"}


@router.post("/matches/{match_id}/reject")
def reject_match(
    match_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    match = db.query(ReconciliationMatch).filter(
        ReconciliationMatch.id == match_id,
        ReconciliationMatch.user_id == current_user.id
    ).first()

    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    match.status = MatchStatusEnum.REJECTED.value
    match.reviewed_by = current_user.id
    match.reviewed_at = datetime.utcnow()
    db.commit()
    return {"status": "success"}


@router.post("/items/{item_id}/classify")
def classify_item(
    item_id: UUID,
    payload: ClassifyItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    item = db.query(ReconciliationItem).filter(
        ReconciliationItem.id == item_id,
        ReconciliationItem.user_id == current_user.id
    ).first()

    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    item.brs_category = payload.brs_category
    item.overridden_by_user = True
    item.overridden_by = current_user.id
    item.overridden_at = datetime.utcnow()
    db.commit()
    return {"status": "success"}


@router.get("/runs/{run_id}/export")
def export_brs_report(
    run_id: UUID,
    format: str = Query("pdf"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Export the full BRS reconciliation report as a professional PDF."""
    from io import BytesIO
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph,
        Spacer, HRFlowable, KeepTogether
    )
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    run = db.query(ReconciliationRun).filter(
        ReconciliationRun.id == run_id,
        ReconciliationRun.user_id == current_user.id
    ).first()

    if not run:
        raise HTTPException(status_code=404, detail="Reconciliation run not found")

    items = db.query(ReconciliationItem).filter(ReconciliationItem.run_id == run.id).all()

    # ── Helpers ───────────────────────────────────────────────────────────────
    def rs(paise):
        if paise is None:
            return "—"
        sign = "-" if paise < 0 else ""
        return f"{sign}Rs.{abs(paise) / 100:,.2f}"

    def _verdict_label(v):
        if v in ("reconciled_clean", "RECONCILED_CLEAN"):
            return "RECONCILED — CLEAN", colors.HexColor("#059669"), colors.HexColor("#d1fae5")
        if v in ("reconciled_with_exceptions", "RECONCILED_WITH_EXCEPTIONS"):
            return "RECONCILED WITH EXCEPTIONS", colors.HexColor("#d97706"), colors.HexColor("#fef3c7")
        return "UNRECONCILED", colors.HexColor("#dc2626"), colors.HexColor("#fee2e2")

    def _item_label(cat, side, direction):
        cat_l = (cat or "").lower()
        side_l = (side or "").lower()
        dir_l  = (direction or "").lower()
        if "unpresented" in cat_l or "cheque" in cat_l:
            return "Unpresented Cheque"
        if "uncleared" in cat_l or "deposit_in_transit" in cat_l:
            return "Uncleared Deposit"
        if "interest" in cat_l:
            return "Interest Credit"
        if "direct_credit" in cat_l:
            return "Direct Credit"
        if "direct_debit" in cat_l or "bank_charge" in cat_l:
            return "Bank Charge / Debit"
        return cat.replace("_", " ").title() if cat else "Other"

    # ── Collect data ─────────────────────────────────────────────────────────
    book_items = [it for it in items if getattr(it.side, "value", it.side) == "book"]
    bank_items = [it for it in items if getattr(it.side, "value", it.side) == "bank"]

    verdict_str, verdict_color, verdict_bg = _verdict_label(run.verdict)
    period_str = f"{run.period_from}  to  {run.period_to}"
    residual_paise = run.residual_paise or 0
    residual_str = rs(residual_paise)
    if residual_paise > 0:
        residual_note = "Bank closing is higher than books"
    elif residual_paise < 0:
        residual_note = "Books closing is higher than bank"
    else:
        residual_note = "Fully reconciled — no unexplained difference"

    has_explicit = bool(
        run.import_batch
        and run.import_batch.book_opening_paise is not None
        and run.import_batch.book_opening_paise != 0
    )
    opening_paise = run.import_batch.book_opening_paise if has_explicit else run.book_opening_paise
    net_mov_paise = (run.book_closing_paise or 0) - (opening_paise or 0)

    # ── Document setup ────────────────────────────────────────────────────────
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=1.8 * cm, leftMargin=1.8 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        pageCompression=0,
    )

    ss = getSampleStyleSheet()
    H1  = ParagraphStyle("H1",  parent=ss["Heading1"], fontSize=16, spaceAfter=2)
    H2  = ParagraphStyle("H2",  parent=ss["Heading2"], fontSize=11, spaceAfter=4, spaceBefore=10)
    SUB = ParagraphStyle("SUB", parent=ss["Normal"],   fontSize=9,  textColor=colors.HexColor("#6b7280"))
    NRM = ParagraphStyle("NRM", parent=ss["Normal"],   fontSize=9)
    RGT = ParagraphStyle("RGT", parent=ss["Normal"],   fontSize=9,  alignment=TA_RIGHT)
    BLD = ParagraphStyle("BLD", parent=ss["Normal"],   fontSize=9,  fontName="Helvetica-Bold")

    NAVY  = colors.HexColor("#1a3c5e")
    LGRAY = colors.HexColor("#f3f4f6")
    DGRAY = colors.HexColor("#6b7280")

    def _tbl(data, col_widths, header_rows=1, alt_rows=True):
        tbl = Table(data, colWidths=col_widths, repeatRows=header_rows)
        style = [
            ("BACKGROUND",   (0, 0), (-1, header_rows - 1), NAVY),
            ("TEXTCOLOR",    (0, 0), (-1, header_rows - 1), colors.white),
            ("FONTNAME",     (0, 0), (-1, header_rows - 1), "Helvetica-Bold"),
            ("FONTSIZE",     (0, 0), (-1, -1),              8),
            ("TOPPADDING",   (0, 0), (-1, -1),              4),
            ("BOTTOMPADDING",(0, 0), (-1, -1),              4),
            ("LEFTPADDING",  (0, 0), (-1, -1),              6),
            ("RIGHTPADDING", (0, 0), (-1, -1),              6),
            ("GRID",         (0, 0), (-1, -1),              0.4, colors.HexColor("#d1d5db")),
            ("VALIGN",       (0, 0), (-1, -1),              "MIDDLE"),
        ]
        if alt_rows:
            for i in range(header_rows, len(data)):
                if i % 2 == 0:
                    style.append(("BACKGROUND", (0, i), (-1, i), LGRAY))
        tbl.setStyle(TableStyle(style))
        return tbl

    elems = []

    # ── Header ────────────────────────────────────────────────────────────────
    elems.append(Paragraph("Bank Reconciliation Statement", H1))
    elems.append(Paragraph(f"Period: {period_str}", SUB))
    elems.append(Paragraph(f"Run ID: {run.id}", SUB))
    elems.append(Paragraph(
        f"Generated: {datetime.utcnow().strftime('%d %b %Y, %H:%M UTC')}",
        SUB
    ))
    elems.append(Spacer(1, 0.4 * cm))
    elems.append(HRFlowable(width="100%", thickness=1, color=NAVY))
    elems.append(Spacer(1, 0.3 * cm))

    # ── Verdict banner ────────────────────────────────────────────────────────
    elems.append(Paragraph("Verdict", H2))
    verdict_tbl = Table(
        [[Paragraph(f"<b>{verdict_str}</b>", ParagraphStyle(
            "VRD", parent=ss["Normal"], fontSize=13,
            fontName="Helvetica-Bold", textColor=verdict_color
        ))]],
        colWidths=[doc.width],
    )
    verdict_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), verdict_bg),
        ("TOPPADDING",    (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (-1, -1), 14),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
        ("ROUNDEDCORNERS",(0, 0), (-1, -1), [6, 6, 6, 6]),
        ("BOX",           (0, 0), (-1, -1), 1.5, verdict_color),
    ]))
    elems.append(verdict_tbl)
    elems.append(Spacer(1, 0.4 * cm))

    # ── BRS Bridge Summary ────────────────────────────────────────────────────
    elems.append(Paragraph("BRS Bridge Summary", H2))

    w3 = doc.width / 3.0
    kpi_data = [
        [
            Paragraph("<b>Balance as per books</b>",      BLD),
            Paragraph("<b>Computed Bank Closing</b>",     BLD),
            Paragraph("<b>Actual Bank Closing</b>",       BLD),
        ],
        [
            Paragraph(rs(run.book_closing_paise),          NRM),
            Paragraph(rs(run.computed_bank_closing_paise), NRM),
            Paragraph(rs(run.bank_closing_paise),          NRM),
        ],
    ]
    kpi_tbl = Table(kpi_data, colWidths=[w3, w3, w3])
    kpi_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), LGRAY),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
    ]))
    elems.append(kpi_tbl)
    elems.append(Spacer(1, 0.3 * cm))

    # Residual row
    res_color = colors.HexColor("#dc2626") if residual_paise != 0 else colors.HexColor("#059669")
    res_tbl = Table(
        [[
            Paragraph("<b>Residual Difference</b>", BLD),
            Paragraph(f"<b>{residual_str}</b>", ParagraphStyle(
                "RES", parent=ss["Normal"], fontSize=9,
                fontName="Helvetica-Bold", textColor=res_color
            )),
            Paragraph(residual_note, ParagraphStyle(
                "RNT", parent=ss["Normal"], fontSize=8, textColor=DGRAY
            )),
        ]],
        colWidths=[4 * cm, 4 * cm, doc.width - 8 * cm],
    )
    res_tbl.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#f9fafb")),
        ("BOX",           (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elems.append(res_tbl)
    elems.append(Spacer(1, 0.5 * cm))

    # ── Book position ─────────────────────────────────────────────────────────
    elems.append(Paragraph("Book Position", H2))
    book_rows = [
        ["Item", "Amount"],
        ["Opening Balance",           rs(opening_paise)],
        ["Net Book Movement",          rs(net_mov_paise)],
        ["Book Closing Balance",       rs(run.book_closing_paise)],
    ]
    elems.append(_tbl(book_rows, [doc.width * 0.7, doc.width * 0.3]))
    elems.append(Spacer(1, 0.5 * cm))

    # ── Timing Differences (book-side items) ──────────────────────────────────
    elems.append(Paragraph(f"Timing Differences ({len(book_items)})", H2))
    if book_items:
        td_rows = [["Description", "Direction", "Amount (Rs.)"]]
        for it in book_items:
            label = _item_label(
                getattr(it, "brs_category", ""),
                getattr(it.side, "value", it.side),
                getattr(it.direction, "value", it.direction),
            )
            dir_str = getattr(it.direction, "value", it.direction) or ""
            td_rows.append([
                label,
                dir_str.upper(),
                rs(it.amount_paise),
            ])
        t = _tbl(td_rows, [doc.width * 0.55, doc.width * 0.2, doc.width * 0.25])
        elems.append(t)
    else:
        elems.append(Paragraph("No timing differences.", NRM))
    elems.append(Spacer(1, 0.5 * cm))

    # ── Bank-Only Items ───────────────────────────────────────────────────────
    journal_items = [it for it in bank_items if it.exception_flag]
    other_bank    = [it for it in bank_items if not it.exception_flag]

    elems.append(Paragraph(f"Bank-Only Transactions ({len(bank_items)})", H2))
    if bank_items:
        bk_rows = [["Description", "Direction", "Amount (Rs.)", "Action Required"]]
        for it in bank_items + []:
            label = _item_label(
                getattr(it, "brs_category", ""),
                getattr(it.side, "value", it.side),
                getattr(it.direction, "value", it.direction),
            )
            dir_str = getattr(it.direction, "value", it.direction) or ""
            action  = "Journal Entry Required" if it.exception_flag else "—"
            bk_rows.append([label, dir_str.upper(), rs(it.amount_paise), action])
        t = _tbl(bk_rows, [
            doc.width * 0.38,
            doc.width * 0.15,
            doc.width * 0.22,
            doc.width * 0.25,
        ])
        elems.append(t)
    else:
        elems.append(Paragraph("No bank-only transactions.", NRM))
    elems.append(Spacer(1, 0.5 * cm))

    # ── Counts summary ────────────────────────────────────────────────────────
    elems.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e5e7eb")))
    elems.append(Spacer(1, 0.2 * cm))
    summary_rows = [
        ["Metric", "Count"],
        ["Matched Transactions",      str(run.matched_count or 0)],
        ["Timing Differences",        str(len(book_items))],
        ["Bank-Only Items",           str(len(bank_items))],
        ["Journal Entries Required",  str(len(journal_items))],
        ["Pending Review",            str(run.pending_review_count or 0)],
    ]
    elems.append(Paragraph("Summary Counts", H2))
    elems.append(_tbl(summary_rows, [doc.width * 0.7, doc.width * 0.3]))

    # ── Build PDF ─────────────────────────────────────────────────────────────
    doc.build(elems)
    buf.seek(0)

    filename = f"BRS_{run.period_from}_{run.period_to}_{run_id}.pdf"
    return Response(
        content=buf.read(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
