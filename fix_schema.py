"""One-off repair + verification for the counterparty_memory table.

Run from the PROJECT folder:

    python fix_schema.py

Safe to run more than once. It only adds what is missing and never drops
anything, so your saved counterparty decisions are untouched.
"""

import sys

from sqlalchemy import inspect, text

from app.database.session import engine

TABLE = "counterparty_memory"


def main() -> int:
    inspector = inspect(engine)

    if TABLE not in set(inspector.get_table_names()):
        print(f"ERROR: table '{TABLE}' does not exist at all.")
        print("Run:  python -m alembic upgrade head")
        return 1

    columns = {c["name"] for c in inspector.get_columns(TABLE)}
    print("columns before:", ", ".join(sorted(columns)))

    statements = []
    # An early build named this column 'purpose'. Rename rather than add, so
    # decisions already stored under it are kept.
    if "purpose" in columns and "category" not in columns:
        statements.append(
            f"ALTER TABLE {TABLE} RENAME COLUMN purpose TO category"
        )
    if "kind" not in columns:
        statements.append(
            f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS kind "
            f"VARCHAR(16) NOT NULL DEFAULT 'counterparty'"
        )

    if not statements:
        print("nothing to do - schema is already correct")
    else:
        with engine.begin() as conn:
            for sql in statements:
                print("running:", sql)
                conn.execute(text(sql))

    columns = {c["name"] for c in inspect(engine).get_columns(TABLE)}
    print("columns after :", ", ".join(sorted(columns)))

    ok = "category" in columns and "kind" in columns and "purpose" not in columns
    print("RESULT:", "OK - restart the server now" if ok else "STILL WRONG")

    if ok:
        with engine.connect() as conn:
            saved = conn.execute(
                text(f"SELECT count(*) FROM {TABLE}")
            ).scalar()
        print(f"saved counterparty decisions preserved: {saved}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
