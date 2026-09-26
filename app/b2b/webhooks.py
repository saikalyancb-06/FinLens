"""Outbound webhooks: signing, delivery and retry.

THE SIGNATURE SCHEME (copy this into your integration)
------------------------------------------------------
Every callback carries two headers::

    X-Kredo-Event-Id: evt_9f2c...          stable per event, for deduplication
    X-Kredo-Signature: t=1757404800,v1=3a5f...

``v1`` is ``HMAC-SHA256(secret, f"{t}.{raw_body}")``, hex encoded. Verify it as::

    import hmac, hashlib, time

    def verify(secret, header, body, tolerance=300):
        parts = dict(p.split("=", 1) for p in header.split(","))
        t, sig = int(parts["t"]), parts["v1"]
        if abs(time.time() - t) > tolerance:
            return False                      # replay
        expected = hmac.new(secret.encode(), f"{t}.{body}".encode(),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

Three properties are doing real work here, and dropping any one of them breaks
the scheme:

* **The timestamp is inside the signed material.** If it were only a header, an
  attacker who captured one delivery could replay it forever with a fresh ``t``.
  Signing ``t.body`` binds the two together, so changing either invalidates the
  digest.
* **The tolerance window.** The signature stays valid for as long as the secret
  does, so without a freshness check a captured-and-replayed payload — "this
  applicant's statement passed" — remains verifiable years later. Five minutes
  is enough for a slow network and a queued retry, and short enough that a
  captured request is not a durable capability.
* **``compare_digest``, not ``==``.** String equality returns early at the first
  differing byte. Over enough attempts that timing difference lets an attacker
  build a valid signature one character at a time. This costs nothing to avoid.

The receiver must verify against the RAW REQUEST BODY, byte for byte, before
parsing it. Re-serialising the JSON changes key order and whitespace, and the
signature will not match — that is the single most common integration failure
with schemes like this one, and it is the correct behaviour: it is what makes
the signature cover exactly what was sent.

RETRY POLICY
------------
5xx and timeouts are retried with exponential backoff — the receiver is down or
slow, and it will probably be up later. 429 is retried too, for the same reason.
Every other 4xx is NOT retried: a 400, 404 or 410 means the endpoint has decided
this request is wrong, and repeating it unchanged is a guaranteed waste that
degrades into hammering an endpoint whose owner has already told us to stop.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any, Optional, Tuple

from sqlalchemy.orm import Session

from app.b2b.models import ApiClient, WebhookDelivery

logger = logging.getLogger(__name__)

#: Seconds a signature stays acceptable. See the module docstring.
DEFAULT_TOLERANCE_SECONDS = 300

#: HTTP timeout for one delivery attempt. A webhook receiver that takes longer
#: than this is not going to answer; holding the connection open only ties up a
#: worker that has other deliveries to make.
DELIVERY_TIMEOUT_SECONDS = 10.0

#: Attempts are capped. An endpoint that has failed this many times over the
#: backoff schedule below is broken, not busy, and an uncapped retry loop turns
#: our queue into a permanent source of load on somebody else's server.
MAX_ATTEMPTS = 6

#: Backoff base in seconds: 30s, 1m, 2m, 4m, 8m, then give up. Doubling rather
#: than a fixed delay so a receiver coming back from an outage is not hit by the
#: whole backlog at once.
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600

_RETRYABLE_4XX = {429}


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #

def sign_payload(secret: str, timestamp: int, body: str) -> str:
    """``HMAC-SHA256(secret, "{timestamp}.{body}")`` as hex."""
    material = f"{timestamp}.{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


def signature_header(secret: str, body: str,
                     timestamp: Optional[int] = None) -> str:
    """Build the ``X-Kredo-Signature`` value.

    The ``v1`` label is a version, and it is there so a future scheme can be
    introduced by adding ``v2=`` alongside it: receivers that only know v1 keep
    working through the migration instead of breaking on a flag day.
    """
    ts = int(timestamp if timestamp is not None else time.time())
    return f"t={ts},v1={sign_payload(secret, ts, body)}"


def _parse_signature_header(header: str) -> Tuple[Optional[int], Optional[str]]:
    ts: Optional[int] = None
    sig: Optional[str] = None
    for part in (header or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        label, _, value = part.partition("=")
        label = label.strip()
        if label == "t":
            try:
                ts = int(value.strip())
            except ValueError:
                return None, None
        elif label == "v1":
            sig = value.strip()
    return ts, sig


def verify_signature(secret: str, header: str, body: str,
                     tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS) -> bool:
    """Verify a signature header against the raw body. Never raises.

    Returns False for anything wrong — malformed header, stale timestamp, bad
    digest — rather than distinguishing them. A receiver that reported *why*
    verification failed would be telling an attacker whether their forged digest
    was the right length, the right format, or merely wrong.
    """
    if not secret or not header or body is None:
        return False
    try:
        ts, sig = _parse_signature_header(header)
        if ts is None or not sig:
            return False

        # Absolute difference, so a timestamp from the future is rejected too:
        # a clock-skewed or attacker-chosen ``t`` far ahead would otherwise
        # produce a signature that stays valid indefinitely.
        if tolerance_seconds is not None and abs(time.time() - ts) > tolerance_seconds:
            return False

        expected = sign_payload(secret, ts, body)
        return hmac.compare_digest(expected, sig)
    except Exception:  # pragma: no cover - defensive; verification never throws
        return False


# --------------------------------------------------------------------------- #
# Queueing
# --------------------------------------------------------------------------- #

def event_id_for(client_id, event_type: str, request_id: Optional[str]) -> str:
    """A STABLE id for (client, event type, request).

    Stable, not random, because it is the receiver's deduplication key. Delivery
    is at-least-once by construction — a retry after a timeout may well be
    delivering something the receiver already processed — so the receiver needs
    a value that is identical across those attempts. A fresh uuid per attempt
    would look like a new event every time and would make double-processing the
    default rather than the exception.
    """
    material = f"{client_id}|{event_type}|{request_id or ''}"
    return "evt_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def canonical_body(payload: Any) -> str:
    """The exact JSON string that is signed and sent.

    Serialised once, here, and reused for both the signature and the request
    body. Signing one serialisation and sending another is the classic way to
    ship a webhook that never verifies.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def enqueue(db: Session, client: ApiClient, event_type: str,
            request_id: Optional[str], payload: Any) -> Optional[WebhookDelivery]:
    """Record a delivery to be attempted. Returns None if the client has no URL.

    If a delivery for the same event id already exists it is returned unchanged
    rather than duplicated — enqueueing twice for one event is a bug upstream,
    and creating a second row would send the receiver the same event twice with
    no way to tell they were the same.
    """
    url = (client.webhook_url or "").strip()
    if not url:
        return None

    event_id = event_id_for(client.id, event_type, request_id)
    existing = (db.query(WebhookDelivery)
                  .filter(WebhookDelivery.event_id == event_id).first())
    if existing is not None:
        return existing

    delivery = WebhookDelivery(
        id=uuid.uuid4(),
        event_id=event_id,
        client_id=client.id,
        request_id=request_id,
        event_type=event_type,
        url=url,
        payload=payload,
        attempts=0,
        delivered=False,
        next_attempt_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    return delivery


def _backoff_seconds(attempts: int) -> int:
    return min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))


def _schedule_retry(delivery: WebhookDelivery, error: str,
                    status_code: Optional[int] = None) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    delivery.last_error = error[:2000]
    delivery.last_status_code = status_code
    if delivery.attempts >= MAX_ATTEMPTS:
        # Give up. next_attempt_at is cleared so nothing picks it up again, and
        # the row survives as the record of why the integrator never got it.
        delivery.next_attempt_at = None
        logger.warning("[b2b-webhook] giving up on %s after %d attempts: %s",
                       delivery.event_id, delivery.attempts, error)
    else:
        delivery.next_attempt_at = now + datetime.timedelta(
            seconds=_backoff_seconds(delivery.attempts))


async def deliver(db: Session, delivery: WebhookDelivery) -> WebhookDelivery:
    """Attempt one delivery, recording the outcome. Never raises.

    One attempt per call. The scheduling loop (a worker polling
    ``next_attempt_at``) decides when the next one happens, which keeps the
    backoff in the database where it survives a restart rather than in a
    sleeping coroutine that a deploy would silently discard.
    """
    import httpx

    client_row = (db.query(ApiClient)
                    .filter(ApiClient.id == delivery.client_id).first())
    secret = (getattr(client_row, "webhook_secret", None) or "")

    body = canonical_body(delivery.payload)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Kredo-Webhooks/1",
        "X-Kredo-Event-Id": delivery.event_id,
        "X-Kredo-Event-Type": delivery.event_type,
    }
    if secret:
        headers["X-Kredo-Signature"] = signature_header(secret, body)
    else:
        # Unsigned delivery is a misconfiguration, not a feature: the receiver
        # has no way to tell our POST from anybody else's. Sent anyway (the
        # integrator asked for a callback) but recorded so it is visible.
        logger.warning("[b2b-webhook] client %s has a webhook URL but no secret; "
                       "delivery %s is UNSIGNED.",
                       delivery.client_id, delivery.event_id)

    delivery.attempts = (delivery.attempts or 0) + 1
    now = datetime.datetime.now(datetime.timezone.utc)

    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as http:
            # `content=`, not `json=`, so the bytes on the wire are exactly the
            # bytes that were signed.
            response = await http.post(delivery.url, content=body.encode("utf-8"),
                                       headers=headers)
        status = response.status_code
        delivery.last_status_code = status

        if 200 <= status < 300:
            delivery.delivered = True
            delivery.delivered_at = now
            delivery.next_attempt_at = None
            delivery.last_error = None
        elif status in _RETRYABLE_4XX or status >= 500:
            _schedule_retry(delivery, f"HTTP {status}", status)
        else:
            # 4xx other than 429: permanent. Retrying an endpoint that answered
            # 404 or 410 will produce the same answer every time.
            delivery.next_attempt_at = None
            delivery.last_error = f"HTTP {status} (not retried: client error)"
            logger.warning("[b2b-webhook] %s rejected by receiver with %d; "
                           "not retrying.", delivery.event_id, status)
    except Exception as exc:
        # Timeouts, DNS failures, TLS errors, connection resets — all transient
        # by nature, all retried. The class name only: an exception string from
        # an HTTP client can contain the full URL including a query string the
        # integrator put a token in.
        _schedule_retry(delivery, f"{exc.__class__.__name__}", None)

    try:
        db.commit()
        db.refresh(delivery)
    except Exception:  # pragma: no cover - bookkeeping must not raise upward
        logger.warning("[b2b-webhook] could not persist delivery state for %s",
                       delivery.event_id, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
    return delivery


def due_deliveries(db: Session, limit: int = 100):
    """Deliveries whose retry is due — what a background worker polls."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return (db.query(WebhookDelivery)
              .filter(WebhookDelivery.delivered.is_(False),
                      WebhookDelivery.next_attempt_at.isnot(None),
                      WebhookDelivery.next_attempt_at <= now,
                      WebhookDelivery.attempts < MAX_ATTEMPTS)
              .order_by(WebhookDelivery.next_attempt_at)
              .limit(limit).all())


__all__ = [
    "DEFAULT_TOLERANCE_SECONDS", "MAX_ATTEMPTS", "sign_payload",
    "signature_header", "verify_signature", "event_id_for", "canonical_body",
    "enqueue", "deliver", "due_deliveries",
]


def deliver_sync(delivery_id) -> None:
    """Run :func:`deliver` from a plain thread, with its own session.

    The router fires webhooks from a daemon thread rather than the request's
    event loop: a slow or hanging receiver must not hold the caller's HTTP
    response open, and a webhook failure must never change the outcome of the
    analysis that triggered it. Every error is swallowed here for that reason —
    the attempt, its status code and its error are already recorded on the
    WebhookDelivery row, which is the durable account of what happened.
    """
    import asyncio

    from app.database.session import SessionLocal

    db = SessionLocal()
    try:
        row = db.query(WebhookDelivery).filter(
            WebhookDelivery.id == delivery_id).first()
        if row is None:
            return
        asyncio.run(deliver(db, row))
    except Exception:  # noqa: BLE001
        logger.warning("webhook delivery thread failed", exc_info=True)
    finally:
        db.close()
