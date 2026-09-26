"""Runs mailbox scans, in the foreground or in the background.

Requirement: a scan of a large mailbox must not be one long synchronous HTTP
request. The application has no Celery, no RQ worker and no message broker, and
introducing one for a job that runs a handful of times per user per day would be
more infrastructure than the work needs. So: a bounded thread pool, with all
progress written to the ``mailbox_scans`` table.

That choice has a consequence worth stating plainly — a scan in flight when the
process restarts is lost, and its row is left ``RUNNING``. ``reap_stale_scans``
below marks those failed on the next startup so the UI never shows a spinner
that will never finish. If this ever needs to survive restarts or spread across
machines, the queue is the piece to replace; nothing else here changes.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from sqlalchemy.orm import Session

# Import the session module before any individual model module: it owns the
# mapper-registration loop that must run first. See app/mailbox/registry.py
# for the full explanation of why the order matters.
import app.database.session  # noqa: F401  (imported for its side effect)
from app.email.models import ConnectedAccount, EmailAttachment, MailboxScan
from app.mailbox.errors import MailboxError
from app.mailbox.registry import mark_connection_failed, mark_connection_healthy, open_provider
from app.statements.classifier import TRANSACTIONAL_TYPES
from app.statements.discovery import ScanLimits, run_discovery
from app.statements.ingest import ingest_attachment

logger = logging.getLogger(__name__)

#: Small on purpose. Each worker holds a database session and a provider
#: connection; letting a hundred scans run at once would exhaust the pool long
#: before it made anything faster.
_MAX_CONCURRENT_SCANS = int(os.getenv("MAILBOX_SCAN_WORKERS", "2"))
_executor = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_SCANS,
                               thread_name_prefix="mailbox-scan")
_inflight: set = set()
_inflight_lock = threading.Lock()


def create_scan(
    db: Session,
    *,
    user_id,
    connection: ConnectedAccount,
    auto_import: bool = False,
) -> MailboxScan:
    """Record a scan in the queued state. Ownership is the caller's to verify."""
    scan = MailboxScan(
        user_id=user_id,
        connection_id=connection.id,
        provider=connection.provider,
        email_address=connection.email_address,
        status=MailboxScan.STATUS_QUEUED,
        stage="Queued",
        auto_import=auto_import,
    )
    db.add(scan)
    db.commit()
    db.refresh(scan)
    return scan


def start_scan_in_background(scan_id) -> None:
    """Hand a queued scan to the worker pool."""
    key = str(scan_id)
    with _inflight_lock:
        if key in _inflight:
            logger.info("[Scan] %s is already running; ignoring duplicate start", key)
            return
        _inflight.add(key)
    _executor.submit(_run_and_release, scan_id)


def _run_and_release(scan_id) -> None:
    try:
        execute_scan(scan_id)
    finally:
        with _inflight_lock:
            _inflight.discard(str(scan_id))


def execute_scan(scan_id) -> Optional[MailboxScan]:
    """Run one scan to completion, in whichever thread calls this.

    Opens its own session: a worker thread must never share the request-scoped
    session, and a foreground caller gets the same isolation for free.
    """
    from app.database.session import SessionLocal

    db: Session = SessionLocal()
    try:
        scan = db.query(MailboxScan).filter(MailboxScan.id == _as_uuid(scan_id)).first()
        if scan is None:
            logger.warning("[Scan] %s no longer exists", scan_id)
            return None

        connection = db.query(ConnectedAccount).filter(
            ConnectedAccount.id == scan.connection_id,
            # Belt and braces: the scan row carries the owning user, and the
            # connection is re-checked against it here so a corrupted or
            # hand-edited row cannot make a worker read someone else's mailbox.
            ConnectedAccount.user_id == scan.user_id,
        ).first()
        if connection is None:
            _fail(db, scan, "connection_missing",
                  "The mailbox connection for this scan no longer exists.")
            return scan

        scan.status = MailboxScan.STATUS_RUNNING
        scan.stage = "Connecting to the mailbox"
        scan.started_at = _dt.datetime.utcnow()
        scan.progress_pct = 2
        db.commit()

        def progress(stage: str, percent: int, outcome) -> None:
            scan.stage = stage
            scan.progress_pct = max(scan.progress_pct, min(95, percent))
            scan.messages_scanned = outcome.messages_scanned
            scan.candidates_found = outcome.candidates_found
            scan.documents_downloaded = outcome.documents_downloaded
            scan.statements_found = outcome.statements_found
            scan.duplicates_skipped = outcome.duplicates_skipped
            scan.failures = outcome.failures
            db.commit()

        try:
            outcome = asyncio.run(_scan_once(db, connection, scan, progress))
        except MailboxError as exc:
            mark_connection_failed(db, connection, exc)
            _fail(db, scan, exc.reason, exc.detail)
            return scan
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("[Scan] %s failed unexpectedly", scan.id)
            _fail(db, scan, "unexpected_error", str(exc))
            return scan

        mark_connection_healthy(db, connection)
        connection.last_scan_at = _dt.datetime.utcnow()

        scan.messages_scanned = outcome.messages_scanned
        scan.candidates_found = outcome.candidates_found
        scan.documents_downloaded = outcome.documents_downloaded
        scan.statements_found = outcome.statements_found
        scan.duplicates_skipped = outcome.duplicates_skipped
        scan.failures = outcome.failures
        scan.error_detail = "\n".join(outcome.notes) if outcome.notes else None

        if scan.auto_import:
            scan.stage = "Extracting transactions"
            scan.progress_pct = 95
            db.commit()
            scan.transactions_extracted = auto_import_statements(
                db, user_id=scan.user_id, attachment_ids=outcome.attachment_ids,
                provider=connection.provider,
            )

        scan.status = MailboxScan.STATUS_COMPLETED
        scan.stage = "Finished"
        scan.progress_pct = 100
        scan.finished_at = _dt.datetime.utcnow()
        db.commit()
        return scan
    finally:
        db.close()


async def _scan_once(db: Session, connection: ConnectedAccount, scan: MailboxScan, progress):
    provider = await open_provider(db, connection)
    try:
        outcome = await run_discovery(
            db, provider, connection,
            user_id=scan.user_id,
            scan=scan,
            limits=ScanLimits(),
            progress=progress,
        )
    finally:
        try:
            await provider.disconnect()
        except Exception:
            logger.debug("[Scan] provider disconnect failed", exc_info=True)

    map_accounts(db, user_id=scan.user_id, attachment_ids=outcome.attachment_ids)
    return outcome


def _fail(db: Session, scan: MailboxScan, reason: str, detail: str) -> None:
    scan.status = MailboxScan.STATUS_FAILED
    scan.stage = "Failed"
    scan.error_reason = reason
    scan.error_detail = (detail or "")[:2000]
    scan.finished_at = _dt.datetime.utcnow()
    scan.progress_pct = 100
    db.commit()


def _as_uuid(value):
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# ---------------------------------------------------------------------------
# Post-scan steps
# ---------------------------------------------------------------------------

def map_accounts(db: Session, *, user_id, attachment_ids) -> int:
    """Attach each discovered statement to one of the user's bank-master accounts.

    Only an unambiguous match is applied. Two accounts whose masked numbers both
    end in the detected digits is not a match, it is a question for the user —
    guessing there would file transactions against the wrong account, which is
    considerably worse than asking.
    """
    from app.models.account import Account

    if not attachment_ids:
        return 0

    accounts = db.query(Account).filter(
        Account.user_id == user_id, Account.deleted_at.is_(None)
    ).all()

    mapped = 0
    rows = db.query(EmailAttachment).filter(
        EmailAttachment.user_id == user_id,
        EmailAttachment.id.in_([_as_uuid(a) for a in attachment_ids]),
    ).all()

    for row in rows:
        if row.account_id is not None:
            continue
        if row.document_type not in TRANSACTIONAL_TYPES:
            continue

        detected = "".join(ch for ch in (row.detected_account_number or "") if ch.isdigit())
        matches = []
        if detected and accounts:
            for account in accounts:
                digits = "".join(ch for ch in (account.account_number_masked or "") if ch.isdigit())
                if not digits:
                    continue
                if detected.endswith(digits) or digits.endswith(detected) or digits in detected:
                    matches.append(account)

        if len(matches) == 1:
            row.account_id = matches[0].id
            row.classification = "BANK_STATEMENT_CONFIRMED"
            row.classification_reason = (
                f"{row.classification_reason or ''} Matched your registered account "
                f"{matches[0].account_number_masked}."
            ).strip()
            mapped += 1
        elif len(accounts) == 1 and not detected:
            # A single registered account and no identifier in the document: the
            # only account it could belong to.
            row.account_id = accounts[0].id
            row.classification = "BANK_STATEMENT_CONFIRMED"
            row.classification_reason = (
                f"{row.classification_reason or ''} No account number was printed on the "
                f"document; assigned to your only registered account "
                f"{accounts[0].account_number_masked}."
            ).strip()
            mapped += 1
        else:
            row.classification = "NEEDS_ACCOUNT_MAPPING"
            if not accounts:
                row.classification_reason = (
                    "Add a bank account under Bank Master, then choose it for this statement."
                )
            else:
                row.classification_reason = (
                    f"{row.classification_reason or ''} Choose which of your accounts this "
                    "statement belongs to."
                ).strip()

    db.commit()
    return mapped


def auto_import_statements(db: Session, *, user_id, attachment_ids, provider: str) -> int:
    """Feed every unambiguously-mapped statement into the ingestion pipeline.

    Statements still awaiting an account choice are left alone; importing them
    against a guessed account is the one failure mode worth avoiding here.
    """
    if not attachment_ids:
        return 0

    rows = db.query(EmailAttachment).filter(
        EmailAttachment.user_id == user_id,
        EmailAttachment.id.in_([_as_uuid(a) for a in attachment_ids]),
        EmailAttachment.import_status == "PENDING",
    ).all()

    total = 0
    for row in rows:
        if row.account_id is None:
            continue
        if row.document_type not in TRANSACTIONAL_TYPES:
            continue
        try:
            result = ingest_attachment(
                db, row, user_id=user_id, account_id=row.account_id, provider=provider,
            )
            total += result.transactions_created if result.ok else 0
        except Exception:
            logger.exception("[Scan] auto-import failed for attachment %s", row.id)
            db.rollback()
    db.commit()
    return total


def reap_stale_scans(db: Session, *, older_than_minutes: int = 60) -> int:
    """Fail scans left ``RUNNING`` by a process that went away.

    Called at startup. Without it a killed worker leaves a row that the UI polls
    forever, which reads to the user as "the scan is stuck" with no way out.
    """
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(minutes=older_than_minutes)
    stale = db.query(MailboxScan).filter(
        MailboxScan.status.in_([MailboxScan.STATUS_RUNNING, MailboxScan.STATUS_QUEUED]),
        MailboxScan.created_at < cutoff,
    ).all()
    for scan in stale:
        scan.status = MailboxScan.STATUS_FAILED
        scan.stage = "Interrupted"
        scan.error_reason = "interrupted"
        scan.error_detail = ("The scan was interrupted, most likely because the server "
                             "restarted. Start it again.")
        scan.finished_at = _dt.datetime.utcnow()
    if stale:
        db.commit()
    return len(stale)
