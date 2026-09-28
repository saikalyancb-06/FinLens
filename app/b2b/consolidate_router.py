"""POST /v1/statements/consolidate — many statements in, one reconciled list out.

The multi-file counterpart of POST /v1/analyze, built for the Credit Lens
"bank statement -> JSON" requirement:

* one JSON record per transaction with Bank Account No., Bank Name, Date,
  Narration, Amount, Type (Money In / Money Out), Category 1 and Category 2;
* Check 1: duplicates across overlapping statements removed (Account + Date +
  Narration + Amount + Balance), internal transfers between the supplied
  accounts tagged, not removed;
* Check 2: running-balance continuity per account across and within
  statements, and each statement's closing balance, with every break flagged
  by date and difference.

Same authentication, scopes, rate limiting, idempotency, async mode, metering
and error envelope as /v1/analyze. The result is also retrievable with
GET /v1/statements/consolidate/{request_id} (or GET /v1/analyze/{request_id}).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.b2b import errors, ingest, metering, ratelimit
from app.b2b.auth import AuthContext, api_key_auth, require_any_scope, require_scope
from app.b2b.errors import ApiError
from app.b2b.idempotency import (
    begin as idem_begin, complete as idem_complete, fail as idem_fail, stored_result,
)
from app.b2b.models import AnalysisRequest, RequestStatus
from app.b2b.router import (MAX_UPLOAD_BYTES, SCOPE_CLASSIFY, SCOPE_READ, SCOPE_WRITE, _envelope,
                            _expired_response)
from app.database.session import SessionLocal, get_db

logger = logging.getLogger("b2b.consolidate.api")

router = APIRouter(prefix="/v1/statements", tags=["Financial Analysis API"])

MAX_FILES = int(os.getenv("B2B_CONSOLIDATE_MAX_FILES", "25"))
MAX_TOTAL_BYTES = int(os.getenv("B2B_CONSOLIDATE_MAX_TOTAL_BYTES", str(150 * 1024 * 1024)))


def _payload(result: Dict[str, Any]) -> Dict[str, Any]:
    s = result["summary"]
    return {"data": result,
            "quality": {"balance_check": s["balance_check"], "flags": s["flags"],
                        "duplicates_removed": s["duplicates_removed"],
                        "files_failed": s["files_failed"]}}


def _run(files: List[tuple], passwords: Dict[str, str], default_password: Optional[str],
         account_numbers: Optional[Dict[str, str]] = None, ruleset=None) -> Dict[str, Any]:
    from app.b2b.consolidate.service import consolidate
    from app.b2b.jobs import heavy_slot
    with heavy_slot():
        result = consolidate(files, passwords=passwords, default_password=default_password,
                             account_numbers=account_numbers, ruleset=ruleset)
    if result["summary"]["files_processed"] == 0:
        first = next((f.get("error") for f in result["files"] if f.get("error")), None) or {}
        raise ApiError(first.get("code") or errors.NO_TRANSACTIONS_FOUND,
                       "None of the files could be processed: " + (first.get("message") or ""),
                       detail={"files": result["files"]})
    return result


def _worker(request_id: str, files: List[tuple], tmp_dir: str, passwords: Dict[str, str],
            default_password: Optional[str], retention_hours: int,
            account_numbers: Optional[Dict[str, str]] = None, ruleset=None) -> None:
    db = SessionLocal()
    try:
        req = db.query(AnalysisRequest).filter(AnalysisRequest.request_id == request_id).first()
        if req is None:
            return
        started = datetime.datetime.now(datetime.timezone.utc)
        try:
            result = _run(files, passwords, default_password, account_numbers, ruleset)
        except ApiError as exc:
            idem_fail(db, req, exc.code, exc.message)
            return
        except Exception:                                 # noqa: BLE001
            logger.exception("[%s] consolidation failed", request_id)
            idem_fail(db, req, errors.INTERNAL_ERROR, "The consolidation failed unexpectedly.")
            return
        ms = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() * 1000)
        idem_complete(db, req, _payload(result), transaction_count=result["summary"]["transactions_output"],
                      detected_format="multi", duration_ms=ms, retention_hours=retention_hours)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        db.close()


@router.post("/consolidate", summary="Consolidate several bank statements into one reconciled list")
async def consolidate_endpoint(
    request: Request,
    response: Response,
    background: BackgroundTasks,
    files: List[UploadFile] = File(..., description="One or more statement files (repeat the 'files' field)."),
    passwords: Optional[str] = Form(None, description='JSON object {"file name": "password"}'),
    pdf_password: Optional[str] = Form(None, description="Password tried on every locked PDF."),
    rules: Optional[str] = Form(None, description="Optional ruleset JSON, same format as POST /v1/classify "
                                "(GET /v1/classify/schema). Matching rules set Category 1."),
    account_numbers: Optional[str] = Form(None, description='JSON {"file name": "account no"}, for files '
                                          'that do not print one (CSV/JSON exports)'),
    include_duplicates: Optional[str] = Form(None),
    async_mode: Optional[str] = Form(None),
    ctx: AuthContext = Depends(api_key_auth),
    db: Session = Depends(get_db),
):
    # Consolidation returns classified rows, the same kind of output as
    # /v1/classify, so a classify-only key may use it as well as an analyze key.
    require_any_scope(ctx, [SCOPE_CLASSIFY, SCOPE_WRITE])
    # Validate the ruleset before reading any upload: a typo in a rule should
    # cost one fast 400, not a 100 MB transfer.
    ruleset = None
    if rules:
        from app.b2b.rules import load_ruleset
        ruleset = load_ruleset(rules)
    state = ratelimit.check_and_consume(ctx.client)
    for k, v in ratelimit.headers_for(state).items():
        response.headers[k] = v

    uploads = [f for f in (files or []) if getattr(f, "filename", None)]
    if not uploads:
        raise ApiError(errors.MISSING_FILE, "Supply at least one file in the 'files' form field.")
    if len(uploads) > MAX_FILES:
        raise ApiError(errors.INVALID_PARAMETER, f"At most {MAX_FILES} files per request.",
                       detail={"received": len(uploads)})
    try:
        pw_map = json.loads(passwords) if passwords else {}
        if not isinstance(pw_map, dict):
            raise ValueError
    except ValueError:
        raise ApiError(errors.INVALID_PARAMETER,
                       "'passwords' must be a JSON object mapping file name to password.")
    try:
        acct_map = json.loads(account_numbers) if account_numbers else {}
        if not isinstance(acct_map, dict):
            raise ValueError
    except ValueError:
        raise ApiError(errors.INVALID_PARAMETER,
                       "'account_numbers' must be a JSON object mapping file name to account number.")
    want_dups = str(include_duplicates or "true").lower() not in ("false", "0", "no")
    want_async = str(async_mode or "false").lower() in ("true", "1", "yes")

    max_bytes = ctx.client.max_file_size_bytes or MAX_UPLOAD_BYTES
    tmp_dir = tempfile.mkdtemp(prefix="b2b_multi_")
    handed_off = False
    try:
        saved: List[tuple] = []
        total = 0
        for i, up in enumerate(uploads):
            sub = os.path.join(tmp_dir, f"f{i:02d}")
            os.makedirs(sub)
            ing = await run_in_threadpool(ingest.save_upload, up, max_bytes=max_bytes, tmp_dir=sub,
                                          allow_archive=True)
            if ing.detected is not None and ing.detected.format == "zip":
                # A ZIP of statements: every statement-like member joins the batch.
                from app.b2b.consolidate.archive import extract_zip
                for path, name in extract_zip(ing.path, sub, max_member_bytes=max_bytes,
                                              max_total_bytes=MAX_TOTAL_BYTES - total):
                    size = os.path.getsize(path)
                    total += size
                    with open(path, "rb") as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()
                    saved.append((path, name, digest, size))
                continue
            total += ing.size_bytes
            if total > MAX_TOTAL_BYTES:
                raise ApiError(errors.FILE_TOO_LARGE,
                               f"The files together exceed {MAX_TOTAL_BYTES // (1024 * 1024)} MB.")
            saved.append((ing.path, up.filename or ing.filename, ing.sha256, ing.size_bytes))
        if len(saved) > MAX_FILES * 4:
            raise ApiError(errors.INVALID_PARAMETER, f"At most {MAX_FILES * 4} statement files per request.")

        fp = hashlib.sha256(json.dumps({"files": sorted(s[2] for s in saved), "accounts": acct_map,
                                        "rules": hashlib.sha256((rules or "").encode()).hexdigest(),
                                        "dups": want_dups, "op": "consolidate"},
                                       sort_keys=True).encode()).hexdigest()
        outcome = idem_begin(db, ctx.client, request.headers.get("Idempotency-Key"), fp)
        if outcome.replay:
            response.headers["Idempotent-Replay"] = "true"
            replayed = stored_result(outcome.request)
            if outcome.request.status == RequestStatus.COMPLETED and replayed is None:
                return _expired_response(outcome.request, {"Idempotent-Replay": "true"})
            return _envelope(outcome.request, replayed)

        req = outcome.request
        request.state.b2b_request_id = req.request_id
        req.filename = ", ".join(s[1] for s in saved)[:255]
        req.file_size_bytes = total
        req.detected_format = "multi"
        req.api_key_id = ctx.api_key.id
        db.commit()
        pairs = [(s[0], s[1]) for s in saved]

        if want_async:
            background.add_task(_worker, req.request_id, pairs, tmp_dir, pw_map, pdf_password,
                                ctx.client.result_retention_hours, acct_map, ruleset)
            handed_off = True
            metering.record_usage(db, ctx, endpoint="/v1/statements/consolidate", method="POST",
                                  status_code=202, succeeded=True, request_id=req.request_id,
                                  file_size_bytes=total, detected_format="multi")
            response.status_code = 202
            return {"request_id": req.request_id, "status": "processing",
                    "poll_url": f"/v1/statements/consolidate/{req.request_id}"}

        started = datetime.datetime.now(datetime.timezone.utc)
        try:
            # CPU-bound for seconds to a minute (PDF extraction). Run in the
            # thread pool: called directly inside this async handler it froze
            # the event loop, so /health and every other client's request
            # waited for the whole batch (measured: /health took 17 s during a
            # 20 s consolidation), and a platform health check could restart
            # the instance mid-request.
            result = await run_in_threadpool(_run, pairs, pw_map, pdf_password, acct_map, ruleset)
        except ApiError as exc:
            idem_fail(db, req, exc.code, exc.message)
            metering.record_usage(db, ctx, endpoint="/v1/statements/consolidate", method="POST",
                                  status_code=exc.status_code, succeeded=False,
                                  request_id=req.request_id, error_code=exc.code)
            raise
        if not want_dups:
            result.pop("duplicates_removed", None)
        ms = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() * 1000)
        idem_complete(db, req, _payload(result), transaction_count=result["summary"]["transactions_output"],
                      detected_format="multi", duration_ms=ms,
                      retention_hours=ctx.client.result_retention_hours)
        metering.record_usage(db, ctx, endpoint="/v1/statements/consolidate", method="POST",
                              status_code=200, succeeded=True, request_id=req.request_id,
                              file_processed=True, file_size_bytes=total, detected_format="multi",
                              transaction_count=result["summary"]["transactions_output"], duration_ms=ms)
        return _envelope(req, _payload(result))
    finally:
        if not handed_off:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@router.get("/consolidate/{request_id}", summary="Fetch a consolidation result")
def get_consolidation(request_id: str, ctx: AuthContext = Depends(api_key_auth),
                      db: Session = Depends(get_db)):
    require_any_scope(ctx, [SCOPE_READ, SCOPE_CLASSIFY])
    req = db.query(AnalysisRequest).filter(
        AnalysisRequest.request_id == request_id,
        AnalysisRequest.client_id == ctx.client.id).first()
    if req is None:
        raise ApiError(errors.REQUEST_NOT_FOUND,
                       "No request with that id exists for this client.")
    payload = stored_result(req)
    if req.status == RequestStatus.COMPLETED and payload is None:
        return _expired_response(req)
    return _envelope(req, payload)
