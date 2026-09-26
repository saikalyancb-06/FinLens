"""Populate purpose / event_type on existing transactions.

DRY-RUN BY DEFAULT. Adds the two columns if missing, then fills them from the
legacy category where that mapping is honest, and from the narration where the
legacy label is rail-named and therefore says nothing about purpose.

    python -m scripts.backfill_dual_labels                    # preview
    python -m scripts.backfill_dual_labels --apply --confirm  # write

Never overwrites an existing purpose. Never invents one: a row that cannot be
resolved is left blank so it surfaces for review rather than entering reports
under a guess.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import inspect, text  # noqa: E402

from app.categorization.dual_taxonomy import map_legacy  # noqa: E402
from app.categorization.purpose_rules import derive  # noqa: E402
from app.database.session import engine  # noqa: E402

NEW_COLUMNS = [("purpose", "VARCHAR(60)"), ("event_type", "VARCHAR(60)")]
INDEXES = [("ix_transactions_purpose", "purpose"), ("ix_transactions_event_type", "event_type")]


def ensure_columns(apply: bool) -> list:
    insp = inspect(engine)
    existing = {c["name"] for c in insp.get_columns("transactions")}
    todo = [(n, t) for n, t in NEW_COLUMNS if n not in existing]
    if not todo:
        return []
    if apply:
        with engine.begin() as conn:
            for name, ddl in todo:
                conn.execute(text(f"ALTER TABLE transactions ADD COLUMN {name} {ddl}"))
            for iname, col in INDEXES:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {iname} ON transactions ({col})"))
    return todo


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    args = ap.parse_args()
    writing = args.apply and args.confirm

    print("=" * 74)
    print(f"DUAL-LABEL BACKFILL {'[APPLY]' if writing else '[DRY RUN - nothing written]'}")
    print(f"Database: {engine.url}")
    print("=" * 74)

    todo = ensure_columns(writing)
    if todo:
        print(f"\nColumns to add (nullable, additive): {[n for n, _ in todo]}")
        if not writing:
            print("  (dry run - not added, so the preview below runs in-memory only)")

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT t.id, t.narration_clean, t.narration_raw, t.direction, c.name
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.superseded_by_id IS NULL
        """)).fetchall()

    from_legacy = from_narration = unresolved = 0
    purposes, events, sources = Counter(), Counter(), Counter()
    updates = []

    for tid, clean, raw, direction, legacy in rows:
        narration = clean or raw or ""
        purpose, event = map_legacy(legacy)
        source = "legacy category"

        if purpose is None:
            purpose, event, note = derive(narration, direction)
            source = f"narration ({note})" if purpose else "unresolved"

        if purpose:
            updates.append((tid, purpose, event))
            purposes[purpose] += 1
            events[event or "(none)"] += 1
            sources[source.split(" (")[0]] += 1
            if source.startswith("legacy"):
                from_legacy += 1
            else:
                from_narration += 1
        else:
            unresolved += 1

    total = len(rows)
    print(f"\nActive transactions      : {total}")
    print(f"  resolved from category : {from_legacy}")
    print(f"  resolved from narration: {from_narration}")
    print(f"  left blank for review  : {unresolved}")

    print("\nPURPOSE distribution (leads reports):")
    for p, n in purposes.most_common():
        print(f"   {n:>6}  {p}")

    print("\nEVENT TYPE distribution (optional):")
    for e, n in events.most_common():
        print(f"   {n:>6}  {e}")

    if not writing:
        print("\nDRY RUN - nothing written.")
        print("To apply:  python -m scripts.backfill_dual_labels --apply --confirm")
        return 0

    with engine.begin() as conn:
        for tid, purpose, event in updates:
            conn.execute(
                text("UPDATE transactions SET purpose=:p, event_type=:e "
                     "WHERE id=:i AND (purpose IS NULL OR purpose='')"),
                {"p": purpose, "e": event, "i": tid},
            )
    print(f"\nDone. {len(updates)} transactions labelled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
