"""Database engine, session factory and declarative Base.

PostgreSQL is the only supported backend. There is no SQLite fallback: if the
configured database cannot be reached the process fails loudly at import time
rather than silently diverting writes into a local file that nobody is backing
up. That silent-divert behaviour is what this module used to do, and it is how a
development file could quietly accumulate real data.
"""
import logging
import os
import time

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", settings.DATABASE_URL)


def _safe_url(url: str) -> str:
    """Return the URL with any password redacted, for log and error messages."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def _require_postgres(url: str) -> str:
    """Reject any non-PostgreSQL URL before a connection is attempted."""
    scheme = url.split("://", 1)[0].lower()
    if not scheme.startswith("postgres"):
        raise RuntimeError(
            f"Unsupported DATABASE_URL scheme '{scheme}'. This application runs on "
            "PostgreSQL only — set DATABASE_URL to a postgresql:// URL, e.g. "
            "postgresql://postgres:postgres@localhost:5432/backend_db"
        )
    return url


def normalize_postgres_url(url: str) -> str:
    """Pin the DBAPI driver to psycopg2, the one requirements.txt installs.

    SQLAlchemy 2.1 changed what a bare ``postgresql://`` URL means: it now loads
    psycopg (v3) instead of psycopg2. requirements.txt allows ``sqlalchemy>=2.0``
    and installs only ``psycopg2-binary``, so a fresh build (Docker, Render)
    picked up 2.1 and died at import with ``No module named 'psycopg'`` — the
    process never started. Naming the driver explicitly makes the URL mean the
    same thing on every SQLAlchemy version.

    ``postgres://`` (the Heroku-style scheme some platforms still hand out) is
    rewritten too; SQLAlchemy rejects it outright. A URL that already names a
    driver (``postgresql+psycopg://`` etc.) is left alone.
    """
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if scheme.lower() in ("postgres", "postgresql"):
        return f"postgresql+psycopg2://{rest}"
    return url


SQLALCHEMY_DATABASE_URL = normalize_postgres_url(_require_postgres(SQLALCHEMY_DATABASE_URL))


def build_engine(url: str = SQLALCHEMY_DATABASE_URL):
    """Create an engine with a pool sized for the API and worker processes."""
    return create_engine(
        url,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
        # Verifies a pooled connection is still alive before handing it out, so a
        # Postgres restart or an idle-connection reaper surfaces as one retry
        # rather than a 500 on the next request.
        pool_pre_ping=True,
        echo=settings.DB_ECHO,
        future=True,
        connect_args={
            "connect_timeout": settings.DB_CONNECT_TIMEOUT,
            # Makes this process identifiable in pg_stat_activity.
            "application_name": settings.DB_APPLICATION_NAME,
        },
    )


engine = build_engine(SQLALCHEMY_DATABASE_URL)


@event.listens_for(engine, "connect")
def _set_postgres_session_defaults(dbapi_connection, connection_record):
    """Apply per-connection server settings the application relies on."""
    if settings.DB_STATEMENT_TIMEOUT_MS > 0:
        cursor = dbapi_connection.cursor()
        try:
            # A runaway query otherwise holds a pooled connection open
            # indefinitely; this bounds it server-side.
            cursor.execute(f"SET statement_timeout = {int(settings.DB_STATEMENT_TIMEOUT_MS)}")
        finally:
            cursor.close()


def wait_for_database(eng=None) -> None:
    """Block until PostgreSQL answers, or raise after the configured retries.

    Under docker-compose the API container can start before Postgres has
    finished first-boot initialisation, so a short bounded retry replaces what
    used to be an immediate fallback to SQLite.
    """
    eng = eng or engine
    attempts = max(1, settings.DB_CONNECT_RETRIES)
    delay = settings.DB_CONNECT_RETRY_DELAY
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            if attempt > 1:
                logger.info("[Database] Connected to PostgreSQL after %d attempts.", attempt)
            return
        except OperationalError as exc:  # pragma: no cover - timing dependent
            last_error = exc
            if attempt < attempts:
                logger.warning(
                    "[Database] PostgreSQL not ready (attempt %d/%d): %s. Retrying in %.1fs.",
                    attempt, attempts, exc.__class__.__name__, delay,
                )
                time.sleep(delay)

    raise RuntimeError(
        f"Could not connect to PostgreSQL at '{_safe_url(SQLALCHEMY_DATABASE_URL)}' after "
        f"{attempts} attempt(s): {last_error}. PostgreSQL is required — there is no SQLite "
        "fallback. Check the server is running and DATABASE_URL is correct "
        "(docker compose up -d postgres)."
    ) from last_error


wait_for_database()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


# Import all model modules so that they are registered with Base.metadata
import importlib  # noqa: E402  — must follow the Base definition above

for _mod in [
    "app.models.user",
    "app.models.entity",
    "app.models.account",
    "app.models.uploaded_file",
    "app.models.category",
    "app.models.transaction",
    "app.models.prediction",
    "app.models.report",
    "app.models.audit_log",
    "app.models.refresh_token",
    "app.models.processed_transaction",
    "app.email.models",
    "app.models.rpa_job",
    "app.models.reconciliation",
    "app.models.compliance",
    "app.models.currency",
    # Account Aggregator tables were previously registered only as a side effect
    # of importing app.aa.routes, so create_all could miss them entirely.
    "app.aa.models",
]:
    importlib.import_module(_mod)


if settings.DB_AUTO_CREATE:
    # Convenience for local development. In production the schema is owned by
    # Alembic (`alembic upgrade head`); set DB_AUTO_CREATE=false there so a
    # stale model definition can never quietly create a table.
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
