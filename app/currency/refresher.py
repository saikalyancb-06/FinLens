"""Pull rates from the configured sources, vet them, and store what survives.

The vetting is the point of this module. Fetching is three lines; deciding what
deserves to be written is the rest, because a wrong rate does not look wrong. It
does not raise, it does not appear in a log the user reads, and it does not
break a page. It quietly multiplies every converted figure on the Transactions
tab by the wrong number, and it keeps doing that until somebody notices a total
they cannot explain.

So a fetched rate is written only if it is positive, inside a plausible band,
and within a configured step of the last rate stored for that currency.
Everything else is rejected, kept in the run report, and logged - the previous
rate stays in place, which is stale but true, rather than fresh but wrong.

ROUTING
FBIL - India's official reference rate publisher since 2018 - is asked first,
for the five currencies it covers. The general central-bank aggregate is asked
only for what FBIL did not answer. That ordering is not about reliability: it is
because the FBIL rate is the one an Indian auditor expects, and falling back to
a market rate when the official one is available would quietly downgrade the
provenance of the number.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date as date_type, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.currency.service import BASE_CURRENCY
from app.currency.sources.base import RateQuote, RateSourceError, validate
from app.currency.sources.frankfurter import FbilRateSource, FrankfurterRateSource
from app.models.currency import Currency, CurrencyRate

logger = logging.getLogger(__name__)


@dataclass
class RefreshReport:
    started_at: datetime
    finished_at: Optional[datetime] = None
    updated: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    #: Currencies where a fetched rate differs from one the user typed in.
    #: Reported rather than applied, so the choice stays theirs.
    conflicts: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "rejected": self.rejected,
            "errors": self.errors,
            "conflicts": self.conflicts,
            "ok": not self.errors and bool(self.updated or self.unchanged),
        }


# Last run's outcome, for the status endpoint. Deliberately in memory: it
# describes this process's refresher, and a restart genuinely has not run yet.
_LAST_REPORT: Optional[RefreshReport] = None
_LOCK = threading.Lock()


def last_report() -> Optional[dict]:
    with _LOCK:
        return _LAST_REPORT.as_dict() if _LAST_REPORT else None


def _record(report: RefreshReport) -> None:
    global _LAST_REPORT
    with _LOCK:
        _LAST_REPORT = report


# Consecutive refusals per source, and when each is allowed to be tried again.
# A source that has said no three times will say no the fourth time too; asking
# anyway every 30 minutes is pointless traffic aimed at someone else's server and
# it buries the real errors in a log full of the same one.
_FAILURES: Dict[str, int] = {}
_COOLDOWN_UNTIL: Dict[str, datetime] = {}


def build_sources() -> List[object]:
    """FBIL first, then the general aggregate for what it does not publish.

    Order is provenance, not reliability. FBIL is India's official benchmark, so
    taking a market rate for a currency FBIL publishes would silently downgrade
    a number that may end up in a filing.
    """
    timeout = settings.FX_HTTP_TIMEOUT
    return [FbilRateSource(timeout=timeout), FrankfurterRateSource(timeout=timeout)]


def _in_cooldown(name: str) -> Optional[datetime]:
    until = _COOLDOWN_UNTIL.get(name)
    if until and datetime.now(timezone.utc) < until:
        return until
    return None


def _note_failure(name: str, retryable: bool) -> None:
    """Count a refusal. Timeouts do not count - those are worth retrying."""
    if retryable:
        return
    _FAILURES[name] = _FAILURES.get(name, 0) + 1
    if _FAILURES[name] >= settings.FX_SOURCE_FAILURE_LIMIT:
        _COOLDOWN_UNTIL[name] = datetime.now(timezone.utc) + timedelta(
            hours=settings.FX_SOURCE_COOLDOWN_HOURS)
        logger.warning(
            "[FX] %s has refused %d times in a row - not asking again for %sh",
            name, _FAILURES[name], settings.FX_SOURCE_COOLDOWN_HOURS)


def _note_success(name: str) -> None:
    _FAILURES.pop(name, None)
    _COOLDOWN_UNTIL.pop(name, None)


def source_health() -> dict:
    """Which sources are currently being skipped, and until when."""
    return {
        name: {
            "consecutive_failures": count,
            "skipped_until": _COOLDOWN_UNTIL[name].isoformat()
            if name in _COOLDOWN_UNTIL else None,
        }
        for name, count in _FAILURES.items()
    }


#: Rate origins that cannot serve as a baseline for the step-change gate.
#:
#: `seed` is a placeholder that shipped in a source file, dated to no real day.
#: It has no authority to protect, and using it as a baseline rejected the first
#: real rate for six of twelve currencies.
#:
#: `manual` is excluded for a subtler reason. The gate answers one question -
#: "has this source started returning nonsense?" - and that question only means
#: anything compared against what the same source said last time. Comparing a
#: fetched rate to a hand-typed one instead conflates two different events: a
#: broken scraper, and a user whose bank advice legitimately disagrees with the
#: market. It resolved both as "rejected", which silently swallowed exactly the
#: disagreement the conflict flow exists to surface - the user was never asked,
#: and "Use fetched" had nothing to apply.
_UNTRUSTED_BASELINES = {"seed", "manual"}

#: How much authority a rate's origin carries, high to low.
#:
#: `manual` tops it because a human took that number off a document. `fbil` is
#: India's official benchmark; `frankfurter` is a market aggregate with no
#: statutory standing; `seed` is a placeholder.
#:
#: This exists because FBIL publishes with a lag of several days. The general
#: aggregate is therefore asked for dates FBIL has not covered yet - and without
#: a rank, a later poll that happened to include an already-FBIL-covered date
#: would overwrite the official rate with a market one. Nothing would look
#: wrong; the badge would just quietly change, and a figure someone cited as the
#: official rate would no longer be it.
_SOURCE_AUTHORITY = {"manual": 3, "fbil": 2, "frankfurter": 1, "seed": 0}


def _authority(source: Optional[str]) -> int:
    return _SOURCE_AUTHORITY.get(source or "", 1)


def _latest_stored(db: Session, code: str) -> Optional[Decimal]:
    """The newest *fetched* rate for a code, or None if there has never been one.

    Source-to-source by design; see `_UNTRUSTED_BASELINES`.
    """
    row = db.execute(
        select(CurrencyRate.inr_per_unit, CurrencyRate.source)
        .where(CurrencyRate.code == code,
               CurrencyRate.source.notin_(tuple(_UNTRUSTED_BASELINES)))
        .order_by(CurrencyRate.as_of.desc())
        .limit(1)
    ).first()
    return Decimal(row[0]) if row is not None else None


def _active_codes(db: Session) -> List[str]:
    rows = db.execute(
        select(Currency.code).where(Currency.is_active.is_(True))
    ).scalars().all()
    return [c for c in rows if c != BASE_CURRENCY]


def store_quotes(
    db: Session,
    quotes: Sequence[RateQuote],
    report: RefreshReport,
    *,
    max_step: Optional[Decimal] = None,
    overwrite_manual: Optional[Sequence[str]] = None,
) -> None:
    """Vet and upsert. One row per (code, as_of); a repeat date corrects it.

    A manually entered rate is never overwritten *silently*. Somebody typed that
    number off a bank advice, and a scraper's opinion does not quietly outrank
    it. Instead the difference is recorded in `report.conflicts` so the caller
    can put the choice in front of whoever asked for the refresh.

    `overwrite_manual` is that person's answer coming back: the codes they
    chose to replace. Passing it means the fetched value wins for those codes
    and only those. Note what this does NOT do - it does not let a client supply
    a rate. The number still comes from the source, so a row tagged `rbi` was
    genuinely published by RBI no matter which button was clicked.
    """
    overwrite = {c.upper() for c in (overwrite_manual or ())}
    step = max_step if max_step is not None else Decimal(str(settings.FX_MAX_STEP_CHANGE))
    known = {c for c in db.execute(select(Currency.code)).scalars().all()}
    previous: Dict[str, Optional[Decimal]] = {}

    for quote in quotes:
        if quote.code not in known:
            report.rejected.append(f"{quote.code}: not a configured currency")
            continue

        if quote.code not in previous:
            previous[quote.code] = _latest_stored(db, quote.code)

        reason = validate(quote, previous[quote.code], max_step=step)
        if reason:
            msg = f"{quote.code} {quote.as_of} from {quote.source}: {reason}"
            report.rejected.append(msg)
            logger.warning("[FX] rejected %s", msg)
            continue

        existing = db.execute(
            select(CurrencyRate).where(
                CurrencyRate.code == quote.code,
                CurrencyRate.as_of == quote.as_of,
            )
        ).scalar_one_or_none()

        if existing is None:
            db.add(CurrencyRate(
                code=quote.code, as_of=quote.as_of,
                inr_per_unit=quote.inr_per_unit,
                source=quote.source, note=quote.note,
            ))
            report.updated.append(f"{quote.code} {quote.as_of} = {quote.inr_per_unit}")
            previous[quote.code] = quote.inr_per_unit
        elif existing.source == "manual":
            current = Decimal(existing.inr_per_unit)
            if quote.code in overwrite:
                existing.inr_per_unit = quote.inr_per_unit
                existing.source = quote.source
                existing.note = quote.note
                report.updated.append(
                    f"{quote.code} {quote.as_of} = {quote.inr_per_unit} "
                    f"(replaced your {current})")
                previous[quote.code] = quote.inr_per_unit
            elif current != quote.inr_per_unit:
                drift = ((quote.inr_per_unit - current) / current * 100) if current else Decimal(0)
                report.conflicts.append({
                    "code": quote.code,
                    "as_of": quote.as_of.isoformat(),
                    "manual_rate": float(current),
                    "fetched_rate": float(quote.inr_per_unit),
                    "fetched_source": quote.source,
                    "difference_pct": round(float(drift), 2),
                })
                report.unchanged.append(
                    f"{quote.code} {quote.as_of}: kept your {current}, "
                    f"{quote.source} says {quote.inr_per_unit}")
            else:
                report.unchanged.append(
                    f"{quote.code} {quote.as_of}: your rate already matches {quote.source}")
        elif _authority(quote.source) < _authority(existing.source):
            # A market rate must not displace an official one for the same day.
            report.unchanged.append(
                f"{quote.code} {quote.as_of}: kept the {existing.source} rate "
                f"rather than downgrade it to {quote.source}")
        elif Decimal(existing.inr_per_unit) != quote.inr_per_unit:
            existing.inr_per_unit = quote.inr_per_unit
            existing.source = quote.source
            existing.note = quote.note
            report.updated.append(f"{quote.code} {quote.as_of} = {quote.inr_per_unit}")
            previous[quote.code] = quote.inr_per_unit
        else:
            report.unchanged.append(f"{quote.code} {quote.as_of}")


def refresh_now(
    db: Session,
    codes: Optional[Sequence[str]] = None,
    overwrite_manual: Optional[Sequence[str]] = None,
) -> RefreshReport:
    """Fetch the newest rate for each active currency and store what passes.

    Never raises. A refresh that cannot reach RBI must not take down the page
    that shows a transaction list - the stored rates are still perfectly usable,
    they are just not newer.
    """
    report = RefreshReport(started_at=datetime.now(timezone.utc))
    wanted = list(codes) if codes else _active_codes(db)
    outstanding = {c.upper() for c in wanted}

    for source in build_sources():
        if not outstanding:
            break
        askable = [c for c in outstanding if c in source.supported]
        if not askable:
            continue

        resume_at = _in_cooldown(source.name)
        if resume_at:
            report.errors.append(
                f"{source.name}: skipped, it refused "
                f"{_FAILURES.get(source.name, 0)} times in a row "
                f"(will try again after {resume_at:%Y-%m-%d %H:%M} UTC)")
            continue

        try:
            quotes = source.fetch_latest(askable)
        except RateSourceError as exc:
            _note_failure(source.name, getattr(exc, "retryable", False))
            report.errors.append(f"{source.name}: {exc}")
            logger.warning("[FX] %s unavailable: %s", source.name, exc)
            continue
        except Exception as exc:                      # never kill the caller
            _note_failure(source.name, False)
            report.errors.append(f"{source.name}: unexpected {type(exc).__name__}: {exc}")
            logger.exception("[FX] %s raised unexpectedly", source.name)
            continue

        _note_success(source.name)
        if quotes:
            store_quotes(db, quotes, report, overwrite_manual=overwrite_manual)
            outstanding -= {q.code for q in quotes}

    if outstanding:
        report.errors.append(
            "no source returned a rate for: " + ", ".join(sorted(outstanding)))

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        report.errors.append(f"database: {exc}")
        logger.exception("[FX] failed to commit refreshed rates")

    report.finished_at = datetime.now(timezone.utc)
    _record(report)
    logger.info("[FX] refresh: %d updated, %d unchanged, %d rejected, %d errors, "
                "%d awaiting a decision",
                len(report.updated), len(report.unchanged),
                len(report.rejected), len(report.errors), len(report.conflicts))
    return report


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def refresh_loop(stop_event) -> None:
    """Poll on the configured interval until asked to stop.

    The fetches are blocking HTTP, so they run on a worker thread. Doing them
    inline would stall the event loop for the duration of an RBI round trip -
    two requests against a government ASP.NET page - and every other request
    served by this process would wait behind it.
    """
    import asyncio

    from app.database.session import SessionLocal

    interval = settings.FX_REFRESH_MINUTES * 60

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=settings.FX_REFRESH_STARTUP_DELAY)
        return                                        # asked to stop during the delay
    except asyncio.TimeoutError:
        pass

    while not stop_event.is_set():
        def _run() -> None:
            db = SessionLocal()
            try:
                refresh_now(db)
            finally:
                db.close()

        try:
            await asyncio.to_thread(_run)
        except Exception:                             # pragma: no cover
            logger.exception("[FX] refresh loop iteration failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            continue
