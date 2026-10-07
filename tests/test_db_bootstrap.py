"""A fresh clone must start on any database state (2026-10-07).

Covers what broke on another machine: an older schema missing new columns,
and a database built by create_all that alembic then could not upgrade.
"""
from sqlalchemy import inspect, text

from app.database.bootstrap import _alembic_config, _head_revision, add_missing_columns, prepare_schema
from app.database.session import Base
from tests.pgtestdb import make_isolated_engine


def _fresh(suffix):
    engine, _ = make_isolated_engine(suffix)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    return engine


def test_missing_columns_are_added_back():
    engine = _fresh("boot_cols")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as c:
        c.execute(text("ALTER TABLE reconciliation_runs DROP COLUMN book_opening_source"))
        c.execute(text("ALTER TABLE oauth_states DROP COLUMN result_status"))
    changes = add_missing_columns(engine, Base.metadata)
    assert "added reconciliation_runs.book_opening_source" in changes
    cols = {c["name"] for c in inspect(engine).get_columns("oauth_states")}
    assert "result_status" in cols
    engine.dispose()


def test_unversioned_database_is_adopted_and_alembic_is_a_no_op():
    engine = _fresh("boot_adopt")
    Base.metadata.create_all(bind=engine)                 # what DB_AUTO_CREATE leaves
    msg = prepare_schema(engine, Base.metadata, str(engine.url.render_as_string(hide_password=False)))
    assert "recorded as revision" in msg
    with engine.connect() as c:
        rev = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert rev == _head_revision(_alembic_config("postgresql://x/y"))
    assert prepare_schema(engine, Base.metadata, engine.url.render_as_string(hide_password=False)).startswith("up to date")
    engine.dispose()
