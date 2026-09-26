#!/usr/bin/env python
"""Re-parse already-ingested statements from their original files.

Why this exists
---------------
`clean_ocr_text()` used to correct OCR confusions (O->0, I->1, l->1, S->5) on
any whitespace-delimited token that contained a digit ANYWHERE, rewriting the
whole token. Indian bank narrations put the reference number hard against the
merchant name, so the entire narration was one such token:

    NEFT-HDFCH00168015948-TOPSACK PACKAGING  ->  ...-T0P5ACK PACKAGING
    EBANK:WIB/1450085375/SHIVKUMAR VEG       ->  EBANK:W1B/.../5H1VKUMAR VEG
    .../79707/BOBCARD LIMITED                ->  .../79707/B0BCARD LIMITED

That is not cosmetic. The rule engine matches merchants BY NAME, so a corrupted
name misses its rule and the row is filed under the wrong category. CSV and
Excel uploads went through the same path, where no OCR was involved at all.

The corrector is fixed, but rows already in the ledger still hold the corrupted
narration — and re-running classification alone cannot help, because the
classifier would read the same corrupted string. The narration itself has to
come back, and the only place it still exists is the file the user uploaded.

What this does
--------------
For each COMPLETED uploaded file that still has its original on disk, re-runs
the ordinary parsing task. That is REPLACE, not append: the ledger write is
keyed by statement and `process_file_parsing_task` deletes that statement's
transactions before writing the new ones, so running this twice is the same as
running it once.

What it costs
-------------
Transactions are recreated, so they get new ids. Everything that points at them
is declared ON DELETE CASCADE (predictions, anomaly findings, policy
violations, duplicate matches) or ON DELETE SET NULL (reconciliation match
lines), so nothing is orphaned — but derived data does go and has to be rebuilt:

  * anomaly findings and policy violations   -> re-run the compliance scan
    (this script does it for you unless --no-scan)
  * manual categorisations made in the Review Queue -> LOST. The script counts
    them first and REFUSES to run if it finds any, unless you pass
    --discard-manual-categorisations, because they are a person's work and
    cannot be recovered from the file.

Usage
-----
    # Report only. This is the default and changes nothing.
    python scripts/reparse_statements.py

    # One user, still a report.
    python scripts/reparse_statements.py --user demo@kredo.in

    # Do it.
    python scripts/reparse_statements.py --user demo@kredo.in --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.database.session import SessionLocal  # noqa: E402
from app.models.prediction import Prediction  # noqa: E402
from app.models.transaction import Transaction  # noqa: E402
from app.models.uploaded_file import UploadedFile  # noqa: E402
from app.models.user import User  # noqa: E402

#: A digit sitting between letters inside one word, where the digit is one the
#: old corrector could have produced. `B0BCARD`, `MARUTH1`, `5H1VKUMAR`.
#: Heuristic on purpose — it is used to REPORT scale, never to decide anything.
CORRUPTION_SIGNATURE = re.compile(r"[A-Za-z][015][A-Za-z]")


def _corrupted(rows) -> int:
    return sum(
        1 for r in rows
        if CORRUPTION_SIGNATURE.search(r.narration_clean or r.narration_raw or "")
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--user", help="limit to one user's files, by email")
    ap.add_argument("--apply", action="store_true",
                    help="actually re-parse. Without this it only reports.")
    ap.add_argument("--no-scan", action="store_true",
                    help="skip the compliance re-scan afterwards")
    ap.add_argument("--discard-manual-categorisations", action="store_true",
                    help="proceed even though hand-made category decisions will be lost")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        q = db.query(UploadedFile).filter(UploadedFile.status == "COMPLETED")
        user = None
        if args.user:
            user = db.query(User).filter(User.email == args.user).first()
            if not user:
                print(f"No user with email {args.user!r}.")
                return 1
            q = q.filter(UploadedFile.user_id == user.id)
        files = q.order_by(UploadedFile.uploaded_at.asc()).all()

        if not files:
            print("No COMPLETED uploaded files found.")
            return 0

        print(f"{len(files)} completed file(s) to consider.\n")
        print(f"{'file':44} {'rows':>6} {'corrupt':>8} {'source on disk':>15}")

        actionable, skipped = [], []
        for f in files:
            rows = db.query(Transaction).filter(
                Transaction.user_id == f.user_id,
                Transaction.statement_id.isnot(None),
            ).all()
            # Narrow to this file's statement where we can.
            from app.models.statement import Statement
            stmt = db.query(Statement).filter(
                Statement.uploaded_file_id == f.id).first()
            rows = [r for r in rows if stmt and r.statement_id == stmt.id]
            bad = _corrupted(rows)
            on_disk = bool(f.file_path) and os.path.exists(f.file_path)
            print(f"{(f.filename or '?')[:44]:44} {len(rows):>6} "
                  f"{bad:>8} {'yes' if on_disk else 'MISSING':>15}")
            (actionable if on_disk else skipped).append((f, rows, bad))

        if skipped:
            print(f"\n{len(skipped)} file(s) skipped: the original upload is no longer "
                  f"on disk, so the true narration cannot be recovered from anywhere.")

        if not actionable:
            print("\nNothing to do.")
            return 0

        # Manual decisions are the one thing a re-parse cannot rebuild.
        all_ids = [r.id for _f, rows, _b in actionable for r in rows]
        manual = 0
        if all_ids:
            manual = db.query(Prediction).filter(
                Prediction.transaction_id.in_(all_ids),
                Prediction.classification_method == "manual",
            ).count()
        print(f"\nManual categorisations among these rows: {manual}")
        if manual and not args.discard_manual_categorisations:
            print("\nREFUSING to continue. Those are decisions a person made in the "
                  "Review Queue, and re-parsing deletes the rows they belong to. "
                  "Re-run with --discard-manual-categorisations if you accept that.")
            return 1

        total_rows = sum(len(rows) for _f, rows, _b in actionable)
        total_bad = sum(bad for _f, _rows, bad in actionable)
        print(f"\n{len(actionable)} file(s), {total_rows:,} transactions, "
              f"{total_bad:,} showing the corruption signature "
              f"({100 * total_bad / max(1, total_rows):.1f}%).")

        if not args.apply:
            print("\nDry run — nothing changed. Re-run with --apply to re-parse.")
            return 0

        from app.services.parsing_queue import process_file_parsing_task

        print()
        for f, rows, _bad in actionable:
            print(f"re-parsing {f.filename} ...", end=" ", flush=True)
            try:
                summary = process_file_parsing_task(
                    file_id=f.id, file_path=f.file_path, user_id=f.user_id,
                )
                print(f"{summary.get('status')} "
                      f"({summary.get('total_stored', 0)} stored)")
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
                print(f"FAILED: {exc}")

        if not args.no_scan:
            # Findings and violations cascaded away with the old rows, and the
            # ones that existed were derived from corrupted narrations anyway.
            from app.compliance.auto_scan import run_scan_safely
            user_ids = {f.user_id for f, _r, _b in actionable}
            for uid in user_ids:
                print(f"re-scanning compliance for user {uid} ...", end=" ", flush=True)
                fresh = SessionLocal()
                try:
                    result = run_scan_safely(fresh, uid)
                    print(f"{(result or {}).get('anomalies_found')} finding(s)")
                finally:
                    fresh.close()

        print("\nDone.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
