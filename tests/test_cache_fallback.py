"""The cache must be invisible when Redis is absent, and correct when it is not.

Redis is optional here: production points REDIS_URL at a hosted instance, and a
developer's machine usually has nothing listening. Both are supported states, so
both are tested. The failure this suite exists to prevent is the one that makes
an optional dependency worse than no dependency at all — a cache that stalls
every request paying a connection timeout to a Redis that was never there.

There is no redis-server, docker or fakeredis on the build machines, so the
Redis half runs against a minimal RESP server defined here. It implements only
the handful of commands the cache issues, but redis-py is the real client, so
the code path under test is the one that will run against a hosted Redis.
"""

import fnmatch
import socket
import sys
import threading
import time

import pytest

from app.services.cache import Cache


# ---------------------------------------------------------------------------
# A RESP server small enough to read in one sitting
# ---------------------------------------------------------------------------

class RespStub:
    """Speaks PING / SET..EX / GET / SCAN / DEL, and nothing else."""

    def __init__(self):
        self.store = {}
        self.log = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(16)
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self):
        return "redis://127.0.0.1:%d/0" % self.port

    def close(self):
        self._sock.close()

    # -- plumbing -----------------------------------------------------------

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while True:
                args, rest = self._parse(buf)
                if args is None:
                    break
                buf = rest
                try:
                    conn.sendall(self._dispatch(args))
                except OSError:
                    return

    @staticmethod
    def _parse(buf):
        if not buf.startswith(b"*"):
            return None, buf
        try:
            nl = buf.index(b"\r\n")
            count = int(buf[1:nl])
            pos, args = nl + 2, []
            for _ in range(count):
                if buf[pos:pos + 1] != b"$":
                    return None, buf
                nl2 = buf.index(b"\r\n", pos)
                length = int(buf[pos + 1:nl2])
                start = nl2 + 2
                args.append(buf[start:start + length])
                pos = start + length + 2
            return args, buf[pos:]
        except (ValueError, IndexError):
            return None, buf

    def _alive(self, key):
        entry = self.store.get(key)
        if entry is None:
            return False
        _, expires_at = entry
        if expires_at is not None and expires_at <= time.monotonic():
            del self.store[key]
            return False
        return True

    def _dispatch(self, args):
        cmd = args[0].upper().decode()
        self.log.append(cmd)

        if cmd == "PING":
            return b"+PONG\r\n"

        if cmd == "HELLO":
            # A RESP2-only server. redis-py 8 opens with HELLO 3 by default and
            # dies here unless the client pins protocol=2 — which is exactly the
            # portability guarantee this reply is here to hold us to.
            return b"-ERR unknown command 'HELLO'\r\n"

        if cmd == "SET":
            key, value = args[1].decode(), args[2].decode()
            expires_at = None
            for i, token in enumerate(args[3:], start=3):
                if token.upper() == b"EX":
                    expires_at = time.monotonic() + int(args[i + 1])
            self.store[key] = (value, expires_at)
            return b"+OK\r\n"

        if cmd == "GET":
            key = args[1].decode()
            if not self._alive(key):
                return b"$-1\r\n"
            payload = self.store[key][0].encode()
            return b"$%d\r\n%s\r\n" % (len(payload), payload)

        if cmd == "SCAN":
            match = "*"
            for i, token in enumerate(args):
                if token.upper() == b"MATCH":
                    match = args[i + 1].decode()
            keys = [k for k in list(self.store)
                    if self._alive(k) and fnmatch.fnmatch(k, match)]
            out = b"*2\r\n$1\r\n0\r\n*%d\r\n" % len(keys)
            for k in keys:
                kb = k.encode()
                out += b"$%d\r\n%s\r\n" % (len(kb), kb)
            return out

        if cmd == "DEL":
            deleted = sum(1 for a in args[1:] if self.store.pop(a.decode(), None) is not None)
            return b":%d\r\n" % deleted

        return b"+OK\r\n"      # CLIENT SETINFO and friends


def _cache_pointing_at(monkeypatch, url):
    import app.services.cache as cache_mod
    monkeypatch.setattr(cache_mod.settings, "REDIS_URL", url, raising=False)
    return Cache()


def _await_backend(cache, want, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cache.backend_name() == want:
            return True
        time.sleep(0.02)
    return cache.backend_name() == want


# ---------------------------------------------------------------------------
# No Redis — the ordinary development machine
# ---------------------------------------------------------------------------

# TEST-NET-1. Reserved by RFC 5737 and routed nowhere, so a connection attempt
# hangs rather than being refused — which is the slow case worth defending
# against, not the fast one.
BLACKHOLE = "redis://192.0.2.1:6379/0"


def test_cache_works_with_no_redis(monkeypatch):
    cache = _cache_pointing_at(monkeypatch, BLACKHOLE)
    key = cache.user_key("u1", "summary")
    cache.set(key, {"total_cash": 4664553.18}, 30)
    assert cache.get(key) == {"total_cash": 4664553.18}


def test_unreachable_redis_never_blocks_a_request(monkeypatch):
    """The regression that matters most.

    An earlier revision probed Redis on the calling thread, so the first cache
    call on a machine with nothing listening blocked for 2.5 seconds — landing
    squarely in the first page load after every restart. The probe now runs on a
    daemon thread and callers are answered from memory meanwhile.
    """
    cache = _cache_pointing_at(monkeypatch, BLACKHOLE)

    started = time.perf_counter()
    for i in range(200):
        k = cache.user_key("u1", "k", i)
        cache.set(k, {"i": i}, 30)
        assert cache.get(k) == {"i": i}
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, (
        "400 cache operations took %.2fs against an unreachable Redis; "
        "something is waiting on the socket" % elapsed
    )


def test_keys_are_isolated_per_user(monkeypatch):
    cache = _cache_pointing_at(monkeypatch, BLACKHOLE)
    a, b = cache.user_key("user-A", "summary"), cache.user_key("user-B", "summary")

    cache.set(a, {"cash": 111}, 30)
    cache.set(b, {"cash": 222}, 30)
    assert a != b
    assert cache.get(a) == {"cash": 111}
    assert cache.get(b) == {"cash": 222}

    cache.invalidate_user("user-A")
    assert cache.get(a) is None, "invalidate_user left the target user's entries"
    assert cache.get(b) == {"cash": 222}, "invalidate_user reached into another user"


def test_entries_expire(monkeypatch):
    cache = _cache_pointing_at(monkeypatch, BLACKHOLE)
    key = cache.user_key("u1", "shortlived")
    cache.set(key, {"x": 1}, 1)
    assert cache.get(key) == {"x": 1}
    time.sleep(1.2)
    assert cache.get(key) is None


def test_non_serialisable_values_are_refused_not_mangled(monkeypatch):
    """A date must not come back as a string only on a cache hit."""
    from datetime import date
    from decimal import Decimal

    cache = _cache_pointing_at(monkeypatch, BLACKHOLE)
    key = cache.user_key("u1", "bad")
    cache.set(key, {"when": date(2026, 3, 31), "amount": Decimal("1.5")}, 30)
    assert cache.get(key) is None


def test_get_or_set_computes_once(monkeypatch):
    cache = _cache_pointing_at(monkeypatch, BLACKHOLE)
    calls = []

    def producer():
        calls.append(1)
        return {"computed": True}

    key = cache.user_key("u1", "produced")
    first = cache.get_or_set(key, 30, producer)
    second = cache.get_or_set(key, 30, producer)

    assert first == second == {"computed": True}
    assert len(calls) == 1


def test_memory_fallback_is_bounded(monkeypatch):
    """A long-lived worker must not grow a dict forever."""
    cache = _cache_pointing_at(monkeypatch, BLACKHOLE)
    for i in range(2000):
        cache.set(cache.user_key("u", "bulk", i), {"i": i}, 60)
    assert len(cache._memory._data) <= 512


# ---------------------------------------------------------------------------
# Redis present — what production will do
# ---------------------------------------------------------------------------

@pytest.fixture
def stub():
    server = RespStub()
    yield server
    server.close()


def test_cache_uses_redis_when_reachable(monkeypatch, stub):
    cache = _cache_pointing_at(monkeypatch, stub.url)
    assert _await_backend(cache, "redis"), "never promoted to the Redis backend"

    key = cache.user_key("demo", "dashboard-summary", "2025-10-01", "2025-12-31")
    cache.set(key, {"total_cash": 4664553.18, "txns": 1823}, 30)

    assert any(k.startswith("kredo:") and ":u:demo:" in k for k in stub.store), \
        "value never reached Redis"
    assert cache.get(key) == {"total_cash": 4664553.18, "txns": 1823}


def test_redis_entries_always_carry_an_expiry(monkeypatch, stub):
    """An entry without a TTL outlives the facts it describes."""
    cache = _cache_pointing_at(monkeypatch, stub.url)
    assert _await_backend(cache, "redis")

    cache.set(cache.user_key("demo", "k"), {"v": 1}, 30)
    stored = [v for v in stub.store.values()]
    assert stored and stored[0][1] is not None, "SET was issued without EX"


def test_invalidate_user_scans_and_deletes_only_that_user(monkeypatch, stub):
    cache = _cache_pointing_at(monkeypatch, stub.url)
    assert _await_backend(cache, "redis")

    cache.set(cache.user_key("user-A", "s"), {"cash": 111}, 30)
    cache.set(cache.user_key("user-B", "s"), {"cash": 222}, 30)
    cache.invalidate_user("user-A")

    assert cache.get(cache.user_key("user-A", "s")) is None
    assert cache.get(cache.user_key("user-B", "s")) == {"cash": 222}
    assert "SCAN" in stub.log and "DEL" in stub.log


def test_keys_are_namespaced_by_environment(monkeypatch, stub):
    """Staging and production can share one hosted database without collisions."""
    import app.services.cache as cache_mod
    monkeypatch.setattr(cache_mod.settings, "ENVIRONMENT", "production", raising=False)

    cache = _cache_pointing_at(monkeypatch, stub.url)
    assert cache.user_key("u", "k").startswith("kredo:production:")
