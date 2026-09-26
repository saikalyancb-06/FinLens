"""The public /v1 surface.

Everything an external integrator touches is in this file. It owns HTTP
concerns only — status codes, headers, request ids, serialisation — and
delegates the actual work to `app.b2b.service.run_analysis`.

Processing mode: **synchronous by default, asynchronous on request.** Most
statements finish in well under a second (a 500-row CSV parses and analyses in
tens of milliseconds); a scanned PDF needing OCR can take a minute. Rather than
guess, the client chooses with `async_mode=true`, and either way the response
carries a `request_id` that `GET /v1/analyze/{request_id}` will answer. That
keeps the simple case one call, without leaving a slow case holding a socket
open until a load balancer times it out.
"""
from __future__ import annotations

import datetime
import logging
import os
import tempfile
import threading
from typing import Any, Dict, Optional

from fastapi import (
    APIRouter, BackgroundTasks, Depends, Form, Request, Response, UploadFile, File,
)
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.b2b import errors, ingest, metering, ratelimit, webhooks
from app.b2b.auth import AuthContext, api_key_auth, require_scope
from app.b2b.detect import detect_format
from app.b2b.errors import ApiError
from app.b2b.idempotency import (
    IdempotencyOutcome, begin as idem_begin, complete as idem_complete,
    fail as idem_fail, fingerprint as idem_fingerprint, stored_result,
)
from app.b2b.models import AnalysisRequest, RequestStatus
from app.b2b.parsers.registry import SUPPORTED_FORMATS, UNSUPPORTED_FORMATS
from app.b2b.service import run_analysis
from app.config import settings
from app.database.session import SessionLocal, get_db

logger = logging.getLogger("b2b.api")

router = APIRouter(prefix="/v1", tags=["Financial Analysis API"])

API_VERSION = "1.0.0"
SCOPE_WRITE = "analyze:write"
SCOPE_READ = "analyze:read"

MAX_UPLOAD_BYTES = int(os.getenv("B2B_MAX_FILE_SIZE_BYTES",
                                 str(getattr(settings, "MAX_FILE_SIZE_BYTES",
                                             50 * 1024 * 1024))))


# --------------------------------------------------------------------- helpers

def _request_id(request: Request) -> str:
    return getattr(request.state, "b2b_request_id", None) or "req_unknown"


def _float_or_none(raw: Optional[str], field: str) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ApiError(errors.INVALID_PARAMETER,
                       f"'{field}' must be a number.",
                       detail={"field": field, "received": raw})


def _int_or_none(raw: Optional[str], field: str) -> Optional[int]:
    val = _float_or_none(raw, field)
    if val is None:
        return None
    if val != int(val):
        raise ApiError(errors.INVALID_PARAMETER,
                       f"'{field}' must be a whole number.",
                       detail={"field": field, "received": raw})
    return int(val)


def _loan_params(loan_amount, interest_rate, tenure_months) -> Optional[Dict[str, Any]]:
    """Loan parameters are all-or-nothing.

    A partial set is a client bug, not a default to be filled in: guessing a
    tenure would produce an EMI and a debt-to-income figure that look computed
    but rest on a number nobody supplied.
    """
    supplied = [p for p in (loan_amount, interest_rate, tenure_months) if p is not None]
    if not supplied:
        return None
    if len(supplied) != 3:
        raise ApiError(
            errors.INVALID_PARAMETER,
            "loan_amount, interest_rate and tenure_months must be supplied "
            "together or not at all.",
            detail={"loan_amount": loan_amount, "interest_rate": interest_rate,
                    "tenure_months": tenure_months})
    for name, val, lo, hi in (("loan_amount", loan_amount, 0, None),
                              ("interest_rate", interest_rate, 0, 100),
                              ("tenure_months", tenure_months, 1, 600)):
        if val <= lo if name != "interest_rate" else val < lo:
            raise ApiError(errors.INVALID_PARAMETER,
                           f"'{name}' must be greater than {lo}.",
                           detail={"field": name})
        if hi is not None and val > hi:
            raise ApiError(errors.INVALID_PARAMETER,
                           f"'{name}' must not exceed {hi}.",
                           detail={"field": name})
    return {"loan_amount": loan_amount, "interest_rate": interest_rate,
            "tenure_months": tenure_months}


def _envelope(req: AnalysisRequest, payload: Optional[Dict[str, Any]] = None
              ) -> Dict[str, Any]:
    """The response shape, identical for sync, async and replay."""
    body: Dict[str, Any] = {
        "request_id": req.request_id,
        "status": req.status.value if hasattr(req.status, "value") else str(req.status),
        "created_at": req.created_at.isoformat() if req.created_at else None,
    }
    if req.completed_at:
        body["completed_at"] = req.completed_at.isoformat()
    if req.duration_ms is not None:
        body["duration_ms"] = req.duration_ms

    if payload:
        body["data"] = payload.get("data")
        body["quality"] = payload.get("quality")
        body["metadata"] = {
            "country": req.country,
            "currency": req.currency,
            "detected_format": req.detected_format,
            "filename": req.filename,
            "api_version": API_VERSION,
            "transaction_count": req.transaction_count,
        }
    elif req.status == RequestStatus.FAILED:
        body["error"] = {"code": req.error_code, "message": req.error_message,
                         "request_id": req.request_id}
    return body


# ------------------------------------------------------------------ operations

@router.get("/health", summary="Liveness probe")
def health() -> Dict[str, Any]:
    """Is the process up. Deliberately touches nothing else — a health check
    that queries the database reports the database's health, and a load
    balancer then removes a perfectly good instance because a replica was slow.
    """
    return {"status": "healthy", "service": "financial-analysis-api",
            "version": API_VERSION,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}


@router.get("/ready", summary="Readiness probe")
def ready(db: Session = Depends(get_db)) -> Any:
    """Can this instance actually serve a request — i.e. are its dependencies up.

    The database is required (requests and keys live there). Redis is not: the
    rate limiter degrades to in-process counters, so it is reported but never
    makes the service unready.
    """
    checks: Dict[str, Any] = {}
    ok = True
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:                              # noqa: BLE001
        checks["database"] = "unavailable"
        ok = False
    checks["cache"] = "ok" if ratelimit.redis_available() else "degraded"
    body = {"status": "ready" if ok else "not_ready", "checks": checks,
            "version": API_VERSION}
    return JSONResponse(status_code=200 if ok else 503, content=body)


@router.get("/version", summary="API and ruleset version")
def version() -> Dict[str, Any]:
    return {
        "api_version": API_VERSION,
        "ruleset_version": os.getenv("B2B_RULESET_VERSION", "2026.09.1"),
        "supported_format_count": len(SUPPORTED_FORMATS),
    }


@router.get("/formats", summary="Supported and unsupported input formats")
def formats() -> Dict[str, Any]:
    """The honest list. Anything absent here is rejected with 415, and each
    entry carries its real limitations rather than an optimistic yes.
    """
    return {
        "supported": SUPPORTED_FORMATS,
        "unsupported": UNSUPPORTED_FORMATS,
        "max_file_size_bytes": MAX_UPLOAD_BYTES,
        "max_file_size_mb": round(MAX_UPLOAD_BYTES / (1024 * 1024), 1),
    }


# --------------------------------------------------------------------- analyze

def _process(request_obj_id: str, tmp_path: str, tmp_dir: str,
             filename: str, sha256: str, head: bytes,
             params: Dict[str, Any], retention_hours: int) -> None:
    """Async worker body. Runs in a background task with its own session."""
    db = SessionLocal()
    ingested = None
    try:
        req = db.query(AnalysisRequest).filter(
            AnalysisRequest.request_id == request_obj_id).first()
        if req is None:
            return
        req.status = RequestStatus.PROCESSING
        db.commit()

        ingested = ingest.IngestedFile(
            path=tmp_path, filename=filename, size_bytes=os.path.getsize(tmp_path),
            sha256=sha256, head_bytes=head, tmp_dir=tmp_dir)
        try:
            outcome = run_analysis(ingested, request_id=request_obj_id, **params)
        except ApiError as exc:
            idem_fail(db, req, exc.code, exc.message)
            _fire_webhook(db, req, "analysis.failed")
            return
        except Exception:                          # noqa: BLE001
            logger.exception("[%s] unexpected failure in worker", request_obj_id)
            idem_fail(db, req, errors.INTERNAL_ERROR,
                      "The analysis failed unexpectedly.")
            _fire_webhook(db, req, "analysis.failed")
            return

        idem_complete(db, req, outcome.payload,
                      transaction_count=outcome.transaction_count,
                      detected_format=outcome.detected_format,
                      overall_confidence=outcome.overall_confidence,
                      duration_ms=outcome.duration_ms,
                      retention_hours=retention_hours)
        _fire_webhook(db, req, "analysis.completed")
    finally:
        try:
            ingest.cleanup(ingested)
        except Exception:                          # noqa: BLE001
            pass
        db.close()


def _fire_webhook(db: Session, req: AnalysisRequest, event_type: str) -> None:
    """Best effort. A webhook that cannot be delivered must not change the
    outcome of the analysis that triggered it."""
    try:
        client = req.client if hasattr(req, "client") else None
        if client is None:
            from app.b2b.models import ApiClient
            client = db.query(ApiClient).filter(
                ApiClient.id == req.client_id).first()
        if not client or not getattr(client, "webhook_url", None):
            return
        payload = {
            "event": event_type,
            "request_id": req.request_id,
            "status": req.status.value if hasattr(req.status, "value") else str(req.status),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if req.status == RequestStatus.FAILED:
            payload["error"] = {"code": req.error_code, "message": req.error_message}
        delivery = webhooks.enqueue(db, client, event_type, req.request_id, payload)
        threading.Thread(target=webhooks.deliver_sync, args=(delivery.id,),
                         daemon=True).start()
    except Exception:                              # noqa: BLE001
        logger.warning("[%s] webhook dispatch failed", req.request_id, exc_info=True)


@router.post("/analyze", summary="Analyse a financial statement file")
async def analyze_endpoint(
    request: Request,
    response: Response,
    background: BackgroundTasks,
    file: UploadFile = File(..., description="The statement file."),
    country: Optional[str] = Form(None),
    currency: Optional[str] = Form(None),
    loan_amount: Optional[str] = Form(None),
    interest_rate: Optional[str] = Form(None),
    tenure_months: Optional[str] = Form(None),
    pdf_password: Optional[str] = Form(None),
    include_transactions: Optional[str] = Form(None),
    async_mode: Optional[str] = Form(None),
    ctx: AuthContext = Depends(api_key_auth),
    db: Session = Depends(get_db),
):
    require_scope(ctx, SCOPE_WRITE)
    rid = _request_id(request)

    state = ratelimit.check_and_consume(ctx.client)
    for k, v in ratelimit.headers_for(state).items():
        response.headers[k] = v

    if file is None or not getattr(file, "filename", None):
        raise ApiError(errors.MISSING_FILE,
                       "A file must be supplied in the 'file' form field.")

    country_v = (country or "IN").strip().upper()[:8]
    currency_v = (currency or "INR").strip().upper()[:8]
    loan = _loan_params(_float_or_none(loan_amount, "loan_amount"),
                        _float_or_none(interest_rate, "interest_rate"),
                        _int_or_none(tenure_months, "tenure_months"))
    want_txns = str(include_transactions or "true").lower() not in ("false", "0", "no")
    want_async = str(async_mode or "false").lower() in ("true", "1", "yes")

    max_bytes = ctx.client.max_file_size_bytes or MAX_UPLOAD_BYTES
    tmp_dir = tempfile.mkdtemp(prefix="b2b_")
    ingested = None
    started = datetime.datetime.now(datetime.timezone.utc)

    try:
        ingested = ingest.save_upload(file, max_bytes=max_bytes, tmp_dir=tmp_dir)
        ingested.detected = detect_format(ingested.filename, ingested.head_bytes,
                                          ingested.path)

        idem_key = request.headers.get("Idempotency-Key")
        fp = idem_fingerprint(ingested.sha256, {
            "country": country_v, "currency": currency_v, "loan": loan,
            "include_transactions": want_txns,
        })
        outcome: IdempotencyOutcome = idem_begin(db, ctx.client, idem_key, fp)

        if outcome.replay:
            metering.record_usage(
                db, ctx, endpoint="/v1/analyze", method="POST", status_code=200,
                succeeded=True, request_id=outcome.request_id,
                detected_format=outcome.request.detected_format,
                transaction_count=outcome.request.transaction_count)
            response.headers["Idempotent-Replay"] = "true"
            return _envelope(outcome.request, stored_result(outcome.request))

        req = outcome.request
        request.state.b2b_request_id = req.request_id
        rid = req.request_id

        req.filename = ingested.filename
        req.file_size_bytes = ingested.size_bytes
        req.file_sha256 = ingested.sha256
        req.detected_format = ingested.detected.format
        req.country = country_v
        req.currency = currency_v
        req.api_key_id = ctx.api_key.id
        db.commit()

        params = {"password": pdf_password or None, "country": country_v,
                  "currency": currency_v, "loan": loan,
                  "include_transactions": want_txns}

        if want_async:
            # Hand the temp dir to the worker; it owns cleanup from here.
            background.add_task(_process, req.request_id, ingested.path, tmp_dir,
                                ingested.filename, ingested.sha256,
                                ingested.head_bytes, params,
                                ctx.client.result_retention_hours)
            ingested = None                       # do not clean up in `finally`
            metering.record_usage(db, ctx, endpoint="/v1/analyze", method="POST",
                                  status_code=202, succeeded=True,
                                  request_id=req.request_id,
                                  file_size_bytes=req.file_size_bytes,
                                  detected_format=req.detected_format)
            response.status_code = 202
            return {"request_id": req.request_id, "status": "processing",
                    "poll_url": f"/v1/analyze/{req.request_id}"}

        try:
            result = run_analysis(ingested, request_id=req.request_id, **params)
        except ApiError as exc:
            idem_fail(db, req, exc.code, exc.message)
            metering.record_usage(db, ctx, endpoint="/v1/analyze", method="POST",
                                  status_code=exc.status_code, succeeded=False,
                                  request_id=req.request_id, error_code=exc.code,
                                  file_size_bytes=req.file_size_bytes,
                                  detected_format=req.detected_format)
            raise

        idem_complete(db, req, result.payload,
                      transaction_count=result.transaction_count,
                      detected_format=result.detected_format,
                      overall_confidence=result.overall_confidence,
                      duration_ms=result.duration_ms,
                      retention_hours=ctx.client.result_retention_hours)
        metering.record_usage(db, ctx, endpoint="/v1/analyze", method="POST",
                              status_code=200, succeeded=True,
                              request_id=req.request_id, file_processed=True,
                              file_size_bytes=req.file_size_bytes,
                              detected_format=result.detected_format,
                              transaction_count=result.transaction_count,
                              duration_ms=result.duration_ms)
        return _envelope(req, result.payload)

    finally:
        if ingested is not None:
            ingest.cleanup(ingested)
        elif not want_async:
            try:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:                      # noqa: BLE001
                pass
        del started


@router.get("/analyze/{request_id}", summary="Fetch the result of an analysis")
def get_analysis(request_id: str,
                 ctx: AuthContext = Depends(api_key_auth),
                 db: Session = Depends(get_db)):
    require_scope(ctx, SCOPE_READ)

    req = db.query(AnalysisRequest).filter(
        AnalysisRequest.request_id == request_id,
        # Scoped to the caller: a request id is opaque, but an integrator must
        # never be able to read another client's analysis by guessing one.
        AnalysisRequest.client_id == ctx.client.id).first()
    if req is None:
        raise ApiError(errors.REQUEST_NOT_FOUND,
                       "No analysis request with that id exists for this client.")

    payload = stored_result(req)
    if req.status == RequestStatus.COMPLETED and payload is None:
        # Retention has cleared it. Say so rather than returning an empty result.
        body = _envelope(req)
        body["status"] = "expired"
        body["message"] = ("The result has passed its retention window and is "
                           "no longer stored. Submit the file again to re-analyse it.")
        return JSONResponse(status_code=410, content=body)

    return _envelope(req, payload)


@router.get("/usage", summary="This client's API usage")
def usage(days: int = 30,
          ctx: AuthContext = Depends(api_key_auth),
          db: Session = Depends(get_db)):
    require_scope(ctx, SCOPE_READ)
    days = max(1, min(int(days or 30), 365))
    until = datetime.datetime.now(datetime.timezone.utc)
    since = until - datetime.timedelta(days=days)
    return {"client": ctx.client.slug, "period_days": days,
            "since": since.isoformat(), "until": until.isoformat(),
            **metering.usage_summary(db, ctx.client.id, since, until)}
