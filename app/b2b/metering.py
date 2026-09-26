"""Usage metering — one row per countable event, and the aggregates over them.

WHY THIS IS SEPARATE FROM ``analysis_requests``
-----------------------------------------------
``analysis_requests`` records *what happened*, including the result payload, and
retention deletes that payload on a schedule. ``api_usage_records`` records
*what to count*, and nothing deletes it. A bill built from the first table would
shrink as retention ran; a bill built from this one is stable, which is the
whole reason the two exist.

WHY NOTHING HERE MAY RAISE
--------------------------
Metering is bookkeeping that happens *after* the client's work is done. If
:func:`record_usage` raised — a lost connection, a deadlock, a value too long
for a column — the caller would turn a successful analysis into a 500, and the
client would retry work we already did and already owe them the answer to. That
trade is never worth making: an unrecorded call costs us a fraction of a rupee,
while a failed call costs the integrator a customer interaction.

So every write is wrapped, every failure is logged at WARNING with the request
id, and the function returns None. The log line is the recovery path: a gap in
metering is visible and reconstructible from the request rows, whereas a 500
storm is not recoverable at all.
"""
from __future__ import annotations

import datetime
import logging
import math
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import Integer, func
from sqlalchemy.orm import Session

from app.b2b.models import UsageRecord

logger = logging.getLogger(__name__)


def _client_and_key_ids(ctx) -> tuple:
    """Pull ids out of an AuthContext, an ApiClient, or a bare uuid.

    Accepting all three keeps the call site in the router short and means a
    background job that has only a client id can meter too.
    """
    if ctx is None:
        return None, None
    client = getattr(ctx, "client", None)
    if client is not None:
        return getattr(client, "id", None), getattr(getattr(ctx, "api_key", None), "id", None)
    if isinstance(ctx, uuid.UUID):
        return ctx, None
    return getattr(ctx, "id", None), None


def record_usage(
    db: Session,
    ctx,
    *,
    endpoint: str,
    method: str,
    status_code: int,
    succeeded: bool,
    request_id: Optional[str] = None,
    error_code: Optional[str] = None,
    file_processed: bool = False,
    file_size_bytes: Optional[int] = None,
    detected_format: Optional[str] = None,
    transaction_count: Optional[int] = None,
    duration_ms: Optional[int] = None,
    occurred_at: Optional[datetime.datetime] = None,
) -> Optional[UsageRecord]:
    """Write one usage row. Returns None on any failure, never raises."""
    try:
        client_id, api_key_id = _client_and_key_ids(ctx)
        if client_id is None:
            # Unauthenticated traffic (a 401 before a key was resolved) has no
            # client to bill. It is counted by the HTTP access log, not here.
            return None

        record = UsageRecord(
            id=uuid.uuid4(),
            client_id=client_id,
            api_key_id=api_key_id,
            request_id=request_id,
            endpoint=endpoint[:120],
            method=(method or "POST")[:10],
            status_code=int(status_code),
            succeeded=bool(succeeded),
            error_code=(error_code[:64] if error_code else None),
            file_processed=bool(file_processed),
            file_size_bytes=file_size_bytes,
            detected_format=(detected_format[:32] if detected_format else None),
            transaction_count=transaction_count,
            duration_ms=duration_ms,
            occurred_at=occurred_at or datetime.datetime.now(datetime.timezone.utc),
        )
        db.add(record)
        db.commit()
        return record
    except Exception:
        logger.warning(
            "[b2b-metering] failed to record usage for %s %s (request_id=%s); "
            "the request itself is unaffected.",
            method, endpoint, request_id, exc_info=True,
        )
        try:
            # The caller's session may be mid-transaction and is theirs, not
            # ours; leaving it in a failed state would break whatever it does
            # next. Rolling back is the only safe repair we can make here.
            db.rollback()
        except Exception:
            pass
        return None


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

def _percentile(values: List[int], fraction: float) -> Optional[float]:
    """Nearest-rank percentile.

    Nearest-rank rather than an interpolated one: an interpolated P95 invents a
    duration that no request actually took, and the point of this figure is to
    answer "how slow is a slow call for this client" with a real observation.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def usage_summary(db: Session, client_id, since: datetime.datetime,
                  until: datetime.datetime) -> Dict[str, Any]:
    """Aggregate one client's usage over ``[since, until)``.

    Half-open on purpose: consecutive periods queried back to back must not
    double-count the row that lands exactly on the boundary, which is precisely
    the row a monthly invoice run would hit.

    The counts come from SQL aggregates; the latency percentile is computed in
    Python from the non-null durations. Postgres could do it with
    ``percentile_cont``, but pulling the durations keeps this readable and the
    volume per client per month is thousands of integers, not millions.
    """
    if isinstance(client_id, str):
        client_id = uuid.UUID(client_id)
    since = _aware(since)
    until = _aware(until)

    base = (db.query(UsageRecord)
              .filter(UsageRecord.client_id == client_id,
                      UsageRecord.occurred_at >= since,
                      UsageRecord.occurred_at < until))

    totals = (db.query(
                func.count(UsageRecord.id),
                func.coalesce(func.sum(
                    func.cast(UsageRecord.succeeded, Integer)), 0),
                func.coalesce(func.sum(
                    func.cast(UsageRecord.file_processed, Integer)), 0),
                func.coalesce(func.sum(UsageRecord.file_size_bytes), 0),
                func.coalesce(func.sum(UsageRecord.transaction_count), 0),
              )
              .filter(UsageRecord.client_id == client_id,
                      UsageRecord.occurred_at >= since,
                      UsageRecord.occurred_at < until)
              .one())

    request_count = int(totals[0] or 0)
    successes = int(totals[1] or 0)
    files_processed = int(totals[2] or 0)
    total_bytes = int(totals[3] or 0)
    total_transactions = int(totals[4] or 0)

    durations = [int(d) for (d,) in
                 base.with_entities(UsageRecord.duration_ms)
                     .filter(UsageRecord.duration_ms.isnot(None)).all()
                 if d is not None]

    by_format: Dict[str, int] = {}
    for fmt, count in (db.query(UsageRecord.detected_format, func.count(UsageRecord.id))
                         .filter(UsageRecord.client_id == client_id,
                                 UsageRecord.occurred_at >= since,
                                 UsageRecord.occurred_at < until,
                                 UsageRecord.detected_format.isnot(None))
                         .group_by(UsageRecord.detected_format).all()):
        by_format[str(fmt)] = int(count)

    by_error: Dict[str, int] = {}
    for code, count in (db.query(UsageRecord.error_code, func.count(UsageRecord.id))
                          .filter(UsageRecord.client_id == client_id,
                                  UsageRecord.occurred_at >= since,
                                  UsageRecord.occurred_at < until,
                                  UsageRecord.error_code.isnot(None))
                          .group_by(UsageRecord.error_code).all()):
        by_error[str(code)] = int(count)

    return {
        "client_id": str(client_id),
        "since": since.isoformat(),
        "until": until.isoformat(),
        "request_count": request_count,
        "successes": successes,
        # Derived rather than summed separately, so the two can never disagree.
        "failures": request_count - successes,
        "files_processed": files_processed,
        "total_file_bytes": total_bytes,
        "total_transactions": total_transactions,
        "mean_duration_ms": (round(sum(durations) / len(durations), 2)
                             if durations else None),
        "p95_duration_ms": _percentile(durations, 0.95),
        "by_format": by_format,
        "by_error_code": by_error,
    }


def _aware(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


__all__ = ["record_usage", "usage_summary"]
