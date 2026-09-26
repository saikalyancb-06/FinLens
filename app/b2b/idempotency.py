"""Idempotency for POST /v1/analyze.

THE PROBLEM
-----------
An integrator's HTTP client times out at 30s. Our parse took 32s and succeeded.
The client retries. Without idempotency that is two parses, two usage records,
two bills and — if the caller is a lender pulling a bureau-adjacent decision —
two different answers to the same question, because analysis is not bit-exact
across runs. The retry is the correct client behaviour; making it safe is our
job, not theirs.

THE THREE ANSWERS
-----------------
An ``Idempotency-Key`` that has been seen before can mean three different
things, and conflating them is what makes idempotency implementations
frustrating to integrate against:

1. *Same key, same request, finished* — the first answer is what they want.
   Replay it verbatim. No re-parse, no second usage record.
2. *Same key, same request, still running* — they retried before we finished.
   409 REQUEST_IN_PROGRESS with the request id, so they can poll instead of
   piling on. Not a 202 with a new id: that would hand them a second handle to
   the same work.
3. *Same key, DIFFERENT request* — a client bug, almost always a key generated
   once per process or per loop iteration instead of per logical operation.
   409 IDEMPOTENCY_KEY_REUSED, loudly. The tempting alternative — quietly
   treating it as a new request — turns "your retry key is wrong" into "your
   customer got someone else's statement analysed under their key", and it is
   silent until it is a support incident.

THE CONCURRENCY GUARD IS THE DATABASE
-------------------------------------
Two retries can arrive on two workers within microseconds of each other, so
SELECT-then-INSERT loses: both selects miss, both insert, both parse. The guard
is the ``uq_b2b_client_idempotency`` unique index — we attempt the INSERT and let
PostgreSQL arbitrate. Exactly one transaction wins; the loser catches
IntegrityError, re-reads the winner's row and answers from it. There is no
window, because the check and the claim are the same operation.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.b2b import errors as err
from app.b2b.errors import ApiError
from app.b2b.models import AnalysisRequest, ApiClient, RequestStatus

logger = logging.getLogger(__name__)

#: Reserved key inside ``AnalysisRequest.result``. The fingerprint has to live
#: somewhere durable and the schema is fixed, so it rides in the result JSON
#: under a namespaced key that cannot collide with an analysis payload field.
#: :func:`stored_result` unwraps it again, so nothing above this module has to
#: know the envelope exists.
_ENVELOPE_KEY = "_kredo_idempotency"


# --------------------------------------------------------------------------- #
# Fingerprinting
# --------------------------------------------------------------------------- #

def fingerprint(file_sha256: Optional[str],
                params: Optional[Mapping[str, Any]] = None) -> str:
    """``sha256(file hash + sorted analysis params)``.

    Sorted, because a dict's iteration order is not part of what the caller
    asked for: ``{"country": "IN", "currency": "INR"}`` and the same two keys the
    other way round are the same request, and a fingerprint that disagreed would
    reject an honest retry as key reuse.

    None-valued params are dropped rather than serialised as ``null``, so a
    client that stops sending an optional field it was never setting does not
    suddenly collide with itself.
    """
    cleaned: Dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        cleaned[str(key)] = value
    canonical = json.dumps(cleaned, sort_keys=True, separators=(",", ":"),
                           default=str)
    material = f"{file_sha256 or ''}|{canonical}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _wrap_result(payload: Any, fp: Optional[str]) -> Any:
    if fp is None:
        return payload
    return {_ENVELOPE_KEY: {"fingerprint": fp}, "payload": payload}


def stored_result(request: Optional[AnalysisRequest]) -> Any:
    """The analysis payload held by a request row, envelope removed."""
    if request is None or request.result is None:
        return None
    result = request.result
    if isinstance(result, dict) and _ENVELOPE_KEY in result:
        return result.get("payload")
    return result


def _stored_fingerprint(request: AnalysisRequest) -> Optional[str]:
    result = request.result
    if isinstance(result, dict) and isinstance(result.get(_ENVELOPE_KEY), dict):
        fp = result[_ENVELOPE_KEY].get("fingerprint")
        return str(fp) if fp else None
    return None


# --------------------------------------------------------------------------- #
# Outcome
# --------------------------------------------------------------------------- #

@dataclass
class IdempotencyOutcome:
    """What the router should do next.

    ``replay`` True  -> return ``result`` (or the recorded error) as-is.
    ``replay`` False -> do the work, then call :func:`complete` or :func:`fail`
                        on ``request`` (which is None only when no key was sent).
    """

    replay: bool
    request: Optional[AnalysisRequest] = None
    result: Any = None
    idempotency_key: Optional[str] = None
    fingerprint: Optional[str] = None

    @property
    def request_id(self) -> Optional[str]:
        return getattr(self.request, "request_id", None)


def _new_request_id() -> str:
    # Prefixed and opaque. The integrator quotes this in a support ticket, so it
    # is short enough to copy by hand and carries no client identity.
    return f"req_{uuid.uuid4().hex}"


# --------------------------------------------------------------------------- #
# begin
# --------------------------------------------------------------------------- #

def begin(
    db: Session,
    client: ApiClient,
    idempotency_key: Optional[str],
    request_fingerprint: Optional[str],
    *,
    api_key_id: Optional[uuid.UUID] = None,
    filename: Optional[str] = None,
    file_sha256: Optional[str] = None,
    file_size_bytes: Optional[int] = None,
    country: Optional[str] = None,
    currency: Optional[str] = None,
) -> IdempotencyOutcome:
    """Claim ``idempotency_key`` for this request, or answer from a prior one.

    Returns an :class:`IdempotencyOutcome`; raises REQUEST_IN_PROGRESS or
    IDEMPOTENCY_KEY_REUSED for the two conflict cases.
    """
    key = (idempotency_key or "").strip() or None

    if key is None:
        # No key: nothing to deduplicate against. A request row is still created
        # so the work has an id and a status to poll, but it claims no key and a
        # retry will parse again — which is exactly what "no idempotency key"
        # asks for.
        request = _create_request(
            db, client, None, request_fingerprint,
            api_key_id=api_key_id, filename=filename, file_sha256=file_sha256,
            file_size_bytes=file_size_bytes, country=country, currency=currency,
        )
        return IdempotencyOutcome(replay=False, request=request,
                                  fingerprint=request_fingerprint)

    if len(key) > 128:
        # The column is String(128); rejecting here gives the integrator a clear
        # 400-family answer instead of a database error rendered as a 500.
        raise ApiError(
            err.INVALID_PARAMETER,
            "Idempotency-Key must be at most 128 characters.",
        )

    try:
        request = _create_request(
            db, client, key, request_fingerprint,
            api_key_id=api_key_id, filename=filename, file_sha256=file_sha256,
            file_size_bytes=file_size_bytes, country=country, currency=currency,
        )
    except IntegrityError:
        # Lost the race (or this is a plain retry). Roll back the failed INSERT —
        # the session is unusable until we do — and answer from the winner.
        db.rollback()
        existing = _lookup(db, client, key)
        if existing is None:  # pragma: no cover - would mean the row vanished
            raise ApiError(
                err.INTERNAL_ERROR,
                "Could not resolve the idempotency key. Retry with a new key.",
            )
        return _replay_or_conflict(existing, request_fingerprint)

    return IdempotencyOutcome(replay=False, request=request,
                              idempotency_key=key,
                              fingerprint=request_fingerprint)


def _lookup(db: Session, client: ApiClient, key: str) -> Optional[AnalysisRequest]:
    return (db.query(AnalysisRequest)
              .filter(AnalysisRequest.client_id == client.id,
                      AnalysisRequest.idempotency_key == key)
              .first())


def _create_request(
    db: Session,
    client: ApiClient,
    key: Optional[str],
    fp: Optional[str],
    *,
    api_key_id: Optional[uuid.UUID],
    filename: Optional[str],
    file_sha256: Optional[str],
    file_size_bytes: Optional[int],
    country: Optional[str],
    currency: Optional[str],
) -> AnalysisRequest:
    """INSERT the claim. Raises IntegrityError if the key is already taken."""
    request = AnalysisRequest(
        id=uuid.uuid4(),
        request_id=_new_request_id(),
        client_id=client.id,
        api_key_id=api_key_id,
        idempotency_key=key,
        # PROCESSING, not QUEUED: the row exists because work is starting now.
        # A concurrent retry must see "in progress", and QUEUED would read as
        # "nothing has happened yet, go ahead".
        status=RequestStatus.PROCESSING,
        filename=filename,
        file_sha256=file_sha256,
        file_size_bytes=file_size_bytes,
        country=country,
        currency=currency,
        result=_wrap_result(None, fp),
    )
    db.add(request)
    # Committed rather than flushed. The claim has to be visible to the other
    # worker's SELECT the moment it exists — a flush inside an open transaction
    # is invisible outside it, so the second worker would block on the unique
    # index until the first request's whole parse finished, holding a connection
    # for the duration.
    db.commit()
    db.refresh(request)
    return request


def _replay_or_conflict(existing: AnalysisRequest,
                        fp: Optional[str]) -> IdempotencyOutcome:
    """Apply the decision table to a row that already holds the key."""
    stored_fp = _stored_fingerprint(existing)

    # Mismatch is checked FIRST. A different request under the same key is a bug
    # whichever state the original is in, and reporting "still processing" for it
    # would send the integrator to look at our latency instead of their key.
    if stored_fp is not None and fp is not None and stored_fp != fp:
        raise ApiError(
            err.IDEMPOTENCY_KEY_REUSED,
            "This Idempotency-Key was already used for a different request. "
            "Generate one key per logical operation and reuse it only to retry "
            "that exact operation.",
            detail={"request_id": existing.request_id,
                    "original_created_at": _iso(existing.created_at)},
        )
    # stored_fp is None when the row predates the fingerprint (retention has
    # cleared the result, or the router replaced it wholesale). We cannot prove
    # reuse then, so we fall back to key-only semantics: replaying is safe, a
    # false IDEMPOTENCY_KEY_REUSED against an honest retry is not.

    if existing.status in (RequestStatus.QUEUED, RequestStatus.PROCESSING):
        raise ApiError(
            err.REQUEST_IN_PROGRESS,
            "A request with this Idempotency-Key is still being processed. "
            f"Poll /v1/requests/{existing.request_id} for the result.",
            detail={"request_id": existing.request_id,
                    "status": existing.status.value},
        )

    # COMPLETED or FAILED: both replay. A recorded failure is an answer, and
    # re-running it would charge the client twice for the same rejection.
    return IdempotencyOutcome(
        replay=True,
        request=existing,
        result=stored_result(existing),
        idempotency_key=existing.idempotency_key,
        fingerprint=stored_fp,
    )


# --------------------------------------------------------------------------- #
# Finishing a request
# --------------------------------------------------------------------------- #

def complete(db: Session, request: AnalysisRequest, result: Any, *,
             transaction_count: Optional[int] = None,
             detected_format: Optional[str] = None,
             overall_confidence: Optional[float] = None,
             duration_ms: Optional[int] = None,
             retention_hours: Optional[int] = None) -> AnalysisRequest:
    """Mark a request completed and store its result for replay.

    Use this rather than assigning ``request.result`` directly: it preserves the
    fingerprint envelope, without which a later retry cannot be distinguished
    from key reuse.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    fp = _stored_fingerprint(request)
    request.result = _wrap_result(result, fp)
    request.status = RequestStatus.COMPLETED
    request.completed_at = now
    if transaction_count is not None:
        request.transaction_count = transaction_count
    if detected_format is not None:
        request.detected_format = detected_format
    if overall_confidence is not None:
        request.overall_confidence = overall_confidence
    if duration_ms is not None:
        request.duration_ms = duration_ms
    if retention_hours is not None and retention_hours > 0:
        request.result_expires_at = now + datetime.timedelta(hours=retention_hours)
    db.commit()
    db.refresh(request)
    return request


def fail(db: Session, request: AnalysisRequest, error_code: str,
         error_message: str, *, duration_ms: Optional[int] = None
         ) -> AnalysisRequest:
    """Mark a request failed, keeping the fingerprint so a retry still replays."""
    request.status = RequestStatus.FAILED
    request.error_code = error_code
    # Whatever the caller passes; ``errors.py`` already guarantees these
    # messages carry no traceback, path or SQL.
    request.error_message = error_message
    request.completed_at = datetime.datetime.now(datetime.timezone.utc)
    if duration_ms is not None:
        request.duration_ms = duration_ms
    db.commit()
    db.refresh(request)
    return request


def _iso(value: Optional[datetime.datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


__all__ = [
    "IdempotencyOutcome", "begin", "complete", "fail", "fingerprint",
    "stored_result",
]
