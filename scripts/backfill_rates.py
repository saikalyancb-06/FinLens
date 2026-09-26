"""Populate the rate table for the dates your transactions actually fall on.

WHY THIS EXISTS SEPARATELY FROM THE LIVE REFRESHER
The background refresher stores today's rate. Conversion picks the newest rate
dated on or before each transaction's own date, so today's rate applies to
today's transactions and to nothing else. A ledger of historical rows keeps
converting at whatever the oldest stored rate is - the shipped placeholder -
no matter how often the live poller runs.

This walks the transaction dates that exist, asks each source for the daily
series covering them, and writes one row per currency per published day. After
it runs, a payment made in March converts at March's rate.

FBIL publishes India's official reference rate for USD, EUR, GBP, JPY and AED;
the general central-bank aggregate covers the rest. Rows keep their source tag,
so an official figure stays distinguishable from a market one.

Usage:
    python scripts/backfill_rates.py --dry-run
    python scripts/backfill_rates.py
    python scripts/backfill_rates.py --from 2024-04-01 --to 2026-08-18
    python scripts/backfill_rates.py --codes USD,EUR --chunk-days 60
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date as date_type, datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional, Sequence

from dotenv import load_dotenv

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()

from sqlalchemy import func, select                                   # noqa: E402

from app.currency.refresher import RefreshReport, store_quotes        # noqa: E402
from app.currency.service import BASE_CURRENCY, seed_currencies       # noqa: E402
from app.currency.sources.base import RateQuote, RateSourceError      # noqa: E402
from app.currency.sources.frankfurter import (                        # noqa: E402
    FbilRateSource, FrankfurterRateSource,
)
from app.database.session import SessionLocal                         # noqa: E402
from app.models.currency import Currency                              # noqa: E402
from app.models.transaction import Transaction                        # noqa: E402


def transaction_date_span(db) -> Optional[tuple]:
    row = db.execute(
        select(func.min(Transaction.txn_date), func.max(Transaction.txn_date))
    ).first()
    if not row or row[0] is None:
        return None
    return row[0], row[1]


def chunks(start: date_type, end: date_type, days: int):
    """Split a span into windows.

    A year of twelve currencies is a few thousand records, and a slow link times
    out on it. Chunking keeps both publishers on the same code path and means a
    failure loses one window, not the whole run.
    """
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=days - 1), end)
        yield cursor, stop
        cursor = stop + timedelta(days=1)


#: Stop halving a failed window below this. Past here the failure is not size.
MIN_CHUNK_DAYS = 7


def fetch_window(source, codes, start, end, report, depth=0):
    """Fetch a window, halving and retrying if it fails on size.

    A year of twelve currencies is a few thousand records, and a slow link times
    out on it. Reporting "nothing was stored" for what is really a too-large
    request wastes the user's afternoon, so a failure is retried as two smaller
    requests before it is believed.
    """
    try:
        return source.fetch_range(codes, start, end)
    except RateSourceError as exc:
        span = (end - start).days + 1
        if not getattr(exc, "retryable", False) or span <= MIN_CHUNK_DAYS or depth >= 4:
            report.errors.append(f"{source.name} {start}..{end}: {exc}")
            print(f"  {start} .. {end}  {source.name:<12} FAILED: {exc}")
            return []
        mid = start + timedelta(days=span // 2 - 1)
        print(f"  {start} .. {end}  {source.name:<12} retrying as two smaller windows")
        return (fetch_window(source, codes, start, mid, report, depth + 1)
                + fetch_window(source, codes, mid + timedelta(days=1), end, report, depth + 1))
    except Exception as exc:
        report.errors.append(
            f"{source.name} {start}..{end}: {type(exc).__name__}: {exc}")
        print(f"  {start} .. {end}  {source.name:<12} ERROR: {exc}")
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", help="YYYY-MM-DD (default: oldest transaction)")
    ap.add_argument("--to", dest="end", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--codes", help="Comma-separated currencies (default: all active)")
    ap.add_argument("--chunk-days", type=int, default=90)
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch and report, write nothing")
    ap.add_argument("--max-step", type=float, default=1.0,
                    help="Step-change limit between consecutive stored rates. "
                         "Defaults to 1.0 (100%%) because a multi-year backfill "
                         "legitimately spans large moves; the live refresher "
                         "uses the much tighter FX_MAX_STEP_CHANGE.")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        seed_currencies(db)
        db.commit()

        if args.codes:
            codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]
        else:
            codes = [c for c in db.execute(
                select(Currency.code).where(Currency.is_active.is_(True))
            ).scalars().all() if c != BASE_CURRENCY]

        span = transaction_date_span(db)
        if args.start:
            start = date_type.fromisoformat(args.start)
        elif span:
            start = span[0]
        else:
            print("No transactions and no --from given: nothing to backfill.")
            return 0
        end = date_type.fromisoformat(args.end) if args.end else min(
            span[1] if span else date_type.today(), date_type.today())

        if start > end:
            print(f"Nothing to do: start {start} is after end {end}.")
            return 0

        print(f"Backfilling {', '.join(codes)}")
        print(f"  span      : {start} .. {end}  ({(end - start).days + 1} days)")
        print(f"  chunk     : {args.chunk_days} days")
        print(f"  mode      : {'DRY RUN — nothing will be written' if args.dry_run else 'writing'}")
        print()

        sources = [FbilRateSource(), FrankfurterRateSource()]
        report = RefreshReport(started_at=datetime.now(timezone.utc))
        fetched_total = 0

        for chunk_start, chunk_end in chunks(start, end, args.chunk_days):
            outstanding = {c for c in codes}
            for source in sources:
                askable = [c for c in outstanding if c in source.supported]
                if not askable:
                    continue
                quotes = fetch_window(source, askable, chunk_start, chunk_end, report)
                if not quotes:
                    continue

                fetched_total += len(quotes)
                covered = {q.code for q in quotes}
                print(f"  {chunk_start} .. {chunk_end}  {source.name:<12} "
                      f"{len(quotes):>5} quotes  ({', '.join(sorted(covered)) or 'none'})")

                if quotes and not args.dry_run:
                    store_quotes(db, quotes, report, max_step=Decimal(str(args.max_step)))
                outstanding -= covered

            if outstanding:
                print(f"  {chunk_start} .. {chunk_end}  {'':<12} "
                      f"no source covered: {', '.join(sorted(outstanding))}")

        if args.dry_run:
            db.rollback()
        else:
            db.commit()

        print()
        print(f"fetched   : {fetched_total}")
        print(f"stored    : {len(report.updated)}")
        print(f"unchanged : {len(report.unchanged)}")
        print(f"rejected  : {len(report.rejected)}")
        for line in report.rejected[:15]:
            print(f"    {line}")
        if len(report.rejected) > 15:
            print(f"    ... and {len(report.rejected) - 15} more")
        print(f"errors    : {len(report.errors)}")
        for line in report.errors[:10]:
            print(f"    {line}")

        # A backfill that stored nothing is a failure even though it exited
        # cleanly, so say so with an exit code rather than a cheerful summary.
        if not args.dry_run and not report.updated and fetched_total == 0:
            print("\nNothing was stored. Check network access and the source errors above.")
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
