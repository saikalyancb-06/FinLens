"""Bring any database to the current schema, whatever state it is in.

Why this exists: a fresh clone of the repo used to fail on someone else's
machine in four different ways —

1. ``database "backend_db" does not exist``: nothing created it.
2. ``alembic upgrade head`` after the first start: ``DuplicateColumn`` —
   the app had already built the tables (``DB_AUTO_CREATE``), and the
   migration chain cannot run on top of them (revision 001 assumes tables
   that only ``create_all`` builds).
3. ``scripts/db_migrate.py``: refused, "tables but no alembic_version".
4. A database built by an older checkout: ``UndefinedColumn`` on the newest
   columns (e.g. ``reconciliation_runs.book_opening_source``), because
   ``create_all`` creates missing *tables* but never adds missing *columns*.

The rule here: create the database if it is missing, create missing tables,
add missing columns (only ever additive — nothing is dropped or rewritten),
relax a NOT NULL the models no longer require, and record the result in
``alembic_version`` so ``alembic upgrade head`` is a no-op afterwards and
later migrations apply normally.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from sqlalchemy import Enum as SAEnum
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------- database

def ensure_database_exists(url: str) -> bool:
    """CREATE DATABASE when the server is up but the database is not there.

    Returns True when it created one. A server that is down or rejects the
    password is left alone: the normal connection error then says so.
    """
    try:
        u = make_url(url)
    except Exception:
        return False
    name = u.database
    if not name:
        return False
    admin = None
    try:
        admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT",
                              connect_args={"connect_timeout": 5})
        with admin.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"),
                                  {"n": name}).scalar()
            if exists:
                return False
            conn.execute(text(f'CREATE DATABASE "{name}"'))
            logger.warning("[Database] database '%s' did not exist — created it.", name)
            return True
    except (OperationalError, ProgrammingError) as exc:
        logger.debug("[Database] could not check/create database '%s': %s", name, exc)
        return False
    finally:
        if admin is not None:
            admin.dispose()


# ----------------------------------------------------------------------- columns

def _default_sql(col, dialect) -> str:
    sd = col.server_default
    if sd is None or not hasattr(sd, "arg"):
        return ""
    arg = sd.arg
    if isinstance(arg, str):
        return " DEFAULT '" + arg.replace("'", "''") + "'"
    try:
        return " DEFAULT " + str(arg.compile(dialect=dialect))
    except Exception:
        return ""


def add_missing_columns(engine, metadata) -> List[str]:
    """Add every model column the live tables lack; relax dropped NOT NULLs.

    Additive only. A new column is added nullable (with its server default
    when it has one), so existing rows stay valid.
    """
    insp = inspect(engine)
    live_tables = set(insp.get_table_names())
    changes: List[str] = []
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            if table.name not in live_tables:
                continue                        # create_all builds whole tables
            live = {c["name"]: c for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in live:
                    if isinstance(col.type, SAEnum):
                        col.type.create(bind=conn, checkfirst=True)
                    type_sql = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS "{col.name}" '
                        f'{type_sql}{_default_sql(col, engine.dialect)}'))
                    changes.append(f"added {table.name}.{col.name}")
                elif col.nullable and not live[col.name]["nullable"] and not col.primary_key:
                    conn.execute(text(
                        f'ALTER TABLE "{table.name}" ALTER COLUMN "{col.name}" DROP NOT NULL'))
                    changes.append(f"made {table.name}.{col.name} nullable")
    for c in changes:
        logger.warning("[Database] schema repair: %s", c)
    return changes


# ----------------------------------------------------------------------- alembic

def _alembic_config(url: str):
    from alembic.config import Config

    # No config file name on purpose: with one, env.py runs logging.fileConfig,
    # which would switch off the application's own loggers mid-start.
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _head_revision(cfg) -> Optional[str]:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(cfg).get_current_head()


def _current_revision(engine) -> Optional[str]:
    if "alembic_version" not in inspect(engine).get_table_names():
        return None
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _stamp(engine, revision: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version "
                          "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": revision})


def prepare_schema(engine, metadata, url: str) -> str:
    """Make the database match the code. Returns what it did, for the log.

    * empty / tables but never versioned -> create_all + add columns + stamp head
    * versioned, behind                  -> alembic upgrade head (then repair)
    * versioned, at head                 -> add any columns still missing
    """
    import os

    cfg = _alembic_config(url)
    head = _head_revision(cfg)
    current = _current_revision(engine)

    if current is None:
        metadata.create_all(bind=engine)
        changes = add_missing_columns(engine, metadata)
        if head:
            _stamp(engine, head)
        return f"schema built from the models and recorded as revision {head}" + (
            f" ({len(changes)} column repair(s))" if changes else "")

    if head and current != head:
        os.environ.setdefault("DATABASE_URL", url)
        from alembic import command
        try:
            command.upgrade(cfg, "head")
            action = f"migrated {current} -> {head}"
        except Exception as exc:                     # noqa: BLE001
            # A migration written for a hand-built database can trip over a
            # schema create_all already produced. The columns are repaired
            # below either way, so record head and carry on.
            logger.warning("[Database] alembic upgrade %s -> %s failed (%s); repairing the "
                           "schema from the models instead.", current, head, exc)
            action = f"repaired {current} -> {head}"
        metadata.create_all(bind=engine)
        add_missing_columns(engine, metadata)
        _stamp(engine, head)
        return action

    metadata.create_all(bind=engine)
    changes = add_missing_columns(engine, metadata)
    return f"up to date at {current}" + (f" ({len(changes)} column repair(s))" if changes else "")
