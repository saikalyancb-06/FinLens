"""Production configuration and cache-safety guarantees.

Two independent things are pinned here, and they failed together in the same
way: a setting whose development default is convenient, shipped into production
where the same default is dangerous, with nothing that noticed.

  1. `validate_security()` must actually reject a JWT secret that is published
     in this repository — and must not fire in development, or nobody could run
     the app without configuring one.

  2. The cache must never let one process serve financial figures that another
     process has already invalidated. In development that cannot happen (one
     process), so the in-memory fallback is kept for convenience. In production
     it can, so the fallback is refused and caching is simply off.

The multi-process tests below use TWO `Cache` instances. That is exactly what
two backend processes are, as far as this module is concerned: separate
`_MemoryBackend` dicts, and either a shared Redis or nothing.
"""

import os
import sys

import pytest

from app.config import Settings, settings
from app.services.cache import Cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def as_environment(monkeypatch):
    """Run a block with ENVIRONMENT set, restoring it afterwards.

    Patched on the live `settings` object rather than the process environment,
    because `app.config` reads os.environ at import and the module is long
    since imported by the time a test runs.
    """
    def _set(env: str):
        monkeypatch.setattr(settings, "ENVIRONMENT", env)
    return _set


def _guard(monkeypatch, environment, secret, secret_set=True):
    """Run validate_security() under a given environment and secret.

    `Settings` reads os.environ in its CLASS BODY, which is evaluated once when
    the module is first imported — so constructing a new instance does not
    re-read the environment. The attributes are therefore set on the instance,
    and JWT_SECRET_KEY is ALSO put in os.environ because validate_security()
    checks `os.getenv` directly to tell "unset" from "set to something bad".
    """
    cfg = Settings()
    monkeypatch.setattr(cfg, "ENVIRONMENT", environment, raising=False)
    if secret_set:
        monkeypatch.setattr(cfg, "JWT_SECRET_KEY", secret, raising=False)
        monkeypatch.setenv("JWT_SECRET_KEY", secret)
    else:
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    return cfg


def _settings_in_subprocess(env: dict, expression: str):
    """Evaluate `expression` against app.config in a FRESH interpreter.

    The only faithful way to test a class-body setting, and it also exercises
    the real import path — including `validate_security()` running at module
    level, which is where a misconfigured production process actually dies.

    Returns (returncode, stdout, stderr).
    """
    import subprocess
    child = os.environ.copy()
    child.pop("JWT_SECRET_KEY", None)
    child.pop("ENVIRONMENT", None)
    child.pop("ALLOWED_ORIGINS", None)
    child.pop("DB_AUTO_CREATE", None)
    child.update({k: str(v) for k, v in env.items()})
    proc = subprocess.run(
        [sys.executable, "-c",
         "import app.config as c; print(" + expression + ")"],
        capture_output=True, text=True, env=child, timeout=120,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _two_processes_sharing_redis():
    """Two Cache instances against ONE fake Redis server.

    fakeredis' FakeServer is shared state behind two independent clients, which
    is the same relationship two containers have with one Redis.
    """
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()

    def one():
        c = Cache()
        assert c._redis is not None, "REDIS_URL must be set for this test"
        c._redis._client = fakeredis.FakeStrictRedis(
            server=server, decode_responses=True)
        c._probed = True          # stand in for a successful background probe
        return c

    return one(), one()


# ===========================================================================
# 1. Production security validation
# ===========================================================================

class TestProductionSecretValidation:
    """The guard that stops production booting on a secret anyone can read."""

    def test_the_historical_default_is_rejected_in_production(self, monkeypatch):
        cfg = _guard(monkeypatch, "production", Settings.INSECURE_DEFAULT_SECRET)
        with pytest.raises(RuntimeError, match="published in this repository"):
            cfg.validate_security()

    @pytest.mark.parametrize("placeholder", sorted(Settings.INSECURE_SECRETS))
    def test_every_published_placeholder_is_rejected(self, monkeypatch, placeholder):
        """Not just the one historical value.

        The example file's placeholder changed once already. If the guard only
        knew the old string, the new one would pass validation while still being
        readable by anyone who has the repository.
        """
        cfg = _guard(monkeypatch, "production", placeholder)
        with pytest.raises(RuntimeError):
            cfg.validate_security()

    def test_an_unset_secret_is_rejected_in_production(self, monkeypatch):
        cfg = _guard(monkeypatch, "production", None, secret_set=False)
        with pytest.raises(RuntimeError, match="not set"):
            cfg.validate_security()

    def test_a_short_secret_is_rejected_in_production(self, monkeypatch):
        cfg = _guard(monkeypatch, "production", "x" * 8)
        with pytest.raises(RuntimeError, match="shorter than"):
            cfg.validate_security()

    def test_a_real_secret_passes(self, monkeypatch):
        cfg = _guard(monkeypatch, "production", "P4Z" + "q" * 61)
        cfg.validate_security()                  # must not raise

    def test_development_is_not_constrained(self, monkeypatch):
        """The whole point of the production branch is that dev needs no setup."""
        cfg = _guard(monkeypatch, "development", Settings.INSECURE_DEFAULT_SECRET)
        cfg.validate_security()                  # must not raise

    def test_the_guard_runs_at_import_not_only_when_called(self):
        """A misconfigured production process must FAIL TO START.

        Checked in a real interpreter, because that is the thing that protects
        the deployment: `validate_security()` is invoked at the bottom of
        app/config.py, so the import itself raises and the container dies rather
        than serving traffic on a forgeable token.
        """
        rc, _out, err = _settings_in_subprocess(
            {"ENVIRONMENT": "production",
             "JWT_SECRET_KEY": Settings.INSECURE_DEFAULT_SECRET},
            "'imported-without-error'",
        )
        assert rc != 0, "production imported cleanly on the insecure default secret"
        assert "CRITICAL SECURITY CONFIGURATION ERROR" in err

    def test_development_still_imports_with_no_configuration_at_all(self):
        """The other half: a laptop with an empty environment must still run."""
        rc, out, err = _settings_in_subprocess(
            {"ENVIRONMENT": "development"}, "'imported-ok'")
        assert rc == 0, f"development import failed: {err}"
        assert out == "imported-ok"

    def test_db_auto_create_defaults_off_in_production(self):
        """Alembic owns the production schema; the models must not create tables."""
        rc, out, _ = _settings_in_subprocess(
            {"ENVIRONMENT": "production", "JWT_SECRET_KEY": "P4Z" + "q" * 61},
            "c.settings.DB_AUTO_CREATE")
        assert rc == 0 and out == "False", f"production DB_AUTO_CREATE={out!r}"

        rc, out, _ = _settings_in_subprocess(
            {"ENVIRONMENT": "development"}, "c.settings.DB_AUTO_CREATE")
        assert rc == 0 and out == "True", f"development DB_AUTO_CREATE={out!r}"

    def test_allowed_origins_does_not_fall_back_to_localhost_in_production(self):
        rc, out, _ = _settings_in_subprocess(
            {"ENVIRONMENT": "production", "JWT_SECRET_KEY": "P4Z" + "q" * 61},
            "c.settings.ALLOWED_ORIGINS")
        assert rc == 0, "production config failed to import"
        assert "localhost" not in out and "127.0.0.1" not in out, (
            f"production CORS fell back to a development origin: {out}"
        )

    def test_allowed_origins_is_configurable(self):
        rc, out, _ = _settings_in_subprocess(
            {"ENVIRONMENT": "production", "JWT_SECRET_KEY": "P4Z" + "q" * 61,
             "ALLOWED_ORIGINS": "https://a.example,https://b.example"},
            "c.settings.ALLOWED_ORIGINS")
        assert rc == 0
        assert "a.example" in out and "b.example" in out


# ===========================================================================
# 2. Cache behaviour, by environment and Redis availability
# ===========================================================================

class TestCacheWithRedisUnavailable:
    """No Redis. The two environments are allowed to differ here, and do."""

    def test_development_keeps_the_in_process_fallback(self, as_environment):
        as_environment("development")
        c = Cache()
        assert c.backend_name() == "memory"
        key = c.user_key("u1", "figure")
        c.set(key, {"total": 1}, 30)
        assert c.get(key) == {"total": 1}, (
            "development lost its cache; a laptop with no Redis should still "
            "get the speed-up, because there is only one process"
        )

    def test_production_disables_caching_rather_than_using_memory(
            self, as_environment):
        as_environment("production")
        c = Cache()
        assert c.backend_name() == "disabled"
        key = c.user_key("u1", "figure")
        c.set(key, {"total": 1}, 30)
        assert c.get(key) is None, (
            "production cached to process memory during a Redis outage. That "
            "entry cannot be invalidated by another process."
        )

    def test_production_does_not_serve_figures_another_process_invalidated(
            self, as_environment):
        """The failure this whole change exists to prevent.

        Process A deletes a user's transactions and invalidates. Process B must
        not answer from a copy A could never reach.
        """
        as_environment("production")
        a, b = Cache(), Cache()
        uid = "user-1"
        key = a.user_key(uid, "dashboard-summary")

        a.set(key, {"total_debit": 128871.50}, 30)
        b.set(key, {"total_debit": 128871.50}, 30)
        a.invalidate_user(uid)

        assert b.get(key) is None, (
            "process B served a figure that process A had invalidated"
        )

    def test_development_accepts_that_risk_knowingly(self, as_environment):
        """Documents the deliberate difference rather than leaving it implied.

        This asserts the CURRENT development contract: the fallback is per
        process, so a second process would not see an invalidation. That is
        acceptable only because development runs one process. If this test ever
        fails, the fallback policy changed and the docstring above is stale.
        """
        as_environment("development")
        a, b = Cache(), Cache()
        uid = "user-1"
        key = a.user_key(uid, "dashboard-summary")
        a.set(key, {"total_debit": 1.0}, 30)
        b.set(key, {"total_debit": 1.0}, 30)
        a.invalidate_user(uid)

        assert a.get(key) is None            # the writer's own view is correct
        assert b.get(key) == {"total_debit": 1.0}

    def test_a_dead_redis_never_blocks_a_call(self, as_environment):
        """The breaker must keep the dead socket off the request path.

        A connect timeout is ~1s. A thousand cache operations that each paid one
        would be a quarter of an hour, so this bound is loose on purpose and
        still catches the regression it is there for.
        """
        import time
        as_environment("production")
        c = Cache()
        started = time.monotonic()
        for i in range(500):
            c.get(f"k{i}")
            c.set(f"k{i}", {"v": i}, 30)
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, (
            f"1000 cache operations took {elapsed:.1f}s with Redis down — "
            "something is waiting on the socket instead of the breaker"
        )


class TestCacheWithRedisAvailable:
    """Redis up. Both environments behave identically, and must."""

    def test_production_uses_redis(self, as_environment):
        as_environment("production")
        a, _ = _two_processes_sharing_redis()
        assert a.backend_name() == "redis"

    def test_one_process_reads_what_another_wrote(self, as_environment):
        as_environment("production")
        a, b = _two_processes_sharing_redis()
        key = a.user_key("u1", "dashboard-summary")
        a.set(key, {"total_debit": 128871.50}, 30)
        assert b.get(key) == {"total_debit": 128871.50}

    def test_invalidate_user_reaches_the_other_process(self, as_environment):
        """The scenario, with Redis present: A writes, A invalidates, B reads."""
        as_environment("production")
        a, b = _two_processes_sharing_redis()
        uid = "u1"
        key = a.user_key(uid, "dashboard-summary")

        a.set(key, {"total_debit": 128871.50}, 30)
        assert b.get(key) is not None            # B is genuinely holding it
        a.invalidate_user(uid)
        assert b.get(key) is None, (
            "invalidate_user did not clear the shared cache"
        )

    def test_invalidate_all_reaches_the_other_process(self, as_environment):
        """The bulk-write path — `query.delete()` widens to invalidate_all."""
        as_environment("production")
        a, b = _two_processes_sharing_redis()
        key = a.user_key("u1", "dashboard-summary")
        a.set(key, {"total_debit": 1.0}, 30)
        assert b.get(key) is not None
        a.invalidate_all()
        assert b.get(key) is None

    def test_one_users_invalidation_does_not_clear_another(self, as_environment):
        """Scoping: figures are per user, and so is eviction."""
        as_environment("production")
        a, b = _two_processes_sharing_redis()
        k1 = a.user_key("user-1", "dashboard-summary")
        k2 = a.user_key("user-2", "dashboard-summary")
        a.set(k1, {"total": 1}, 30)
        a.set(k2, {"total": 2}, 30)

        a.invalidate_user("user-1")

        assert b.get(k1) is None
        assert b.get(k2) == {"total": 2}, (
            "invalidating one user evicted another user's figures"
        )
