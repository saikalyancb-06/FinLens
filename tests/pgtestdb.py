"""Helpers for running the test suite against a real PostgreSQL server.

The suite used to run on `sqlite:///:memory:`, which meant tests exercised
different type coercion, different NULL ordering and different constraint
enforcement than production. Everything now runs on PostgreSQL, so a test that
passes here is evidence about the database the application actually uses.

Configuration
-------------
TEST_DATABASE_URL   full URL of the test database
                    (default: the admin URL with database name `backend_test_db`)
TEST_DATABASE_ADMIN_URL
                    URL of a database used only to issue CREATE DATABASE
                    (default: DATABASE_URL with its database swapped for `postgres`)

The test database is created if missing and its schema is dropped and rebuilt at
the start of every session, so runs never inherit state from each other.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# The application reads its credentials from .env (app/config.py calls
# load_dotenv at import). This module runs BEFORE app.config is imported —
# conftest has to resolve the test database first — so without this it saw an
# empty environment, silently fell back to DEFAULT_URL below, and the suite
# died on
#
#     FATAL: password authentication failed for user "postgres"
#
# while the application itself connected fine. Same file, same credentials, so
# running the tests needs no shell setup. Loaded by explicit path so it does not
# depend on the working directory, and without override so an env var set on the
# command line still wins.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

DEFAULT_URL = "postgresql://postgres:postgres@localhost:5432/backend_db"


def _redact(url: str) -> str:
    """URL with the password removed, safe to put in an error message."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    creds, host = parts.netloc.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return urlunsplit((parts.scheme, f"{user}:***@{host}", parts.path,
                       parts.query, parts.fragment))


def _with_database(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def test_database_url() -> str:
    """URL of the database the suite should use."""
    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        return explicit
    base = os.getenv("DATABASE_URL", DEFAULT_URL)
    return _with_database(base, os.getenv("TEST_DATABASE_NAME", "backend_test_db"))


def _admin_url() -> str:
    explicit = os.getenv("TEST_DATABASE_ADMIN_URL")
    if explicit:
        return explicit
    return _with_database(test_database_url(), "postgres")


def ensure_database(url: str | None = None) -> str:
    """Create the test database if it does not exist yet. Returns the URL."""
    url = url or test_database_url()
    name = urlsplit(url).path.lstrip("/")

    admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT", future=True)
    try:
        try:
            conn_ctx = admin.connect()
        except Exception as exc:
            # Say which URL failed and what to set. The raw psycopg2 error names
            # neither, which makes a missing .env look like a broken server.
            raise RuntimeError(
                f"The test suite could not reach PostgreSQL at "
                f"'{_redact(_admin_url())}'. Set DATABASE_URL (or "
                f"TEST_DATABASE_URL) in .env, or export it before running "
                f"pytest. Original error: {exc}"
            ) from exc
        with conn_ctx as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()
    return url


def reset_schema(engine) -> None:
    """Drop and recreate the public schema — a guaranteed-empty database.

    Cheaper and more thorough than DROP TABLE per model: it also clears the
    PostgreSQL ENUM types the models declare, which would otherwise survive and
    collide on the next create_all.
    """
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


def make_engine(url: str | None = None, reset: bool = True):
    """Return an engine bound to a freshly reset test database."""
    url = ensure_database(url)
    engine = create_engine(url, future=True, pool_pre_ping=True)
    if reset:
        reset_schema(engine)
    return engine


def make_isolated_engine(suffix: str):
    """A separate, freshly created database for a module that wants its own.

    Some test modules build a private engine so their fixtures cannot see rows
    created by the shared session fixture. Under SQLite that was
    `sqlite:///:memory:`; under PostgreSQL it is a dedicated database.
    """
    base = test_database_url()
    name = f"{urlsplit(base).path.lstrip('/')}_{suffix}"
    url = _with_database(base, name)
    engine = make_engine(url, reset=True)
    return engine, sessionmaker(bind=engine, autocommit=False, autoflush=False)
