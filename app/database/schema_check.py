"""Compare the SQLAlchemy models against the live database, at startup.

WHY THIS EXISTS. Twice, a column was added to a model and to an ALREADY-APPLIED
alembic revision. Alembic records a revision in `alembic_version` the first time
it runs and never reads that file again, so `alembic upgrade head` reported
"nothing to do" while the database was missing the column. The mismatch surfaced
much later, from inside an endpoint, in production, as:

    psycopg2.errors.UndefinedColumn: column counterparty_memory.kind does not exist

By then the user is looking at a 500 on a page that worked yesterday, and the
traceback points at the query rather than at the cause.

This check runs once at startup and reports the drift where it can be acted on,
with the exact SQL to fix it. It deliberately does NOT alter anything: a process
that silently rewrites its own schema on boot is how two servers on one database
corrupt each other. Reporting is the whole job.

It is also read-only and failure-tolerant. A problem in the checker must never
be the reason the application will not start.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# Rough SQL types for the ALTER TABLE hint. Close enough to paste and run;
# the migration remains the authoritative definition.
def _sql_type(column) -> str:
    try:
        return column.type.compile(dialect=None)
    except Exception:
        try:
            return str(column.type)
        except Exception:
            return "TEXT"


def _default_clause(column) -> str:
    default = getattr(column, "server_default", None)
    if default is None or getattr(default, "arg", None) is None:
        return ""
    arg = default.arg
    text = getattr(arg, "text", None) or str(arg)
    text = text.strip()
    if not text:
        return ""
    if not (text.startswith("'") or text.endswith(")") or text.isdigit()):
        text = f"'{text}'"
    return f" DEFAULT {text}"


@dataclass
class SchemaDrift:
    missing_tables: List[str] = field(default_factory=list)
    # table -> [column names the model has and the database does not]
    missing_columns: Dict[str, List[str]] = field(default_factory=dict)
    repair_sql: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def is_clean(self) -> bool:
        return not self.missing_tables and not self.missing_columns


def check_schema(engine: Engine, base) -> SchemaDrift:
    """Report model columns the database does not have.

    Only reports things the MODEL has and the database lacks — that is the
    direction that causes UndefinedColumn at query time. A database with extra
    columns is untidy but harmless, and flagging it would fire on every
    deployment mid-rollout.
    """
    drift = SchemaDrift()
    try:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())

        for table in base.metadata.sorted_tables:
            if table.name not in existing_tables:
                drift.missing_tables.append(table.name)
                continue

            db_columns = {c["name"] for c in inspector.get_columns(table.name)}
            missing = [c.name for c in table.columns if c.name not in db_columns]
            if not missing:
                continue

            drift.missing_columns[table.name] = missing
            for column in table.columns:
                if column.name not in missing:
                    continue
                nullable = "" if column.nullable else " NOT NULL"
                drift.repair_sql.append(
                    f"ALTER TABLE {table.name} ADD COLUMN {column.name} "
                    f"{_sql_type(column)}{nullable}{_default_clause(column)};"
                )
    except Exception as exc:  # noqa: BLE001 - see module docstring
        drift.error = str(exc)

    return drift


def log_schema_drift(engine: Engine, base) -> SchemaDrift:
    """Run the check and log the result. Never raises."""
    try:
        drift = check_schema(engine, base)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Schema] drift check failed, continuing: %s", exc)
        return SchemaDrift(error=str(exc))

    if drift.error:
        logger.warning("[Schema] drift check could not run: %s", drift.error)
        return drift

    if drift.is_clean:
        logger.info("[Schema] models and database agree")
        return drift

    if drift.missing_tables:
        logger.error(
            "[Schema] MISSING TABLES: %s. Run: alembic upgrade head",
            ", ".join(sorted(drift.missing_tables)),
        )
    for table, columns in sorted(drift.missing_columns.items()):
        logger.error(
            "[Schema] table '%s' is missing column(s) %s that the models "
            "expect. Any query touching them will fail with UndefinedColumn.",
            table, ", ".join(columns),
        )
    logger.error(
        "[Schema] Run 'alembic upgrade head'. If that reports nothing to do, a "
        "migration was edited after it had already been applied — alembic will "
        "not re-run it. Either add a NEW revision, or apply directly:\n%s",
        "\n".join(drift.repair_sql),
    )
    return drift
