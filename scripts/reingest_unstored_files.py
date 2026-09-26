"""Re-ingest uploaded files that never reached the ledger.

Why this exists
---------------
Between 2026-08-18 17:21 and 17:42 the parsing job died on every statement with

    psycopg2.errors.UndefinedColumn: column counterparty_memory.kind does not exist

The failure handler then tried to record 'FAILED' by committing on the session
postgres had already poisoned, so the write never landed and those files were
left at 'PROCESSING' — the value set when the job started. /files/upload read
'PROCESSING' as already-ingested, so every re-upload of the same statement
returned the dead file id and queued nothing.

The schema is fixed and both code paths are fixed, but the rows from that
window are still sitting there with no transactions behind them. This walks
them and re-runs the parse.

Usage
-----
    python scripts/reingest_unstored_files.py              # report only
    python scripts/reingest_unstored_files.py --apply      # re-parse them
    python scripts/reingest_unstored_files.py --apply --user someone@example.com

Re-parsing is safe to repeat: the ledger write is keyed by statement and
replaces that statement's rows rather than appending to them.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database.session import SessionLocal
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.models.uploaded_file import UploadedFile
from app.models.user import User


def ledger_rows(db, file_id: str) -> int:
    return (
        db.query(Transaction.id)
        .join(Statement, Transaction.statement_id == Statement.id)
        .filter(Statement.uploaded_file_id == file_id)
        .count()
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually re-parse; without it this only reports")
    ap.add_argument("--user", help="limit to one user's email")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        q = db.query(UploadedFile)
        if args.user:
            user = db.query(User).filter(User.email == args.user).first()
            if not user:
                print(f"No user with email {args.user!r}")
                return 1
            q = q.filter(UploadedFile.user_id == user.id)

        stuck = []
        for f in q.order_by(UploadedFile.uploaded_at).all():
            rows = ledger_rows(db, f.id)
            if rows == 0:
                stuck.append((f, rows))

        if not stuck:
            print("Nothing to do: every uploaded file has transactions in the ledger.")
            return 0

        print(f"{len(stuck)} uploaded file(s) have no transactions in the ledger:\n")
        for f, rows in stuck:
            exists = "on disk" if f.file_path and os.path.exists(f.file_path) else "MISSING FROM DISK"
            print(f"  {f.uploaded_at}  {f.status:<11} {f.filename}  [{exists}]")
            print(f"      file_id={f.id}  path={f.file_path}")

        if not args.apply:
            print("\nReport only. Re-run with --apply to re-parse these files.")
            return 0

        from app.services.parsing_queue import process_file_parsing_task

        print("\nRe-parsing:\n")
        ok = failed = skipped = 0
        for f, _ in stuck:
            if not f.file_path or not os.path.exists(f.file_path):
                print(f"  SKIP    {f.filename}: stored copy is gone; re-upload it from the UI.")
                skipped += 1
                continue
            summary = process_file_parsing_task(f.id, f.file_path, f.user_id)
            if summary.get("status") == "COMPLETED":
                print(f"  OK      {f.filename}: stored {summary.get('total_stored')} transaction(s)")
                ok += 1
            else:
                print(f"  FAILED  {f.filename}: {summary.get('error_message') or summary.get('error')}")
                failed += 1

        print(f"\n{ok} re-ingested, {failed} failed, {skipped} skipped.")
        return 0 if failed == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
