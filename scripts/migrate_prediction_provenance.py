"""Additive schema migration: classification provenance columns on `predictions`.

Dry-run by default. Prints the exact DDL it would execute and the current state
of each column, and writes nothing without --apply --confirm.

    python -m scripts.migrate_prediction_provenance              # preview
    python -m scripts.migrate_prediction_provenance --apply --confirm

Every column is nullable (or has a default), so this is purely additive: no
existing row is rewritten, no column is dropped or retyped, and the migration is
safe to run while the old code is still deployed — the old code simply ignores
the new columns.

Rollback: the added columns can be dropped individually; nothing else changes.
PostgreSQL does DROP COLUMN transactionally, so a rollback is a single statement.
Take a backup before applying anyway.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import inspect, text  # noqa: E402

from app.database.session import engine  # noqa: E402

TABLE = "predictions"

# (column, DDL type, rationale shown in the preview)
NEW_COLUMNS: List[Tuple[str, str, str]] = [
    ("classification_method", "VARCHAR(20)",
     "How the category was decided: rule|ml|hybrid|manual|none. Distinct from the "
     "transaction's source_channel, which records where the row came from."),
    ("model_name", "VARCHAR(100)", "Which model produced the prediction, when ML was used."),
    ("model_confidence", "FLOAT", "Raw model probability, kept separate from the final confidence."),
    ("rule_score", "INTEGER", "Deterministic rule score that supported the decision."),
    ("rule_category", "VARCHAR(100)", "What the rule engine proposed, even if ML won."),
    ("ml_category", "VARCHAR(100)", "What the model proposed, even if the rule won."),
    ("top_3", "JSON", "Top three categories with probabilities, for reviewer context."),
    ("explanation", "TEXT", "Human-readable reason shown in the review queue."),
    ("requires_review", "BOOLEAN DEFAULT 0 NOT NULL", "Flags rows the classifier declined to decide."),
    ("reviewed_at", "DATETIME", "When a human resolved it."),
    ("reviewed_by_user_id", "VARCHAR(32)", "Who resolved it."),
]

INDEXES = [
    ("ix_predictions_requires_review", "requires_review"),
    ("ix_predictions_classification_method", "classification_method"),
]


def existing_columns() -> Dict[str, str]:
    insp = inspect(engine)
    if TABLE not in insp.get_table_names():
        return {}
    return {c["name"]: str(c["type"]) for c in insp.get_columns(TABLE)}


def existing_indexes() -> List[str]:
    insp = inspect(engine)
    if TABLE not in insp.get_table_names():
        return []
    return [i["name"] for i in insp.get_indexes(TABLE)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    writing = args.apply and args.confirm
    if args.apply and not args.confirm:
        print("--apply requires --confirm. Nothing was written.\n")

    print("=" * 78)
    print(f"MIGRATION: {TABLE} provenance columns "
          f"{'[APPLY]' if writing else '[DRY RUN - nothing will be written]'}")
    print(f"Database: {engine.url}")
    print("=" * 78)

    cols = existing_columns()
    if not cols:
        print(f"\nTable '{TABLE}' does not exist. Nothing to migrate — "
              "create_all() will build it with the new columns already present.")
        return 0

    print(f"\nCurrent columns ({len(cols)}): {', '.join(sorted(cols))}")

    to_add = [(n, t, why) for n, t, why in NEW_COLUMNS if n not in cols]
    already = [n for n, _, _ in NEW_COLUMNS if n in cols]

    if already:
        print(f"\nAlready present, will be skipped: {', '.join(already)}")

    if not to_add:
        print("\nNothing to add — schema is already up to date.")
    else:
        print(f"\n{len(to_add)} column(s) would be ADDED. All are nullable or defaulted,")
        print("so no existing row is modified and no data is lost:\n")
        for name, ddl_type, why in to_add:
            print(f"  ALTER TABLE {TABLE} ADD COLUMN {name} {ddl_type};")
            print(f"        -> {why}\n")

    idx_present = existing_indexes()
    idx_to_add = [(n, c) for n, c in INDEXES if n not in idx_present]
    if idx_to_add:
        print("Indexes that would be created (review-queue lookups scan these):\n")
        for name, col in idx_to_add:
            print(f"  CREATE INDEX {name} ON {TABLE} ({col});")
        print()

    try:
        with engine.connect() as conn:
            row_count = conn.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()
        print(f"Rows in {TABLE}: {row_count} — every one keeps its current values; "
              "new columns start NULL (requires_review starts 0).")
    except Exception as exc:
        print(f"Could not count rows: {exc}")

    if not writing:
        print("\nDRY RUN — no changes were made.")
        print("To apply:")
        print("    python -m scripts.migrate_prediction_provenance --apply --confirm")
        print("\nBack up the database file first if it holds anything you cannot regenerate.")
        return 0

    applied = 0
    with engine.begin() as conn:
        for name, ddl_type, _ in to_add:
            conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {name} {ddl_type}"))
            print(f"  added column {name}")
            applied += 1
        for name, col in idx_to_add:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {TABLE} ({col})"))
            print(f"  created index {name}")

    print(f"\nDone. {applied} column(s) added.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
