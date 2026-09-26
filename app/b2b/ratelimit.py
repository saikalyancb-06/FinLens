"""Per-client rate limiting and quota, over three fixed windows.

WHAT IS BEING PROTECTED
-----------------------
Two different things, which is why there are three windows and two error codes:

* the *minute* window protects this service. A client looping without backoff
  can saturate the parser pool and degrade every other tenant. Breaching it is
  RATE_LIMIT_EXCEEDED — "slow down, then carry on", and ``Retry-After`` is a
  handful of seconds.
* the *day* and *month* windows are the client's plan. Breaching one is
  QUOTA_EXCEEDED — "you have used what you bought", and no amount of backing off
  inside the window will change the answer. ``Retry-After`` is honest about
  that: it points at the start of the next window, which may be hours away.

Collapsing the two into one code would be a disservice to the integrator: the
correct client behaviour is completely different (retry with jitter vs. stop and
alert a human), and both would arrive as an indistinguishable 429.

DEGRADATION — REDIS IS AN ACCELERATOR, NEVER A DEPENDENCY
---------------------------------------------------------
This module follows ``app/services/cache.py``: Redis when it is confirmed
reachable, an in-process dictionary otherwise, and NEVER an exception out of
either. A limiter that 500s when Redis blinks has converted a capacity control
into an outage — it fails every request, including the ones well inside their
limit, which is strictly worse than not limiting at all for a few seconds.

The honest cost of the fallback: with N worker processes, each keeps its own
counters, so the effective ceiling is up to N x the configured limit while Redis
is down. That is the right trade here. The minute window exists to stop a
runaway client, and a runaway client is still stopped at N x limit; the day and
month windows are billing figures, and billing is reconciled from
``api_usage_records`` (see ``metering.py``), which is written to PostgreSQL and
does not depend on Redis at all. So an outage can let a client slightly
overshoot a quota; it cannot make the bill wrong, and it cannot take the API
down.
"""
from __future__ import annotations

import calendar
import datetime
import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.b2b import errors as err
from app.b2b.errors import ApiError

logger = logging.getLogger(__name__)

MINUTE = "minute"
DAY = "day"
MONTH = "month"

#: Key namespace, kept distinct from the cache's so a ``FLUSHDB`` of one is not
#: silently a reset of the other, and so both can share one hosted instance.
_NAMESPACE = "kredo:b2b:rl"

#: The in-process fallback is swept no more often than this. Same shape as the
#: sweep in ``main.py``'s IP limiter: an unbounded dict keyed by client id in a
#: long-lived worker is a slow memory leak, and a limiter that leaks memory
#: eventually takes down the process it was protecting.
_SWEEP_INTERVAL_SECONDS = 300.0

_memory_counters: Dict[str, Tuple[int, float]] = {}   # key -> (count, expires_at)
_memory_lock = threading.RLock()
_last_sweep = time.monotonic()


# --------------------------------------------------------------------------- #
# Window arithmetic
# --------------------------------------------------------------------------- #

@dataclass
class WindowState:
    """One window's verdict."""

    name: str
    limit: int
    used: int
    reset: int            # unix epoch at which this window rolls over

    @property
    def remaining(self) -> int:
        # Clamped at zero: a breached window reports 0, not a negative number.
        # Some client libraries treat the header as unsigned and wrap.
        return max(0, self.limit - self.used)

    @property
    def exceeded(self) -> bool:
        return self.used > self.limit

    @property
    def retry_after(self) -> int:
        return max(1, self.reset - int(time.time()))


@dataclass
class RateLimitState:
    """All three windows, after this request has been counted."""

    minute: WindowState
    day: WindowState
    month: WindowState
    #: Which backend answered — "redis" or "memory". Useful in a health probe.
    backend: str = "memory"

    @property
    def windows(self) -> List[WindowState]:
        return [self.minute, self.day, self.month]


def _window_bounds(now: Optional[datetime.datetime] = None
                   ) -> Dict[str, Tuple[str, int, int]]:
    """Return ``{window: (bucket_id, reset_epoch, ttl_seconds)}``.

    Windows are FIXED and aligned to the UTC calendar, not sliding. A sliding
    window is more precise but needs the full set of timestamps in the window to
    evaluate; a fixed window is one integer per bucket, which is what makes it a
    single INCR. The known cost is the boundary: a client can spend its whole
    minute allowance at 10:00:59 and the whole next one at 10:01:00. For a
    capacity control on file parsing that burst is survivable, and the day and
    month ceilings still hold.

    UTC everywhere, deliberately: a "day" that moved with a client's local
    timezone would make a month's quota depend on where the caller lives, and
    would give two clients different reset times for the same plan.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    now = now.astimezone(datetime.timezone.utc)
    epoch = int(now.timestamp())

    minute_start = epoch - (epoch % 60)
    minute_reset = minute_start + 60

    day_start = datetime.datetime(now.year, now.month, now.day,
                                  tzinfo=datetime.timezone.utc)
    day_reset = int((day_start + datetime.timedelta(days=1)).timestamp())

    days_in_month = calendar.monthrange(now.year, now.month)[1]
    month_start = datetime.datetime(now.year, now.month, 1,
                                    tzinfo=datetime.timezone.utc)
    month_reset = int((month_start
                       + datetime.timedelta(days=days_in_month)).timestamp())

    return {
        # bucket id                                   reset        ttl
        MINUTE: (now.strftime("%Y%m%d%H%M"), minute_reset, minute_reset - epoch + 60),
        DAY:    (now.strftime("%Y%m%d"),     day_reset,    day_reset - epoch + 3600),
        MONTH:  (now.strftime("%Y%m"),       month_reset,  month_reset - epoch + 3600),
    }


# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #

def _sweep_locked(now_mono: float) -> None:
    """Drop expired in-process counters. Caller holds the lock."""
    global _last_sweep
    if now_mono - _last_sweep < _SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep = now_mono
    dead = [k for k, (_, expires) in _memory_counters.items() if expires <= now_mono]
    for k in dead:
        _memory_counters.pop(k, None)


def _incr_memory(key: str, ttl_seconds: float) -> int:
    now_mono = time.monotonic()
    with _memory_lock:
        _sweep_locked(now_mono)
        count, expires = _memory_counters.get(key, (0, 0.0))
        if expires <= now_mono:
            count = 0
            expires = now_mono + ttl_seconds
        count += 1
        _memory_counters[key] = (count, expires)
        return count


def _incr_redis(keys_and_ttls: List[Tuple[str, float]]) -> Optional[List[int]]:
    """INCR each key and set its expiry, in one round trip. None on any failure.

    EXPIRE is issued unconditionally alongside INCR rather than only when the
    counter came back as 1. Deciding from the reply would need a second round
    trip, and the failure mode of getting it wrong is a counter with no TTL —
    which never resets, and permanently locks a client out of its own plan. An
    idempotent EXPIRE on every call costs one pipelined command and removes that
    possibility entirely.
    """
    try:
        from app.services.cache import cache

        backend = getattr(cache, "_redis", None)
        if backend is None:
            return None
        client = backend.client()
        pipe = client.pipeline(transaction=False)
        for key, ttl in keys_and_ttls:
            pipe.incr(key, 1)
            pipe.expire(key, max(1, int(ttl)))
        replies = pipe.execute()
        # Replies interleave INCR, EXPIRE, INCR, EXPIRE, ...
        return [int(replies[i]) for i in range(0, len(replies), 2)]
    except Exception as exc:
        logger.warning("[b2b-ratelimit] Redis unavailable (%s); counting in "
                       "process memory for this request.", exc.__class__.__name__)
        return None


def redis_available() -> bool:
    """Whether Redis is *confirmed* reachable, per the shared cache's probe.

    Reuses ``cache``'s background probe and circuit breaker rather than opening
    a socket here: discovering that nothing is listening costs a connect timeout,
    and paying that inside a rate-limit check would put seconds on the front of
    every request — the exact failure the cache module documents at length.
    """
    try:
        from app.services.cache import cache
        return bool(cache._redis_available())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# The check
# --------------------------------------------------------------------------- #

def check_and_consume(client, redis_ok: Optional[bool] = None,
                      *, now: Optional[datetime.datetime] = None) -> RateLimitState:
    """Count this request against all three windows and raise if any is over.

    ``redis_ok=None`` means "ask the shared cache". Passing an explicit value is
    how the caller (or a test) pins the backend.

    All three windows are incremented before any is evaluated, and a rejected
    request still consumes. That is deliberate: if a blocked call were refunded,
    a client in a tight retry loop would pay nothing for hammering us, the minute
    counter would sit exactly at the limit forever, and the cheap 429 path would
    become the client's steady state. Counting the attempt means a caller that
    ignores ``Retry-After`` stays blocked for the rest of the window instead of
    getting a free probe every millisecond.
    """
    bounds = _window_bounds(now)
    limits = {
        MINUTE: int(client.rate_limit_per_minute or 0),
        DAY: int(client.rate_limit_per_day or 0),
        MONTH: int(client.rate_limit_per_month or 0),
    }

    keys = {name: f"{_NAMESPACE}:{client.id}:{name[0]}:{bounds[name][0]}"
            for name in (MINUTE, DAY, MONTH)}

    use_redis = redis_available() if redis_ok is None else bool(redis_ok)
    counts: Optional[List[int]] = None
    backend = "memory"

    if use_redis:
        counts = _incr_redis([(keys[n], bounds[n][2]) for n in (MINUTE, DAY, MONTH)])
        if counts is not None:
            backend = "redis"

    if counts is None:
        # Fallback path. Note this runs both when Redis was never configured and
        # when it just failed mid-request — same code, so there is only one
        # behaviour to reason about.
        counts = [_incr_memory(keys[n], bounds[n][2]) for n in (MINUTE, DAY, MONTH)]

    state = RateLimitState(
        minute=WindowState(MINUTE, limits[MINUTE], counts[0], bounds[MINUTE][1]),
        day=WindowState(DAY, limits[DAY], counts[1], bounds[DAY][1]),
        month=WindowState(MONTH, limits[MONTH], counts[2], bounds[MONTH][1]),
        backend=backend,
    )

    # Evaluated narrowest-first so the message names the window the client can
    # actually do something about within the next few seconds.
    if state.minute.exceeded:
        raise ApiError(
            err.RATE_LIMIT_EXCEEDED,
            f"Rate limit of {state.minute.limit} requests per minute exceeded. "
            f"Retry in {state.minute.retry_after}s.",
            detail={"window": MINUTE, "limit": state.minute.limit,
                    "reset": state.minute.reset},
            headers=headers_for(state, breached=state.minute),
        )
    for window in (state.day, state.month):
        if window.exceeded:
            raise ApiError(
                err.QUOTA_EXCEEDED,
                f"Plan quota of {window.limit} requests per {window.name} exhausted. "
                f"It resets at {datetime.datetime.fromtimestamp(window.reset, datetime.timezone.utc).isoformat()}.",
                detail={"window": window.name, "limit": window.limit,
                        "reset": window.reset},
                headers=headers_for(state, breached=window),
            )

    return state


def headers_for(state: RateLimitState,
                breached: Optional[WindowState] = None) -> Dict[str, str]:
    """The ``X-RateLimit-*`` headers, for successful responses as well as 429s.

    The unsuffixed trio (``X-RateLimit-Limit`` / ``-Remaining`` / ``-Reset``)
    describes the window that is *closest to being hit*, not always the minute
    one. Most client libraries and dashboards read only those three, and showing
    them a comfortable per-minute figure while the monthly quota is two calls
    from exhaustion would be a genuinely misleading answer to "how much do I
    have left". The per-window suffixed headers carry the full picture for
    anyone who wants it.

    ``X-RateLimit-Reset`` is a unix epoch second, not a delta: a delta computed
    on our side and read after a slow hop is already wrong, and an absolute
    instant survives the journey.
    """
    tightest = breached or min(state.windows, key=lambda w: w.remaining)

    headers: Dict[str, str] = {
        "X-RateLimit-Limit": str(tightest.limit),
        "X-RateLimit-Remaining": str(tightest.remaining),
        "X-RateLimit-Reset": str(tightest.reset),
        "X-RateLimit-Window": tightest.name,
    }
    for window in state.windows:
        suffix = window.name.capitalize()
        headers[f"X-RateLimit-Limit-{suffix}"] = str(window.limit)
        headers[f"X-RateLimit-Remaining-{suffix}"] = str(window.remaining)
        headers[f"X-RateLimit-Reset-{suffix}"] = str(window.reset)

    if breached is not None:
        headers["Retry-After"] = str(breached.retry_after)
    return headers


def reset_local_counters() -> None:
    """Drop every in-process counter. For tests and for a deliberate flush."""
    with _memory_lock:
        _memory_counters.clear()


__all__ = [
    "MINUTE", "DAY", "MONTH", "WindowState", "RateLimitState",
    "check_and_consume", "headers_for", "redis_available",
    "reset_local_counters",
]
