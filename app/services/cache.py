"""Optional shared cache, with an in-process fallback that is always available.

WHAT THIS IS FOR
The dashboard and treasury endpoints recompute the same figures for the same
user many times over — a page load fires a dozen requests, several of which walk
the whole transaction history. Those results are worth holding for a few tens of
seconds. They are NOT worth adding a hard runtime dependency for, so this module
treats Redis as an accelerator that may or may not be there:

    Local development
          |
          +-- REDIS_URL reachable  -> shared Redis
          |
          +-- no Redis             -> in-process fallback

    Production
          |
          +-- REDIS_URL            -> hosted Redis (Upstash and friends)

Nothing above the cache knows or cares which one it got. `cache.get`/`set` never
raise, and a dead Redis costs one short timeout and then nothing at all, because
the circuit breaker below stops trying for a while.

WHY THE FALLBACK IS A FALLBACK AND NOT AN L1 LAYER
It would be tempting to keep a small in-process cache in FRONT of Redis to save
the network hop, and for most workloads that is the right call. It is the wrong
call here. Redis is what makes invalidation work across workers: when one
process clears a user's transactions it calls `invalidate_user`, and every other
process must stop serving the old figures immediately. An in-process layer in
front would keep answering from its own copy, and the user who just deleted
their data would still be looking at it. Correct financial figures beat a saved
round trip, so memory is used only when Redis is genuinely unavailable — and
then there is only one process's view to be wrong anyway.

WHY VALUES MUST BE JSON
Redis stores bytes, so anything cached has to survive a round trip through JSON.
Rather than quietly coercing dates and Decimals into strings — which would hand
callers back a different type than they put in, and only on a cache hit, which
is the worst kind of bug to chase — a value that will not serialise is simply
not cached, and says so in the log.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

#: Ceiling on the in-process fallback. Small on purpose: it holds per-user
#: aggregates for a handful of users, not a working set, and an unbounded dict
#: in a long-lived worker is a memory leak with extra steps.
_MEMORY_MAX_ENTRIES = 512

#: A cache lookup that takes longer than this is not helping. The figures behind
#: it are recomputed in a few hundred milliseconds, so waiting seconds for a
#: cache would be slower than not having one. Generous enough for a hosted Redis
#: in another region, tight enough that a hung socket cannot stall a request.
_REDIS_TIMEOUT_SECONDS = float(getattr(settings, "CACHE_REDIS_TIMEOUT", 1.0))

#: Hosted Redis plans meter connections. This is a cache, not a datastore — a
#: small pool is plenty, and exhausting the plan's connection budget to serve
#: cache reads would take the rest of the app down with it.
_REDIS_MAX_CONNECTIONS = 10

#: Consecutive failures before the breaker opens.
_BREAKER_THRESHOLD = 3

#: How long the breaker stays open. During this window every call goes straight
#: to memory with no socket attempt at all — which is the entire point. Without
#: it, REDIS_URL defaulting to localhost means a machine with no Redis pays a
#: connection timeout on every single cache call, and the "optimisation" makes
#: the app dramatically slower than having no cache.
_BREAKER_COOLDOWN_SECONDS = 30.0

#: Bounds a best-effort SCAN during invalidation so a large shared keyspace
#: cannot turn one delete into an unbounded walk.
_INVALIDATE_SCAN_LIMIT = 10_000


def _namespace() -> str:
    """Key prefix, scoped to the deployment.

    A free hosted plan is typically one database. Staging and production
    pointed at the same URL would otherwise read each other's cached figures,
    which is a data leak across environments dressed up as a performance win.
    """
    return "kredo:" + (getattr(settings, "ENVIRONMENT", "development") or "development")


def _is_production() -> bool:
    """Read at call time, not at import, so tests can set the environment."""
    return (getattr(settings, "ENVIRONMENT", "development") or "").lower() == "production"


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class _MemoryBackend:
    """Process-local dict with TTLs and a size cap. Always available."""

    name = "memory"

    def __init__(self) -> None:
        self._data: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[str]:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            expires_at, payload = hit
            if expires_at <= now:
                # Expiry is checked on read rather than swept on a timer: there
                # is no background thread here, and an entry nobody reads again
                # costs nothing but a slot the cap will reclaim.
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return payload

    def set(self, key: str, payload: str, ttl_seconds: float) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + ttl_seconds, payload)
            self._data.move_to_end(key)
            while len(self._data) > _MEMORY_MAX_ENTRIES:
                self._data.popitem(last=False)      # oldest touched goes first

    def delete_prefix(self, prefix: str) -> None:
        with self._lock:
            for key in [k for k in self._data if k.startswith(prefix)]:
                del self._data[key]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class _RedisBackend:
    """Thin wrapper over redis-py. Connects lazily and never at import time.

    Connecting at import would put a network round trip in the startup path of
    a process that may never touch the cache, and would make an unreachable
    Redis look like a broken application.
    """

    name = "redis"

    def __init__(self, url: str) -> None:
        self._url = url
        self._client = None
        self._lock = threading.RLock()

    def client(self):
        with self._lock:
            if self._client is not None:
                return self._client
            import redis  # imported here so a missing package degrades, not crashes

            # from_url handles rediss:// (TLS) as well as redis://, which is
            # what lets the same code point at a local container in development
            # and a TLS-only hosted instance in production with nothing changed
            # but the environment variable.
            self._client = redis.from_url(
                self._url,
                socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
                socket_timeout=_REDIS_TIMEOUT_SECONDS,
                max_connections=_REDIS_MAX_CONNECTIONS,
                # One attempt. A retry here doubles the worst case before the
                # caller gets to fall back, and falling back is cheap.
                retry_on_timeout=False,
                decode_responses=True,
                # RESP2, pinned deliberately.
                #
                # redis-py 8 negotiates RESP3 by default, which means it opens
                # every connection with HELLO 3 and fails outright against a
                # server that does not answer it. Managed providers vary in
                # what they accept on the Redis-protocol port, and this is a
                # cache issuing GET/SET/SCAN/DEL — there is nothing in RESP3 it
                # benefits from. Pinning the older protocol trades nothing for
                # working against every provider and every Redis back to 2.x.
                protocol=2,
            )
            return self._client

    def get(self, key: str) -> Optional[str]:
        return self.client().get(key)

    def set(self, key: str, payload: str, ttl_seconds: float) -> None:
        # Every entry gets an expiry. An entry without one outlives the facts it
        # describes and, on a shared instance, outlives the deployment too.
        self.client().set(key, payload, ex=max(1, int(ttl_seconds)))

    def delete_prefix(self, prefix: str) -> None:
        client = self.client()
        cursor, scanned = 0, 0
        while True:
            cursor, keys = client.scan(cursor=cursor, match=prefix + "*", count=500)
            if keys:
                client.delete(*keys)
            scanned += 500
            if cursor == 0 or scanned >= _INVALIDATE_SCAN_LIMIT:
                break

    def ping(self) -> bool:
        return bool(self.client().ping())


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

class Cache:
    """A cache that is always usable, whatever the state of Redis.

    Every public method swallows backend failures. Callers are expected to use
    this without try/except: a cache that can fail a request is worse than no
    cache, and dashboards have gone blank for less.
    """

    def __init__(self) -> None:
        self._memory = _MemoryBackend()
        self._redis: Optional[_RedisBackend] = None
        self._redis_failures = 0
        self._breaker_open_until = 0.0
        self._lock = threading.RLock()
        self._probed = False
        self._probe_running = False
        self._degraded_logged_until = 0.0

        url = (getattr(settings, "REDIS_URL", "") or "").strip()
        enabled = str(getattr(settings, "CACHE_REDIS_ENABLED", "true")).lower() != "false"
        if url and enabled:
            self._redis = _RedisBackend(url)

    # -- backend selection --------------------------------------------------

    def _redis_available(self) -> bool:
        """True only once a background probe has CONFIRMED Redis is reachable.

        The probe has to happen, because REDIS_URL has a default value: it is
        set on every machine whether or not anything is listening, so "is it
        configured" says nothing about "is it there".

        The probe must not happen on the calling thread. Discovering that
        nothing is listening costs a connect timeout — measured at 2.5s against
        a local port with no server, because the resolver tries IPv6 and then
        IPv4 — and paying that inside a request would put two and a half
        seconds on the first dashboard load after every restart. That is worse
        than the problem this cache exists to solve.

        So: assume unavailable, answer from memory, and let a daemon thread find
        out. The cost of being wrong for a few hundred milliseconds is a cold
        cache. The cost of blocking is a visibly slow app.
        """
        if self._redis is None:
            return False
        with self._lock:
            if self._probed:
                return True
            if time.monotonic() < self._breaker_open_until:
                return False
            if not self._probe_running:
                self._probe_running = True
                threading.Thread(
                    target=self._probe, name="cache-redis-probe", daemon=True,
                ).start()
        return False

    def _probe(self) -> None:
        """Ping Redis off the request path and record the verdict."""
        try:
            self._redis.ping()
        except Exception as exc:
            with self._lock:
                self._probe_running = False
            self._trip(exc, probing=True)
            return
        with self._lock:
            self._probe_running = False
            self._probed = True
        logger.info("[cache] using Redis at %s", _redacted(self._redis._url))

    def _trip(self, exc: Exception, probing: bool = False) -> None:
        """Record a Redis failure and open the breaker once they add up."""
        with self._lock:
            self._redis_failures += 1
            if self._redis_failures < _BREAKER_THRESHOLD and not probing:
                return
            # Were we actually serving from Redis up to this moment?
            was_on_redis = self._probed
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECONDS
            self._probed = False
            self._probe_running = False
            self._redis_failures = 0
        # Logged at info, not warning: on a developer machine with no Redis this
        # is the expected steady state, and a warning every 30 seconds for
        # working-as-intended behaviour trains people to ignore the log.
        logger.info(
            "[cache] Redis unavailable (%s: %s) — using the in-process cache for "
            "the next %.0fs. This is harmless; figures are simply not shared "
            "between workers.",
            exc.__class__.__name__, exc, _BREAKER_COOLDOWN_SECONDS,
        )
        if was_on_redis:
            # Coming DOWN from Redis, drop whatever memory still holds. While
            # Redis was up, invalidations went to Redis, so anything left in
            # memory predates them and may be exactly the figures another worker
            # told us to forget.
            #
            # Note this is conditional. On a machine with no Redis at all the
            # very first probe fails, and clearing there would throw away the
            # entries written during the few hundred milliseconds the probe was
            # in flight — discarding the only cache we have, every startup.
            self._memory.clear()

    def _succeeded(self) -> None:
        if self._redis_failures:
            with self._lock:
                self._redis_failures = 0

    def _memory_fallback_allowed(self) -> bool:
        """Whether a Redis outage may be absorbed by this process's own memory.

        NO IN PRODUCTION, and this is the one place the two environments are
        deliberately allowed to behave differently.

        The in-process cache is correct for exactly one process. The moment
        there are two — `uvicorn --workers 2`, a second container behind a load
        balancer, the API alongside a worker — it stops being a cache and starts
        being a way to serve figures that another process has already
        invalidated. `invalidate_user()` reaches Redis and this process's own
        memory; it cannot reach the memory of a process it does not share an
        address space with. So:

            1. Process A deletes a user's transactions and commits.
            2. The after-commit listener calls invalidate_user(). Redis is down,
               so only A's memory is cleared.
            3. Process B serves that user's dashboard from ITS memory.
            4. B reports a cash position built on rows that no longer exist.

        The TTLs bound that to 30 seconds for the analytics figures and 60 for
        the compliance state. Thirty seconds of a wrong balance, immediately
        after the user pressed delete, is not a degradation a treasury tool can
        offer — and it is silent, which makes it worse.

        Turning caching OFF is the safe failure. Every read then recomputes from
        PostgreSQL, which is slower and always correct. That is the right trade
        for money, and it is bounded: the breaker keeps the dead socket off the
        request path, so "no cache" costs recomputation, never a stall.

        Development keeps the fallback. There is one process, so there is no
        second view to disagree with, and losing the cache on a laptop with no
        Redis would make the app slower for no safety gained.
        """
        return not _is_production()

    def _degraded_in_production(self) -> None:
        """Say — once per breaker window — that production is running uncached.

        A warning here where the same condition is logged at info in
        development, because in production it means every dashboard read is
        hitting PostgreSQL and somebody should know Redis is gone.
        """
        now = time.monotonic()
        with self._lock:
            if now < self._degraded_logged_until:
                return
            self._degraded_logged_until = now + _BREAKER_COOLDOWN_SECONDS
        logger.warning(
            "[cache] PRODUCTION: Redis is unavailable, so caching is DISABLED "
            "rather than falling back to a per-process cache. Figures stay "
            "correct and every read recomputes from the database. Restore Redis "
            "to bring the cache back."
        )

    def backend_name(self) -> str:
        """"redis", "memory", or "disabled" — where a call right now would land."""
        if self._redis_available():
            return "redis"
        return "memory" if self._memory_fallback_allowed() else "disabled"

    # -- keys ---------------------------------------------------------------

    def user_key(self, user_id: Any, *parts: Any) -> str:
        """Build a per-user cache key.

        Always use this. Every figure this application caches belongs to exactly
        one user, and a hand-rolled key that forgets the user id does not
        produce a slow dashboard, it produces one tenant's cash position on
        another tenant's screen.
        """
        tail = ":".join("" if p is None else str(p) for p in parts)
        return f"{_namespace()}:u:{user_id}:{tail}"

    # -- operations ---------------------------------------------------------

    def get(self, key: str) -> Optional[Any]:
        if self._redis_available():
            try:
                payload = self._redis.get(key)
                self._succeeded()
                return None if payload is None else json.loads(payload)
            except Exception as exc:
                self._trip(exc)
            except BaseException:
                raise
        if not self._memory_fallback_allowed():
            # A miss, always. The caller recomputes from the database, which is
            # the whole point: correct and slower beats fast and stale.
            self._degraded_in_production()
            return None
        payload = self._memory.get(key)
        if payload is None:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            return None

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError) as exc:
            # Not cached, and said out loud. Silently skipping would make this
            # look like a cache that simply never hits, which is a much harder
            # thing to notice than a log line.
            logger.warning(
                "[cache] not caching %s: value is not JSON-serialisable (%s). "
                "Call .model_dump() on Pydantic models before caching.", key, exc,
            )
            return

        if self._redis_available():
            try:
                self._redis.set(key, payload, ttl_seconds)
                self._succeeded()
                return
            except Exception as exc:
                self._trip(exc)
        if not self._memory_fallback_allowed():
            # Deliberately dropped. Writing it would create exactly the
            # per-process copy that another process cannot invalidate.
            self._degraded_in_production()
            return
        self._memory.set(key, payload, ttl_seconds)

    def get_or_set(self, key: str, ttl_seconds: float, producer: Callable[[], Any]) -> Any:
        """Return the cached value, or call `producer`, cache it and return it.

        Deliberately NOT locked against a stampede. Two workers computing the
        same dashboard at once is a few hundred milliseconds of duplicated work;
        a lock held across `producer` would be a lock held across database
        queries, and that turns a cache into an outage.
        """
        hit = self.get(key)
        if hit is not None:
            return hit
        value = producer()
        if value is not None:
            self.set(key, value, ttl_seconds)
        return value

    def invalidate_all(self) -> None:
        """Drop every cached entry for this deployment.

        The blunt instrument, for the case where something changed but the
        affected user cannot be determined — a bulk UPDATE or DELETE issued
        straight to the database bypasses the ORM, so the rows it touched never
        pass through the unit of work that would have named their owner.

        Throwing the whole cache away is heavy-handed and completely safe. The
        operations that reach here (clearing a user's transactions, deleting an
        account) are rare and destructive, and a cold cache after one of them
        costs a few hundred milliseconds. Serving a figure from data that no
        longer exists costs rather more.
        """
        prefix = _namespace() + ":"
        if self._redis_available():
            try:
                self._redis.delete_prefix(prefix)
                self._succeeded()
            except Exception as exc:
                self._trip(exc)
        self._memory.clear()

    def invalidate_user(self, user_id: Any) -> None:
        """Drop everything cached for one user.

        Call this from anything that changes what a user's figures are built
        from — importing or clearing transactions, running a scan, editing
        rules. Best effort by design: if it fails, the TTLs still bound how long
        a stale figure can survive, so the worst case is seconds of lag rather
        than a wrong number that persists.
        """
        prefix = f"{_namespace()}:u:{user_id}:"
        if self._redis_available():
            try:
                self._redis.delete_prefix(prefix)
                self._succeeded()
            except Exception as exc:
                self._trip(exc)
        # Cleared in both places regardless: this process may have been writing
        # to memory during a Redis outage, and those entries are exactly the
        # ones most likely to be stale now.
        self._memory.delete_prefix(prefix)


def _redacted(url: str) -> str:
    """Host and port only — a Redis URL carries its password inline."""
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.hostname}:{parts.port or 6379}"
    except Exception:
        return "(redis)"


#: The shared instance. Import this, not the class.
cache = Cache()
