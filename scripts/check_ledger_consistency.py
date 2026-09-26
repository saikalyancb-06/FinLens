"""Report divergence between the canonical ledger and the classifier staging table.

Read-only. Safe to run on production and in CI.

    python -m scripts.check_ledger_consistency
    python -m scripts.check_ledger_consistency --strict   # exit 1 on divergence

Architecture this enforces (see docs/ARCHITECTURE_TRANSACTIONS.md):

    transactions            THE LEDGER. Every financial figure comes from here.
    processed_transactions  Append-only classifier audit log. Never read for
                            money, reporting, or reconciliation.

Divergence means a file was parsed into staging but never reached the ledger, so
its money is missing from every report while still appearing to have imported
successfully.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text  # noqa: E402

from app.database.session import engine  # noqa: E402


CHECKS = [
    (
        "staging_only_users",
        "Users with staged rows but no ledger rows",
        """
        SELECT COUNT(*) FROM (
          SELECT p.user_id FROM processed_transactions p
          GROUP BY p.user_id
          HAVING (SELECT COUNT(*) FROM transactions t WHERE t.user_id = p.user_id) = 0
        )
        """,
        "Their parsed statements never reached the ledger; money is absent from all reports.",
    ),
    (
        "staging_only_rows",
        "Staged rows belonging to those users",
        """
        SELECT COALESCE(SUM(n), 0) FROM (
          SELECT COUNT(*) n FROM processed_transactions p
          GROUP BY p.user_id
          HAVING (SELECT COUNT(*) FROM transactions t WHERE t.user_id = p.user_id) = 0
        )
        """,
        "",
    ),
    (
        "staged_files_without_statement",
        "Staged rows whose file has no Statement",
        """
        SELECT COUNT(*) FROM processed_transactions p
        WHERE p.file_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM statements s WHERE s.uploaded_file_id = p.file_id)
        """,
        "Statement creation is the precondition for the ledger write (parsing_queue.py).",
    ),
    (
        "ledger_rows_without_prediction",
        "Ledger rows with no classification record",
        "SELECT COUNT(*) FROM transactions t WHERE NOT EXISTS "
        "(SELECT 1 FROM predictions p WHERE p.transaction_id = t.id)",
        "These cannot appear in the review queue and have no provenance.",
    ),
    (
        "uncategorized_not_flagged",
        "Uncategorized ledger rows not flagged for review",
        """
        SELECT COUNT(*) FROM transactions t
        LEFT JOIN predictions p ON p.transaction_id = t.id
        WHERE t.category_id IS NULL
          AND t.superseded_by_id IS NULL
          AND (p.requires_review IS NULL OR p.requires_review = 0)
        """,
        "They are invisible to the reviewer: no category and no review flag.",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any divergence is found")
    args = parser.parse_args()

    print("=" * 78)
    print("LEDGER / STAGING CONSISTENCY")
    print(f"Database: {engine.url}")
    print("=" * 78)

    results = {}
    with engine.connect() as conn:
        ledger = conn.execute(text("SELECT COUNT(*) FROM transactions")).scalar()
        staging = conn.execute(text("SELECT COUNT(*) FROM processed_transactions")).scalar()
        print(f"\n  transactions (ledger)        : {ledger}")
        print(f"  processed_transactions (log) : {staging}")
        print("\n  Note: these counts are NOT expected to match. Staging is append-only")
        print("  per parse; the ledger is deduplicated and account-scoped.\n")

        for key, label, sql, why in CHECKS:
            value = conn.execute(text(sql)).scalar() or 0
            results[key] = value
            status = "OK" if value == 0 else "DIVERGENCE"
            print(f"  [{status:^10}] {label:<46} {value}")
            if value and why:
                print(f"               {why}")

    divergent = {k: v for k, v in results.items() if v}
    print()
    if not divergent:
        print("  No divergence. The ledger is the single source of truth.")
        return 0

    print(f"  {len(divergent)} check(s) reported divergence.")
    print("  Staged-but-unledgered rows are recoverable: re-ingest the source file")
    print("  with a resolvable user. Do NOT copy staging rows into the ledger")
    print("  directly - they carry no account binding, and guessing one misattributes money.")
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
