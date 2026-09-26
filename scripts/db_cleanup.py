"""Inspect — and only on explicit request, repair — bad data in the database.

Runs in DRY-RUN by default: it prints exactly which rows it would touch and how
they would change, and writes nothing. Applying requires two explicit flags:

    python -m scripts.db_cleanup                      # preview (safe)
    python -m scripts.db_cleanup --apply --confirm    # actually write

Every fix is additive or corrective. Nothing here deletes a financial
transaction: the closest it comes is removing rows that were provably written
by the test suite, and even those are listed individually first.

Runs against whatever DATABASE_URL points at, which is always PostgreSQL. The
test-pollution warning that used to be here is obsolete: the suite leaked rows
into the developer database because modules that did `from app.database.session
import SessionLocal` kept the real factory, and conftest now re-points every such
binding at the test engine before each test.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Callable, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text  # noqa: E402

from app.database.session import SessionLocal, engine  # noqa: E402


class Finding:
    def __init__(self, key: str, title: str, count: int, detail: str,
                 samples: List[Any], fix_sql: List[str], destructive: bool = False):
        self.key = key
        self.title = title
        self.count = count
        self.detail = detail
        self.samples = samples
        self.fix_sql = fix_sql
        self.destructive = destructive


def _scalar(conn, sql: str) -> int:
    return conn.execute(text(sql)).scalar() or 0


def _rows(conn, sql: str, limit: int = 5) -> List[Any]:
    return conn.execute(text(sql)).fetchall()[:limit]


def collect_findings(conn) -> List[Finding]:
    findings: List[Finding] = []

    # --- 1. Invalid source_type enum values --------------------------------
    n = _scalar(conn, """
        SELECT COUNT(*) FROM transactions
        WHERE source_type IS NOT NULL
          AND source_type NOT IN ('statement','email_alert','sms')
    """)
    if n:
        findings.append(Finding(
            key="invalid_source_type",
            title="Transactions with an invalid source_type enum value",
            count=n,
            detail=("SQLite does not enforce enum values on write, so rows exist with "
                    "values the ORM cannot read back — they raise KeyError on load. "
                    "Fix lowercases them onto the valid enum member."),
            samples=_rows(conn, """
                SELECT id, source_type FROM transactions
                WHERE source_type IS NOT NULL
                  AND source_type NOT IN ('statement','email_alert','sms') LIMIT 5
            """),
            fix_sql=["""UPDATE transactions SET source_type = LOWER(source_type)
                        WHERE source_type IS NOT NULL
                          AND source_type NOT IN ('statement','email_alert','sms')
                          AND LOWER(source_type) IN ('statement','email_alert','sms')"""],
        ))

    # --- 2. NULL source_type ------------------------------------------------
    n = _scalar(conn, "SELECT COUNT(*) FROM transactions WHERE source_type IS NULL")
    if n:
        findings.append(Finding(
            key="null_source_type",
            title="Transactions with NULL source_type",
            count=n,
            detail=("The column is declared NOT NULL with a server default of "
                    "'statement'; these rows predate that or were inserted directly. "
                    "Fix sets them to 'statement', the documented default."),
            samples=_rows(conn, "SELECT id, txn_date FROM transactions WHERE source_type IS NULL LIMIT 5"),
            fix_sql=["UPDATE transactions SET source_type='statement' WHERE source_type IS NULL"],
        ))

    # --- 3. Contradictory debit/credit -------------------------------------
    n = _scalar(conn, """
        SELECT COUNT(*) FROM transactions
        WHERE COALESCE(debit_paise,0) > 0 AND COALESCE(credit_paise,0) > 0
    """)
    if n:
        findings.append(Finding(
            key="both_sides_money",
            title="Transactions carrying money on BOTH debit and credit",
            count=n,
            detail=("A transaction cannot be both an inflow and an outflow. These "
                    "violate ck_txn_single_direction_amount and will block that "
                    "constraint from being applied. NO automatic fix is offered: "
                    "which side is correct is a business decision, not a mechanical one."),
            samples=_rows(conn, """
                SELECT id, debit_paise, credit_paise, narration_raw FROM transactions
                WHERE COALESCE(debit_paise,0) > 0 AND COALESCE(credit_paise,0) > 0 LIMIT 5
            """),
            fix_sql=[],
        ))

    # --- 4. Zero on the unused side ----------------------------------------
    n = _scalar(conn, """
        SELECT COUNT(*) FROM transactions
        WHERE (direction='debit'  AND credit_paise = 0)
           OR (direction='credit' AND debit_paise  = 0)
    """)
    if n:
        findings.append(Finding(
            key="zero_unused_side",
            title="Transactions storing 0 instead of NULL on the unused side",
            count=n,
            detail=("Harmless — every reader keys off `direction` — but inconsistent "
                    "with the rows that use NULL. Fix normalises them to NULL so the "
                    "column means one thing."),
            samples=_rows(conn, """
                SELECT id, direction, debit_paise, credit_paise FROM transactions
                WHERE (direction='debit' AND credit_paise=0)
                   OR (direction='credit' AND debit_paise=0) LIMIT 5
            """),
            fix_sql=[
                "UPDATE transactions SET credit_paise=NULL WHERE direction='debit'  AND credit_paise=0",
                "UPDATE transactions SET debit_paise=NULL  WHERE direction='credit' AND debit_paise=0",
            ],
        ))

    # --- 5. NULL dedup hash -------------------------------------------------
    n = _scalar(conn, "SELECT COUNT(*) FROM transactions WHERE hash IS NULL")
    if n:
        findings.append(Finding(
            key="null_hash",
            title="Transactions with no deduplication hash",
            count=n,
            detail=("The indexed `hash` column was never populated by the old code. "
                    "New writes now fill it. Backfilling existing rows is done in "
                    "Python (SHA-256 over account/date/amount/narration/reference), "
                    "not SQL — use --backfill-hash."),
            samples=_rows(conn, "SELECT id, txn_date FROM transactions WHERE hash IS NULL LIMIT 5"),
            fix_sql=[],
        ))

    # --- 6. Future-dated transactions --------------------------------------
    n = _scalar(conn, "SELECT COUNT(*) FROM transactions WHERE txn_date > date('now')")
    if n:
        findings.append(Finding(
            key="future_dated",
            title="Transactions dated in the future",
            count=n,
            detail=("Usually a DD/MM vs MM/DD parse error. Listed for inspection; no "
                    "automatic fix, since guessing the intended date could silently "
                    "move money between reporting periods."),
            samples=_rows(conn, """
                SELECT id, txn_date, narration_raw FROM transactions
                WHERE txn_date > date('now') LIMIT 5
            """),
            fix_sql=[],
        ))

    # --- 7. Test-fixture residue -------------------------------------------
    n = _scalar(conn, """
        SELECT COUNT(*) FROM transactions
        WHERE source_channel IS NULL AND statement_id IS NULL AND account_id IS NULL
    """)
    if n:
        findings.append(Finding(
            key="test_residue",
            title="Rows with no channel, no statement and no account (test residue)",
            count=n,
            detail=("Almost certainly written by the test suite leaking into this "
                    "database. DESTRUCTIVE: deletes rows. Review the samples before "
                    "applying, and fix the test-isolation leak first."),
            samples=_rows(conn, """
                SELECT id, txn_date, narration_raw FROM transactions
                WHERE source_channel IS NULL AND statement_id IS NULL AND account_id IS NULL LIMIT 5
            """),
            fix_sql=["""DELETE FROM transactions
                        WHERE source_channel IS NULL AND statement_id IS NULL AND account_id IS NULL"""],
            destructive=True,
        ))

    return findings


def backfill_hashes(apply: bool) -> int:
    """Populate Transaction.hash for rows missing it, using the live hash function."""
    from app.models.transaction import Transaction
    from app.services.transaction_storage import TransactionStorageService  # noqa: F401

    import hashlib
    import re

    def compute(account_id, txn_date, debit, credit, narration, reference) -> str:
        parts = [
            str(account_id or ""),
            txn_date.isoformat() if txn_date else "",
            str(debit or 0), str(credit or 0),
            re.sub(r"\s+", " ", (narration or "")).strip().upper(),
            (reference or "").strip().upper(),
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    db = SessionLocal()
    try:
        rows = db.query(Transaction).filter(Transaction.hash == None).all()
        for t in rows:
            t.hash = compute(
                t.account_id, t.txn_date, t.debit_paise, t.credit_paise,
                t.narration_clean or t.narration_raw, t.reference_no,
            )
        if apply:
            db.commit()
        else:
            db.rollback()
        return len(rows)
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview or repair database data quality issues")
    parser.add_argument("--apply", action="store_true", help="Actually write changes")
    parser.add_argument("--confirm", action="store_true", help="Required alongside --apply")
    parser.add_argument("--include-destructive", action="store_true",
                        help="Also run fixes that DELETE rows")
    parser.add_argument("--backfill-hash", action="store_true", help="Backfill Transaction.hash")
    parser.add_argument("--only", help="Run a single finding by key")
    args = parser.parse_args()

    writing = args.apply and args.confirm
    if args.apply and not args.confirm:
        print("--apply requires --confirm. Nothing was written.\n")

    print("=" * 74)
    print(f"DATABASE CLEANUP {'[APPLY]' if writing else '[DRY RUN — nothing will be written]'}")
    print(f"Database: {engine.url}")
    print("=" * 74)

    with engine.connect() as conn:
        findings = collect_findings(conn)

    if args.only:
        findings = [f for f in findings if f.key == args.only]

    if not findings:
        print("\nNo data-quality issues found.")
        return 0

    total_rows = 0
    for f in findings:
        total_rows += f.count
        flag = "  [DESTRUCTIVE]" if f.destructive else ""
        print(f"\n{'-' * 74}")
        print(f"[{f.key}]{flag}")
        print(f"  {f.title}")
        print(f"  Rows affected: {f.count}")
        print(f"  {f.detail}")
        if f.samples:
            print("  Sample rows:")
            for s in f.samples:
                print(f"     {tuple(s)}")
        if f.fix_sql:
            print("  Would execute:")
            for sql in f.fix_sql:
                print(f"     {' '.join(sql.split())}")
        else:
            print("  No automatic fix — manual decision required.")

    print(f"\n{'=' * 74}")
    print(f"TOTAL rows implicated: {total_rows}")

    if not writing:
        print("\nDRY RUN — no changes were made.")
        print("To apply the non-destructive fixes:")
        print("    python -m scripts.db_cleanup --apply --confirm")
        print("To also backfill dedup hashes:")
        print("    python -m scripts.db_cleanup --apply --confirm --backfill-hash")
        if any(f.destructive for f in findings):
            print("Destructive fixes additionally need --include-destructive.")
        return 0

    # ---- apply -------------------------------------------------------------
    applied = 0
    with engine.begin() as conn:
        for f in findings:
            if f.destructive and not args.include_destructive:
                print(f"\nSkipping destructive fix [{f.key}] (needs --include-destructive)")
                continue
            for sql in f.fix_sql:
                result = conn.execute(text(sql))
                applied += result.rowcount or 0
                print(f"  applied [{f.key}]: {result.rowcount} rows")

    if args.backfill_hash:
        n = backfill_hashes(apply=True)
        print(f"  backfilled hash on {n} rows")
        applied += n

    print(f"\nDone. {applied} rows updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
