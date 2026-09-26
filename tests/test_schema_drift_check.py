"""The startup check that would have caught this before it reached a user.

TWICE a column was added to a model and to an alembic revision that had ALREADY
been applied. Alembic stamps a revision in `alembic_version` the first time it
runs and never reads that file again, so `alembic upgrade head` reported
"nothing to do" while the database was missing the column. It surfaced later,
from inside an endpoint, as

    psycopg2.errors.UndefinedColumn: column counterparty_memory.kind does not exist

These tests pin the checker that turns that into a startup error naming the
column and the SQL to add it.
"""

import uuid

import pytest
from sqlalchemy import text

from app.database.schema_check import check_schema, log_schema_drift
from app.database.session import Base
from tests.conftest import test_engine


@pytest.fixture
def drifted_table():
    """A real table missing a column the model declares.

    Built by hand rather than by dropping a column from a live table, so the
    test cannot damage anything another test is using.
    """
    name = f"drift_probe_{uuid.uuid4().hex[:8]}"
    from sqlalchemy import Column, MetaData, String, Table
    metadata = Base.metadata

    table = Table(
        name, metadata,
        Column("id", String(36), primary_key=True),
        Column("present_column", String(40), nullable=True),
        # The model knows about this one; the database will not.
        Column("missing_column", String(16), nullable=False,
               server_default="counterparty"),
    )
    with test_engine.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE {name} (id VARCHAR(36) PRIMARY KEY, "
            f"present_column VARCHAR(40))"
        ))
    try:
        yield name, table
    finally:
        with test_engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        metadata.remove(table)


def test_a_column_the_database_lacks_is_reported(drifted_table):
    name, _table = drifted_table
    drift = check_schema(test_engine, Base)

    assert not drift.is_clean
    assert name in drift.missing_columns
    assert drift.missing_columns[name] == ["missing_column"]


def test_the_report_includes_runnable_sql(drifted_table):
    """A message that only says "something is wrong" costs another hour."""
    name, _table = drifted_table
    drift = check_schema(test_engine, Base)

    sql = "\n".join(drift.repair_sql)
    assert f"ALTER TABLE {name} ADD COLUMN missing_column" in sql
    assert "NOT NULL" in sql
    assert "DEFAULT" in sql


def test_a_table_missing_entirely_is_reported(drifted_table):
    name, table = drifted_table
    with test_engine.begin() as conn:
        conn.execute(text(f"DROP TABLE {name}"))
    drift = check_schema(test_engine, Base)
    assert name in drift.missing_tables


def test_a_schema_that_matches_the_models_is_clean():
    """No false alarm on a correct database, or the warning gets ignored."""
    drift = check_schema(test_engine, Base)
    assert drift.is_clean, (
        f"missing tables: {drift.missing_tables}, "
        f"missing columns: {drift.missing_columns}"
    )


def test_extra_columns_in_the_database_are_not_reported():
    """Only model-ahead-of-database matters.

    A database with columns no model knows about is untidy but harmless, and
    flagging it would fire during every mid-rollout deploy.
    """
    name = f"extra_probe_{uuid.uuid4().hex[:8]}"
    with test_engine.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE {name} (id VARCHAR(36) PRIMARY KEY, spare VARCHAR(10))"
        ))
    try:
        assert check_schema(test_engine, Base).is_clean
    finally:
        with test_engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {name}"))


def test_the_checker_never_takes_the_application_down(monkeypatch):
    """A bug in the guard must not become the reason nothing starts."""
    import app.database.schema_check as module

    def boom(*_a, **_kw):
        raise RuntimeError("inspector exploded")

    monkeypatch.setattr(module, "inspect", boom)

    drift = log_schema_drift(test_engine, Base)
    assert drift.error is not None


def test_the_counterparty_memory_columns_the_app_relies_on_are_present():
    """The specific regression, named.

    `category` and `kind` are the two columns that have gone missing on a real
    deployment. Both are added by migrations 008 and 009.
    """
    from sqlalchemy import inspect as sa_inspect

    columns = {c["name"] for c in
               sa_inspect(test_engine).get_columns("counterparty_memory")}
    assert "category" in columns
    assert "kind" in columns
    assert "purpose" not in columns, "the pre-rename column should be gone"
