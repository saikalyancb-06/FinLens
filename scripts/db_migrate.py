"""Bring the database schema to head — safe to run on every deploy.

Used as Render's `preDeployCommand` (render.yaml), so a deploy never has to
remember the manual bootstrap from docs/B2B_DEPLOY_CHECKLIST.md:

  * empty database (no tables at all)  -> build every table from the models,
    then `alembic stamp head`. The migration chain cannot build a schema from
    nothing (revision 001 alters a table it assumes exists), which is why the
    runbook used to have a manual step for this.
  * database under Alembic control     -> `alembic upgrade head`.
  * tables but no alembic_version      -> a database built by DB_AUTO_CREATE:
    add whatever tables/columns are missing (additive only), then stamp head.

Exit code is non-zero on any failure, which makes Render abort the deploy and
keep the previous version serving — the correct outcome for a schema problem.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("FX_REFRESH_ENABLED", "false")
os.environ.setdefault("B2B_MAINTENANCE_ENABLED", "false")


def main() -> int:
    from sqlalchemy import inspect

    from alembic import command
    from alembic.config import Config

    from app.database.session import Base, engine, wait_for_database
    import app.models  # noqa: F401
    import app.aa.models  # noqa: F401
    import app.b2b.models  # noqa: F401

    wait_for_database(engine)
    tables = set(inspect(engine).get_table_names())
    cfg = Config(str(ROOT / "alembic.ini"))

    if not tables:
        print("[migrate] empty database: creating all tables, then stamping head")
        Base.metadata.create_all(bind=engine)
        command.stamp(cfg, "head")
    elif "alembic_version" in tables:
        print("[migrate] upgrading to head")
        command.upgrade(cfg, "head")
    else:
        # Built by DB_AUTO_CREATE (create_all) and never versioned. It matches
        # the models of whichever checkout built it, so: create missing tables,
        # add missing columns (additive only), then record head.
        print("[migrate] tables but no alembic_version: adopting it — adding missing "
              "tables/columns from the models, then stamping head")
        from app.database.bootstrap import add_missing_columns
        Base.metadata.create_all(bind=engine)
        for change in add_missing_columns(engine, Base.metadata):
            print(f"[migrate]   {change}")
        command.stamp(cfg, "head")

    with engine.connect() as conn:
        from sqlalchemy import text
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        count = len(inspect(engine).get_table_names())
    print(f"[migrate] done: revision {rev}, {count} tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
