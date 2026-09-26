import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # Environment & Database Configuration
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development").lower()

    # PostgreSQL is the only supported backend, in every environment. There is
    # no SQLite fallback: a URL pointing anywhere else is rejected at startup.
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/backend_db")

    # ---- Connection pool -----------------------------------------------------
    # Defaults suit one API process plus a worker against a stock PostgreSQL
    # (max_connections = 100). Raise DB_POOL_SIZE only alongside that limit.
    DB_POOL_SIZE: int = int(os.getenv("DB_POOL_SIZE", "10"))
    DB_MAX_OVERFLOW: int = int(os.getenv("DB_MAX_OVERFLOW", "20"))
    DB_POOL_TIMEOUT: int = int(os.getenv("DB_POOL_TIMEOUT", "30"))        # wait for a free connection, seconds
    DB_POOL_RECYCLE: int = int(os.getenv("DB_POOL_RECYCLE", "1800"))      # recycle before typical idle timeouts
    DB_CONNECT_TIMEOUT: int = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))  # TCP connect timeout, seconds
    DB_ECHO: bool = os.getenv("DB_ECHO", "false").lower() == "true"

    # Startup retry, for the docker-compose case where the API races Postgres.
    DB_CONNECT_RETRIES: int = int(os.getenv("DB_CONNECT_RETRIES", "10"))
    DB_CONNECT_RETRY_DELAY: float = float(os.getenv("DB_CONNECT_RETRY_DELAY", "1.5"))

    # Server-side guard so a runaway query cannot pin a pooled connection.
    # Set to 0 to disable.
    DB_STATEMENT_TIMEOUT_MS: int = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "60000"))
    DB_APPLICATION_NAME: str = os.getenv("DB_APPLICATION_NAME", "kredo-backend")

    # Let SQLAlchemy create missing tables at import time. Convenient locally;
    # in production Alembic owns the schema, so this defaults to off there.
    DB_AUTO_CREATE: bool = os.getenv(
        "DB_AUTO_CREATE",
        "false" if os.getenv("ENVIRONMENT", "development").lower() == "production" else "true",
    ).lower() == "true"

    # ---- FX rate refresh -----------------------------------------------------
    # Rates are pulled from RBI (USD/GBP/EUR/JPY - the statutory reference rate)
    # and a keyless market API for the rest. Both are outbound network calls, so
    # this is a single switch: turned off, the application never contacts either
    # and conversion falls back to whatever rates are already stored.
    FX_REFRESH_ENABLED: bool = os.getenv("FX_REFRESH_ENABLED", "true").lower() == "true"

    # Poll interval. RBI publishes its reference rate once per business day
    # around 13:30 IST, so polling faster than this buys nothing from that
    # source - the market API is the only one that moves intraday. Below 5
    # minutes is refused outright: it would be hammering someone else's server
    # for data that has not changed.
    FX_REFRESH_MINUTES: int = max(5, int(os.getenv("FX_REFRESH_MINUTES", "30")))

    # Wait this long after startup before the first poll, so a cold start serves
    # requests before it starts reaching out to the network.
    FX_REFRESH_STARTUP_DELAY: int = int(os.getenv("FX_REFRESH_STARTUP_DELAY", "20"))

    FX_HTTP_TIMEOUT: float = float(os.getenv("FX_HTTP_TIMEOUT", "20"))

    # After this many consecutive refusals (not timeouts), a source is left
    # alone for FX_SOURCE_COOLDOWN_HOURS instead of being retried on every poll.
    FX_SOURCE_FAILURE_LIMIT: int = int(os.getenv("FX_SOURCE_FAILURE_LIMIT", "3"))
    FX_SOURCE_COOLDOWN_HOURS: float = float(os.getenv("FX_SOURCE_COOLDOWN_HOURS", "12"))

    # A newly fetched rate that differs from the last stored one by more than
    # this fraction is rejected and logged rather than written. Guards against a
    # scraper that has latched onto the wrong column, which otherwise silently
    # rewrites every converted figure on screen.
    FX_MAX_STEP_CHANGE: float = float(os.getenv("FX_MAX_STEP_CHANGE", "0.10"))

    INSECURE_DEFAULT_SECRET: str = "super-secret-jwt-key-change-in-production-12345"

    #: Every JWT secret that appears somewhere in this repository, and therefore
    #: every value an attacker can read rather than guess. `INSECURE_DEFAULT_SECRET`
    #: is kept as its own name because it is the historical default and is
    #: referenced elsewhere; this set is what production is actually checked
    #: against, so a NEW placeholder added to .env.example can never quietly
    #: become a value the guard does not recognise.
    INSECURE_SECRETS: frozenset = frozenset({
        "super-secret-jwt-key-change-in-production-12345",   # the original default
        "dev-only-placeholder-change-me",                    # .env.example
        "changeme", "secret", "change-me", "your-secret-key",
    })

    #: Shorter than this cannot carry enough entropy for HS256 to be worth
    #: anything. Not a substitute for the set above — a bound underneath it.
    MIN_JWT_SECRET_LENGTH: int = 32

    # Defaults to the known-insecure value on purpose: development runs with no
    # configuration at all, and production refuses to start on it (see
    # validate_security). Referenced rather than repeated so the two cannot drift.
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", INSECURE_DEFAULT_SECRET)
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
    REFRESH_TOKEN_EXPIRE_DAYS: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

    # Upload Directory & File Limits
    UPLOAD_DIR: str = os.getenv("UPLOAD_DIR", "uploads")
    MAX_FILE_SIZE_BYTES: int = int(os.getenv("MAX_FILE_SIZE_BYTES", str(50 * 1024 * 1024)))  # 50MB limit

    # ML & Decision Engine
    # Directory holding the trained categorizer artifact (categorizer_model.joblib
    # + model_metadata.json) written by mlmodel/train_purpose_classifier.py. Both
    # the parsing pipeline and the hybrid categorisation layer load from here.
    CATEGORIZER_ARTIFACT_DIR: str = os.getenv("CATEGORIZER_ARTIFACT_DIR", "mlmodel/artifacts")
    # Retained for the legacy pkl layout in mlmodel/models, which no longer backs
    # any prediction path. Kept so existing configuration does not fail to load.
    ML_MODELS_DIR: str = os.getenv("ML_MODELS_DIR", "mlmodel/models")
    CONFIDENCE_THRESHOLD: float = float(os.getenv("CONFIDENCE_THRESHOLD", "0.80"))

    # Redis
    #
    # Used by app/services/cache.py as the SHARED cache, and it is what makes
    # invalidation work across processes. How an outage is handled now depends
    # on the environment, because the safe answer differs:
    #
    #   * development — falls back to an in-process cache and carries on. One
    #     process, so there is no second copy to disagree with, and this is why
    #     the default below being set on every machine is harmless.
    #   * production  — caching is DISABLED rather than falling back. A
    #     per-process cache cannot be invalidated by another process, so with
    #     more than one backend running it would serve financial figures that a
    #     sibling has already invalidated. Reads recompute from PostgreSQL:
    #     slower, and always correct.
    #
    # app/services/parsing_queue.py reads this too but does not connect; file
    # parsing runs inline in the backend via FastAPI background tasks.
    #
    # rediss:// (note the second s) selects TLS, which is what hosted providers
    # such as Upstash require. Nothing else has to change to move from a local
    # container to a hosted instance.
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # Escape hatch: set to "false" to force the in-process cache even where a
    # Redis is reachable. Useful when isolating whether a stale figure came from
    # the shared cache or from the calculation behind it.
    CACHE_REDIS_ENABLED: str = os.getenv("CACHE_REDIS_ENABLED", "true")

    # A cache lookup slower than this is not worth waiting for — the figures
    # behind it recompute in a few hundred milliseconds. Raise it if the hosted
    # Redis sits in a distant region and the logs show the breaker opening.
    CACHE_REDIS_TIMEOUT: float = float(os.getenv("CACHE_REDIS_TIMEOUT", "1.0"))

    # Rate Limiting (Requests per Minute per IP)
    RATE_LIMIT_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
    # Only enable when the app genuinely sits behind a reverse proxy that sets
    # X-Forwarded-For; otherwise clients can spoof the header to evade limits.
    TRUST_PROXY_HEADERS: bool = os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true"

    # Interactive API docs (/docs, /redoc, /openapi.json). On outside
    # production, off inside it — see the note at the FastAPI() call in main.py.
    # Set true to re-enable them in production deliberately.
    ENABLE_API_DOCS: bool = os.getenv("ENABLE_API_DOCS", "false").lower() == "true"

    # CORS Origins (Comma-separated or list)
    _raw_origins = os.getenv("ALLOWED_ORIGINS")
    if _raw_origins:
        ALLOWED_ORIGINS: list = [o.strip() for o in _raw_origins.split(",") if o.strip()]
    elif os.getenv("ENVIRONMENT", "development").lower() == "production":
        ALLOWED_ORIGINS: list = ["https://kredo.in", "https://app.kredo.in"]
    else:
        ALLOWED_ORIGINS: list = ["http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:3000"]

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_DIR: str = os.getenv("LOG_DIR", "logs")
    LOG_FILE: str = os.getenv("LOG_FILE", "app.log")
    LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 10 MB
    LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", "5"))

    # Microsoft Graph OAuth (Multi-Tenant)
    MICROSOFT_CLIENT_ID: str = os.getenv("MICROSOFT_CLIENT_ID", "").strip()
    MICROSOFT_CLIENT_SECRET: str = os.getenv("MICROSOFT_CLIENT_SECRET", "").strip()
    MICROSOFT_TENANT: str = os.getenv("MICROSOFT_TENANT", "common").strip() or "common"
    MICROSOFT_REDIRECT_URI: str = os.getenv(
        "MICROSOFT_REDIRECT_URI",
        "http://localhost:8000/email/oauth/microsoft/callback",
    ).strip()

    # Google Gmail OAuth 2.0
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    GOOGLE_REDIRECT_URI: str = os.getenv(
        "GOOGLE_REDIRECT_URI",
        "http://localhost:8000/email/oauth/callback",
    ).strip()

    DEMO_MODE: bool = os.getenv("DEMO_MODE", "false").lower() == "true"

    def validate_database(self):
        """Reject any database backend other than PostgreSQL.

        Checked here, before the engine is built, so a stray sqlite:/// URL in a
        .env file fails at import with a clear message instead of producing a
        second, invisible copy of the data.
        """
        scheme = (self.DATABASE_URL or "").split("://", 1)[0].lower()
        if not scheme.startswith("postgres"):
            raise RuntimeError(
                "CRITICAL DATABASE CONFIGURATION ERROR: DATABASE_URL must be a "
                f"PostgreSQL URL, got scheme '{scheme or '(empty)'}'. This application "
                "no longer supports SQLite. Example: "
                "postgresql://postgres:postgres@localhost:5432/backend_db"
            )

    def validate_security(self):
        """Validate security properties at startup.

        Runs at import (bottom of this module), so a misconfigured production
        process fails to start rather than serving traffic on a forgeable token.
        Development is untouched: every check below is inside the production
        branch, deliberately, so a laptop needs no configuration to run the app.
        """
        if self.ENVIRONMENT == "production":
            if not os.getenv("JWT_SECRET_KEY"):
                raise RuntimeError(
                    "CRITICAL SECURITY CONFIGURATION ERROR: JWT_SECRET_KEY is not set "
                    "in production. Generate one with "
                    "`python -c \"import secrets; print(secrets.token_urlsafe(64))\"` "
                    "and supply it through the environment."
                )
            if self.JWT_SECRET_KEY in self.INSECURE_SECRETS:
                raise RuntimeError(
                    "CRITICAL SECURITY CONFIGURATION ERROR: JWT_SECRET_KEY is set to a "
                    "placeholder that is published in this repository. Anyone who can "
                    "read the source can forge a token for any user. Generate a real "
                    "secret and supply it through the environment."
                )
            if len(self.JWT_SECRET_KEY) < self.MIN_JWT_SECRET_LENGTH:
                raise RuntimeError(
                    "CRITICAL SECURITY CONFIGURATION ERROR: JWT_SECRET_KEY is shorter "
                    f"than {self.MIN_JWT_SECRET_LENGTH} characters, which is too little "
                    "entropy to sign sessions with."
                )

settings = Settings()
settings.validate_database()
settings.validate_security()
