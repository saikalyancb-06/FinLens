"""Background housekeeping for the B2B API.

Three jobs the API promised in its docs but that nothing was running:

1. **Result retention.** `analysis_requests.result` holds the full analysis —
   every transaction and narration of a client's bank statement. Each row got a
   `result_expires_at`, but nothing ever cleared it, so statement data
   accumulated in Postgres indefinitely and `GET /v1/analyze/{id}` kept serving
   it long after the documented 410. `purge_expired_results` drops the payload
   (keeping the idempotency fingerprint, so key-reuse detection still works) and
   the request row itself — the audit/billing record — stays.

2. **Webhook retries.** `webhooks.deliver` schedules `next_attempt_at` with
   backoff after a failed attempt, and `due_deliveries` finds those rows, but
   no worker ever polled it: a receiver that was down for the first attempt
   never got the event. `retry_due_webhooks` is that worker.

3. **Stuck requests.** Async analysis runs in-process. A restart mid-analysis
   left the row in `processing` forever, and because the idempotency key stayed
   claimed, the client's retry with the same key got REQUEST_IN_PROGRESS
   forever. `reap_stuck_requests` marks such rows failed and releases the key.

All three are idempotent and safe to run on every tick. They run from the
application lifespan (main.py), like the FX refresher: the deployment is a
single instance by design (see render.yaml), so one loop is the whole worker.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
from typing import Optional

from sqlalchemy.orm import Session

from app.b2b import errors
from app.b2b.idempotency import _stored_fingerprint, _wrap_result
from app.b2b.models import AnalysisRequest, RequestStatus

logger = logging.getLogger("b2b.maintenance")

#: How often the loop runs. Retention is hour-grained and webhook backoff
#: starts at 30s, so a minute is fine-grained enough for both.
INTERVAL_SECONDS = int(os.getenv("B2B_MAINTENANCE_INTERVAL_SECONDS", "60"))

#: A request still `processing` after this long is not going to finish. The
#: slowest legitimate case — a long scanned PDF through OCR — is minutes.
STUCK_AFTER_MINUTES = int(os.getenv("B2B_STUCK_REQUEST_MINUTES", "30"))


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def purge_expired_results(db: Session, now: Optional[datetime.datetime] = None,
                          batch: int = 500) -> int:
    """Drop stored payloads whose retention has passed. Returns rows purged."""
    now = now or _now()
    rows = (db.query(AnalysisRequest)
              .filter(AnalysisRequest.result_expires_at.isnot(None),
                      AnalysisRequest.result_expires_at <= now)
              .limit(batch).all())
    for req in rows:
        req.result = _wrap_result(None, _stored_fingerprint(req))
        # Cleared so the row drops out of this query; `stored_result` returns
        # None for it from now on, and the router answers 410.
        req.result_expires_at = None
    if rows:
        db.commit()
    return len(rows)


def reap_stuck_requests(db: Session, now: Optional[datetime.datetime] = None,
                        older_than_minutes: int = STUCK_AFTER_MINUTES) -> int:
    """Fail requests abandoned mid-processing and release their idempotency key."""
    now = now or _now()
    cutoff = now - datetime.timedelta(minutes=older_than_minutes)
    rows = (db.query(AnalysisRequest)
              .filter(AnalysisRequest.status.in_([RequestStatus.QUEUED,
                                                  RequestStatus.PROCESSING]),
                      AnalysisRequest.created_at < cutoff)
              .all())
    for req in rows:
        req.status = RequestStatus.FAILED
        req.error_code = errors.SERVICE_UNAVAILABLE
        req.error_message = ("Processing was interrupted before it finished. "
                             "Submit the file again.")
        req.completed_at = now
        # Releasing the key lets the client's retry with the SAME key start a
        # fresh analysis instead of being told REQUEST_IN_PROGRESS forever, or
        # replaying an interruption that was never their fault.
        req.idempotency_key = None
    if rows:
        db.commit()
    return len(rows)


def retry_due_webhooks(limit: int = 50) -> int:
    """Re-attempt deliveries whose backoff has elapsed. Returns attempts made."""
    from app.b2b import webhooks
    from app.database.session import SessionLocal

    db = SessionLocal()
    try:
        ids = [d.id for d in webhooks.due_deliveries(db, limit=limit)]
    finally:
        db.close()
    for delivery_id in ids:
        # Own session per delivery, exactly as the first attempt does.
        webhooks.deliver_sync(delivery_id)
    return len(ids)


def run_once() -> dict:
    """One maintenance pass. Each job isolated: one failing must not stop the rest."""
    from app.database.session import SessionLocal

    done = {"purged": 0, "reaped": 0, "webhooks_retried": 0}
    db = SessionLocal()
    try:
        try:
            done["purged"] = purge_expired_results(db)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("[b2b] result purge failed")
        try:
            done["reaped"] = reap_stuck_requests(db)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("[b2b] stuck-request reaping failed")
    finally:
        db.close()
    try:
        done["webhooks_retried"] = retry_due_webhooks()
    except Exception:  # noqa: BLE001
        logger.exception("[b2b] webhook retry failed")
    if any(done.values()):
        logger.info("[b2b] maintenance: %s", done)
    return done


async def maintenance_loop(stop_event: asyncio.Event,
                           interval: int = INTERVAL_SECONDS) -> None:
    """Run `run_once` every `interval` seconds until `stop_event` is set.

    The work is synchronous database and HTTP code, so it runs in a thread and
    never blocks the event loop that is serving requests.
    """
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(run_once)
        except Exception:  # noqa: BLE001
            logger.exception("[b2b] maintenance pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
