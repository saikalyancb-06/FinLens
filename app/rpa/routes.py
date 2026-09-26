"""
RPA Statement Download – FastAPI Routes
----------------------------------------
Endpoints exposed under /rpa/ (registered in main.py alongside /email/).

POST  /rpa/start            Start a new RPA download job
POST  /rpa/otp/{job_id}     Submit the OTP for an AWAITING_OTP job
GET   /rpa/status/{job_id}  Poll job status
GET   /rpa/jobs             List all RPA jobs for the current user
DELETE /rpa/{job_id}        Cancel / delete a job record
"""
import asyncio
import datetime
import logging
import os
import uuid
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.rpa_job import RpaJob, RpaJobStatus
from app.models.user import User
from app.utils.security import get_current_user
from app.rpa.registry import get_supported_banks, get_supported_bank_keys
from app.rpa.session_store import set_credentials, set_otp, set_pdf_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rpa", tags=["RPA Statement Download"])


# ── Pydantic schemas ─────────────────────────────────────────────────────────

# ── Pydantic schemas ─────────────────────────────────────────────────────────

class StartRpaRequest(BaseModel):
    bank_name: str                  # e.g. "sbi", "hdfc", "mock_bank"
    start_date: str                 # YYYY-MM-DD
    end_date: str                   # YYYY-MM-DD
    user_acknowledged: bool = False  # User must confirm automated access
    debug_mode: bool = False        # Enable step screenshots + slow-motion


class AgentStatusUpdateRequest(BaseModel):
    job_id: str
    status: str
    error_message: Optional[str] = None
    statement_id: Optional[str] = None


class OtpSubmitRequest(BaseModel):
    otp: str


class PdfPasswordSubmitRequest(BaseModel):
    pdf_password: str


class RpaJobResponse(BaseModel):
    job_id: str
    bank_name: str
    status: str
    start_date: str
    end_date: str
    created_at: str
    updated_at: str
    error_message: Optional[str] = None
    screenshot_path: Optional[str] = None
    file_path: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/banks", summary="List supported bank keys")
def list_supported_banks():
    """Returns the list of bank keys that have a registered RPA adapter."""
    return get_supported_banks()


@router.get("/agent/pending-jobs", summary="Fetch pending RPA jobs for local agent")
def get_pending_jobs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Called by local KredoAgent.exe to claim queued jobs.
    Returns metadata ONLY (job_id, bank_name, date_range). No credentials.
    """
    jobs = (
        db.query(RpaJob)
        .filter(RpaJob.user_id == current_user.id, RpaJob.status == RpaJobStatus.QUEUED)
        .order_by(RpaJob.created_at.asc())
        .all()
    )
    return [
        {
            "job_id": str(j.id),
            "bank_name": j.bank_name,
            "start_date": j.date_range_start.strftime("%Y-%m-%d"),
            "end_date": j.date_range_end.strftime("%Y-%m-%d"),
        }
        for j in jobs
    ]


@router.post("/agent/update-status", summary="Update RPA job status from local agent")
def update_job_status(
    payload: AgentStatusUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Called by local KredoAgent.exe to push state machine progress updates.
    """
    job = _get_job(db, payload.job_id, current_user)
    try:
        new_status = RpaJobStatus(payload.status.lower())
        job.status = new_status
    except ValueError:
        pass  # Keep as-is if unmapped enum string

    if payload.error_message:
        job.error_message = payload.error_message
    if payload.statement_id:
        job.statement_id = payload.statement_id
    job.updated_at = datetime.datetime.utcnow()
    db.commit()

    return {"message": "Status updated successfully", "job_id": str(job.id), "status": str(job.status)}


@router.post("/start", status_code=status.HTTP_202_ACCEPTED)
async def start_rpa_job(
    payload: StartRpaRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Start an RPA download run. Metadata only.
    Credentials are collected locally inside KredoAgent.exe on the user's PC.
    """
    if not payload.user_acknowledged:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You must acknowledge that you authorise automated access to your bank account "
                   "(set user_acknowledged=true).",
        )

    # Validate dates
    try:
        start_dt = datetime.datetime.strptime(payload.start_date, "%Y-%m-%d")
        end_dt = datetime.datetime.strptime(payload.end_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be in YYYY-MM-DD format.")

    if end_dt < start_dt:
        raise HTTPException(status_code=400, detail="end_date must be >= start_date.")

    bank_name = payload.bank_name.lower()
    supported_keys = get_supported_bank_keys()
    if bank_name not in supported_keys and bank_name != "mock_bank":
        raise HTTPException(status_code=400, detail=f"Unsupported bank '{payload.bank_name}'.")

    job = RpaJob(
        user_id=current_user.id,
        bank_name=bank_name,
        date_range_start=start_dt,
        date_range_end=end_dt,
        encrypted_credentials=None,
        status=RpaJobStatus.QUEUED,
        user_acknowledged="yes",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    job_id = str(job.id)
    logger.info(f"[RPA Routes] User {current_user.id} queued metadata RPA job {job_id} for bank={job.bank_name}")

    return {
        "job_id": job_id,
        "status": job.status.value if hasattr(job.status, "value") else str(job.status),
        "bank_name": job.bank_name,
        "message": "RPA job metadata queued. Local KredoAgent.exe will claim and process locally.",
    }


@router.post("/otp/{job_id}", summary="Submit OTP for an awaiting RPA job")
def submit_otp(
    job_id: str,
    payload: OtpSubmitRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Called by the UI OTP modal.  Injects the OTP into the encrypted credentials
    and sets the job back to RUNNING so the background runner can continue.
    """
    job = _get_job(db, job_id, current_user)

    if job.status != RpaJobStatus.AWAITING_OTP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job is not awaiting OTP (current status: {job.status}).",
        )

    if not payload.otp or len(payload.otp.strip()) < 4:
        raise HTTPException(status_code=400, detail="OTP must be at least 4 digits.")

    set_otp(job_id, payload.otp.strip())
    job.updated_at = datetime.datetime.utcnow()
    db.commit()

    logger.info(f"[RPA Routes] OTP submitted for job {job_id}")
    return {"message": "OTP received. The job will resume automatically."}


@router.post("/pdf-password/{job_id}", summary="Submit PDF password for an awaiting RPA job")
def submit_pdf_password(
    job_id: str,
    payload: PdfPasswordSubmitRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _get_job(db, job_id, current_user)

    if job.status != RpaJobStatus.AWAITING_PDF_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Job is not awaiting PDF password (current status: {job.status}).",
        )

    if not payload.pdf_password or not payload.pdf_password.strip():
        raise HTTPException(status_code=400, detail="PDF password must not be empty.")

    set_pdf_password(job_id, payload.pdf_password.strip())
    job.updated_at = datetime.datetime.utcnow()
    db.commit()

    logger.info(f"[RPA Routes] PDF password submitted for job {job_id}")
    return {"message": "PDF password received. The job will resume automatically."}


@router.get("/status/{job_id}", response_model=RpaJobResponse)
def get_job_status(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Poll this endpoint every 3–5 s from the UI to track the job lifecycle."""
    job = _get_job(db, job_id, current_user)
    return _job_to_response(job)


@router.get("/jobs", response_model=List[RpaJobResponse])
def list_rpa_jobs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all RPA jobs for the current user, newest first."""
    jobs = (
        db.query(RpaJob)
        .filter(RpaJob.user_id == current_user.id)
        .order_by(RpaJob.created_at.desc())
        .all()
    )
    return [_job_to_response(j) for j in jobs]


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rpa_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a job record (only allowed for DONE / FAILED jobs)."""
    job = _get_job(db, job_id, current_user)
    if job.status in (
        RpaJobStatus.QUEUED,
        RpaJobStatus.LOGGING_IN,
        RpaJobStatus.AWAITING_OTP,
        RpaJobStatus.AWAITING_PDF_PASSWORD,
        RpaJobStatus.DOWNLOADING,
        RpaJobStatus.PARSING,
        RpaJobStatus.IMPORTING,
    ):
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a job that is still in progress.",
        )
    db.delete(job)
    db.commit()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_job(db: Session, job_id: str, current_user: User) -> RpaJob:
    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format.")
    job = db.query(RpaJob).filter(RpaJob.id == uid, RpaJob.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="RPA job not found.")
    return job


def _job_to_response(job: RpaJob) -> dict:
    status_value = job.status.value if hasattr(job.status, "value") else str(job.status)
    if status_value == RpaJobStatus.FAILED.value and job.error_message:
        status_value = f"failed({job.error_message})"
    return {
        "job_id": str(job.id),
        "bank_name": job.bank_name,
        "status": status_value,
        "start_date": job.date_range_start.strftime("%Y-%m-%d"),
        "end_date": job.date_range_end.strftime("%Y-%m-%d"),
        "created_at": job.created_at.strftime("%Y-%m-%d %H:%M:%S") if job.created_at else "",
        "updated_at": job.updated_at.strftime("%Y-%m-%d %H:%M:%S") if job.updated_at else "",
        "error_message": job.error_message,
        "screenshot_path": job.screenshot_path,
        "file_path": job.file_path,
    }


def _run_in_new_loop(job_id: str) -> None:
    """
    FastAPI's BackgroundTasks runs in the main event loop.
    We create a new event loop so the Playwright coroutine doesn't block
    the HTTP server when running synchronously (e.g. with --workers 1).
    """
    import asyncio
    from app.rpa.runner import run_rpa_job
    asyncio.run(run_rpa_job(job_id))
