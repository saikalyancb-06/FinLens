"""Consolidate bank statements from the command line — same engine as the API.

    python scripts/consolidate_statements.py "Creditlens/Case 3" -o case3.json
    python scripts/consolidate_statements.py a.pdf b.pdf c.pdf -o out.json
    python scripts/consolidate_statements.py Creditlens.zip -o out.json --password 1234

A folder is searched recursively for statement files; a .zip is unpacked
safely. Prints the summary and every flag; writes the full JSON.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("FX_REFRESH_ENABLED", "false")

EXTS = {".pdf", ".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls", ".json", ".ofx", ".qfx", ".xml"}


def collect(inputs, tmp):
    from app.b2b.consolidate.archive import extract_zip
    files = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in EXTS:
                    files.append((str(f), str(f.relative_to(p))))
        elif p.suffix.lower() == ".zip":
            files += extract_zip(str(p), tmp, max_member_bytes=100 << 20, max_total_bytes=1 << 30)
        elif p.is_file():
            files.append((str(p), p.name))
        else:
            sys.exit(f"Not found: {raw}")
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="statement files, folders or .zip archives")
    ap.add_argument("-o", "--output", default="consolidated.json")
    ap.add_argument("--password", help="password tried on every locked PDF")
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)

    from app.b2b.consolidate.service import consolidate
    tmp = tempfile.mkdtemp(prefix="consolidate_")
    try:
        files = collect(args.inputs, tmp)
        result = consolidate(files, default_password=args.password)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)

    s = result["summary"]
    print(f"{s['files_processed']}/{s['files_received']} files, {s['accounts']} accounts, "
          f"{s['transactions_extracted']} rows read, {s['duplicates_removed']} duplicates removed, "
          f"{s['transactions_output']} transactions, {s['internal_transfers']} internal transfers")
    print(f"Balance check: {s['balance_check']}")
    for a in result["accounts"]:
        print(f"  {a['bank_name'] or '?':22} {a['account_no']:>18}  {a['period_from']} → {a['period_to']}  "
              f"{a['transactions']:5} txns  {a['balance_check']}")
    for f in result["files"]:
        if f["status"] != "processed":
            print(f"  FILE {f['file_name']}: {f['status']} {f.get('error', {}).get('message', '')}")
    for f in result["flags"]:
        print(f"  FLAG {f['type']} {f.get('account_no') or ''} {f.get('date') or ''} "
              f"difference={f.get('difference')}  {f.get('message', '')}")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
