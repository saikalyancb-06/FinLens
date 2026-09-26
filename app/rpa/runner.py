"""
RPA Orchestrator / Runner
--------------------------
Launches Playwright Chromium, runs the bank adapter pipeline, handles the
human-in-the-loop OTP pause, and hands the downloaded file to the same
parsing pipeline that email statements use.

Status flow:
  PENDING → RUNNING → AWAITING_OTP (if OTP needed) → DOWNLOADING → DONE
                                                  ↓
                                               FAILED  (at any point)
"""
import asyncio
import datetime
import logging
import os
import uuid

from playwright.async_api import async_playwright

from app.database.session import SessionLocal
from app.models.rpa_job import RpaJob, RpaJobStatus
from app.rpa.base_adapter import OTPRequired, AntiBot
from app.rpa.registry import get_adapter_class, get_supported_banks, create_bank_adapter
from app.rpa.session_store import (
    get_credentials,
    pop_otp,
    pop_pdf_password,
    get_otp_attempts,
    clear_session,
)
from app.services.parsing_queue import process_file_parsing_task

logger = logging.getLogger(__name__)

# Stealth user-agent
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

MAX_OTP_WAIT_SECONDS = 300  # 5 minutes before giving up on OTP
RETRY_COUNT = 2


async def run_rpa_job(job_id: str) -> None:
    """
    Main coroutine launched as an asyncio task when the user starts an RPA run.
    Runs entirely in background; updates the rpa_jobs row at each stage.
    """
    db = SessionLocal()
    try:
        job_uuid = uuid.UUID(str(job_id)) if isinstance(job_id, str) else job_id
        job = db.query(RpaJob).filter(RpaJob.id == job_uuid).first()
        if not job:
            logger.error(f"[RPA Runner] Job {job_id} not found in DB")
            return

        _update_status(db, job, RpaJobStatus.LOGGING_IN)
        debug_mode = os.getenv("RPA_DEBUG", "false").lower() == "true"
        logger.info(f"[RPA Runner] Starting job {job_id} bank={job.bank_name} debug={debug_mode}")

        credentials = get_credentials(job_id)
        if not credentials:
            return _fail(db, job, "Credentials not available in memory for this job.")

        AdapterClass = get_adapter_class(job.bank_name)
        if not AdapterClass:
            return _fail(db, job, f"No RPA adapter registered for bank '{job.bank_name}'. "
                                  f"Supported banks: {[b['key'] for b in get_supported_banks()]}")
        # Instantiate per-job so debug/screenshot state is isolated
        adapter = create_bank_adapter(job.bank_name, debug_mode=debug_mode, job_id=job_id)

        # ── Launch browser ───────────────────────────────────────────────────
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=False,          # non-headless = lower bot-detection risk
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=_UA,
                viewport={"width": 1366, "height": 768},
                accept_downloads=True,
            )
            page = await context.new_page()

            try:
                # ── Login (with retry) ───────────────────────────────────────
                otp_requested = False
                for attempt in range(1, RETRY_COUNT + 1):
                    try:
                        await adapter.login(page, credentials)
                        break  # success
                    except OTPRequired:
                        if otp_requested:
                            if get_otp_attempts(job_id) >= 3:
                                return _fail(db, job, "OTP attempt limit exceeded.")
                            # Second time OTP not present → wait for another submission.
                            _update_status(db, job, RpaJobStatus.AWAITING_OTP)
                            credentials.pop("otp", None)
                            continue
                        otp_requested = True
                        _update_status(db, job, RpaJobStatus.AWAITING_OTP)
                        logger.info(f"[RPA Runner] Job {job_id} awaiting OTP from user")

                        # ── Poll until user submits OTP via /rpa/otp endpoint ──
                        elapsed = 0
                        while elapsed < MAX_OTP_WAIT_SECONDS:
                            await asyncio.sleep(3)
                            elapsed += 3
                            db.refresh(job)
                            otp = pop_otp(job_id)
                            if otp:
                                credentials["otp"] = otp
                                _update_status(db, job, RpaJobStatus.LOGGING_IN)
                                break
                        else:
                            return _fail(db, job, "OTP timeout: user did not submit OTP within 5 minutes.")

                        continue

                    except AntiBot as e:
                        return _fail(db, job, str(e), page=page)
                    except Exception as e:
                        if attempt == RETRY_COUNT:
                            return _fail(db, job, f"Login failed after {RETRY_COUNT} attempts: {e}", page=page)
                        logger.warning(f"[RPA Runner] Login attempt {attempt} failed: {e}. Retrying…")
                        await asyncio.sleep(3)

                # ── Navigate to statements ───────────────────────────────────
                try:
                    await adapter.navigate_to_statements(page, {})
                except Exception as e:
                    return _fail(db, job, f"Navigation to statements failed: {e}", page=page)

                # ── Download ─────────────────────────────────────────────────
                _update_status(db, job, RpaJobStatus.DOWNLOADING)
                date_range = {
                    "start": job.date_range_start.strftime("%Y-%m-%d"),
                    "end": job.date_range_end.strftime("%Y-%m-%d"),
                }
                try:
                    file_path = await adapter.download_statement(page, date_range)
                except Exception as e:
                    return _fail(db, job, f"Download failed: {e}", page=page)

                if file_path.endswith("_NEEDS_PASSWORD.pdf"):
                    job.file_path = file_path
                    _update_status(db, job, RpaJobStatus.AWAITING_PDF_PASSWORD)
                    logger.info(f"[RPA Runner] Job {job_id} awaiting PDF password")
                    elapsed = 0
                    while elapsed < MAX_OTP_WAIT_SECONDS:
                        await asyncio.sleep(3)
                        elapsed += 3
                        pdf_password = pop_pdf_password(job_id)
                        if pdf_password:
                            _update_status(db, job, RpaJobStatus.PARSING)
                            _update_status(db, job, RpaJobStatus.IMPORTING)
                            summary = process_file_parsing_task(
                                file_id=str(job.id),
                                file_path=file_path,
                                user_id=str(job.user_id),
                                pdf_password=pdf_password,
                                rpa_job_id=job.id,
                            )
                            if summary.get("status") != "COMPLETED":
                                return _fail(db, job, summary.get("error_message") or "Parsing failed.")
                            _update_status(db, job, RpaJobStatus.SUCCESS)
                            clear_session(job_id)
                            return
                    return _fail(db, job, "PDF password timeout: user did not submit password within 5 minutes.")

                _update_status(db, job, RpaJobStatus.PARSING)
                _update_status(db, job, RpaJobStatus.IMPORTING)

                # ── Final screenshot for audit ────────────────────────────────
                screens_dir = os.path.join(os.getenv("UPLOAD_DIR", "uploads"), "rpa_screens")
                os.makedirs(screens_dir, exist_ok=True)
                screen_path = os.path.join(screens_dir, f"rpa_{job_id}_done.png")
                await page.screenshot(path=screen_path, full_page=True)

                job.file_path = file_path
                job.screenshot_path = screen_path
                summary = process_file_parsing_task(
                    file_id=str(job.id),
                    file_path=file_path,
                    user_id=str(job.user_id),
                    rpa_job_id=job.id,
                )
                if summary.get("status") != "COMPLETED":
                    return _fail(db, job, summary.get("error_message") or "Parsing failed.")
                _update_status(db, job, RpaJobStatus.SUCCESS)
                clear_session(job_id)
                logger.info(f"[RPA Runner] Job {job_id} DONE → file={file_path}")

            finally:
                await context.close()
                await browser.close()

    except Exception as e:
        logger.exception(f"[RPA Runner] Unhandled error in job {job_id}: {e}")
        try:
            job = db.query(RpaJob).filter(RpaJob.id == job_id).first()
            if job:
                _fail(db, job, str(e))
        except Exception:
            pass
    finally:
        db.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _update_status(db, job: RpaJob, status: RpaJobStatus) -> None:
    job.status = status
    job.updated_at = datetime.datetime.utcnow()
    db.commit()


async def _fail_async(db, job: RpaJob, msg: str, page=None) -> None:
    _fail(db, job, msg, page=None)  # screenshot saved synchronously below
    if page:
        try:
            screens_dir = os.path.join(os.getenv("UPLOAD_DIR", "uploads"), "rpa_screens")
            os.makedirs(screens_dir, exist_ok=True)
            path = os.path.join(screens_dir, f"rpa_{job.id}_error.png")
            await page.screenshot(path=path, full_page=True)
            job.screenshot_path = path
            db.commit()
        except Exception:
            pass


def _fail(db, job: RpaJob, msg: str, page=None) -> None:
    logger.error(f"[RPA Runner] Job {job.id} FAILED: {msg}")
    job.status = RpaJobStatus.FAILED
    job.error_message = msg
    job.updated_at = datetime.datetime.utcnow()
    db.commit()
    clear_session(str(job.id))
