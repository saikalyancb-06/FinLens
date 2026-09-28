"""Alembic environment configuration.

Connects to the PostgreSQL database named by DATABASE_URL and imports all
SQLAlchemy model metadata so that autogenerate can detect schema differences.

The URL is read from the environment rather than alembic.ini so that migrations
always run against the same database the application uses — including inside
docker-compose, where the host is `postgres` rather than `localhost`.
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Make the project root importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv

load_dotenv()

# Import Base + all models so their metadata is registered
from app.database.session import Base
import app.models  # noqa: F401 — registers all ORM models onto Base.metadata

# Alembic Config object (provides access to alembic.ini values)
config = context.config

# Interpret logging config from ini file
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def get_url() -> str:
    """Resolve the migration target URL, preferring the environment."""
    url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Alembic needs the PostgreSQL URL of the "
            "database to migrate, e.g. "
            "DATABASE_URL=postgresql://postgres:postgres@localhost:5432/backend_db"
        )
    scheme = url.split("://", 1)[0].lower()
    if not scheme.startswith("postgres"):
        raise RuntimeError(
            f"Refusing to run migrations against a '{scheme}' database. This project "
            "is PostgreSQL-only; the migration scripts assume PostgreSQL semantics."
        )
    # Same driver pinning as the application (psycopg2), so `alembic upgrade`
    # does not need a second PostgreSQL driver installed under SQLAlchemy 2.1+.
    from app.database.session import normalize_postgres_url
    return normalize_postgres_url(url)


# Feed ORM metadata to Alembic for autogenerate
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DB connection)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (live DB connection)."""
    section = config.get_section(config.config_ini_section, {}) or {}
    section["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # PostgreSQL supports transactional DDL and real ALTER TABLE, so the
            # SQLite batch-mode table rebuild is neither needed nor wanted here.
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
