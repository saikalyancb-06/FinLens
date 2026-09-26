"""The provider-agnostic statement discovery engine.

This is the piece the whole design exists to make possible: it takes an
``EmailProvider`` and knows nothing else about where the mail came from.

    Gmail ┐
Microsoft ├──►  EmailProvider  ──►  [ this module ]  ──►  documents on disk
     IMAP ┘                                                     │
                                                                ▼
                                             app.statements.ingest → existing
                                             parsing / normalisation /
                                             categorisation pipeline

The scan is staged so that a large mailbox costs a bounded amount of work:

    search (cheap metadata)
      → score metadata, keep candidates
        → fetch full message for candidates only
          → score content, walk the MIME tree
            → download only attachments that could be documents
              → classify from document content
                → persist, deduplicate, optionally ingest

Nothing at any stage keys off a sender allow-list or a hardcoded institution.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
# Import the session module before any individual model module: it owns the
# mapper-registration loop that must run first. See app/mailbox/registry.py
# for the full explanation of why the order matters.
import app.database.session  # noqa: F401  (imported for its side effect)
from app.email.models import ConnectedAccount, EmailAttachment, MailboxScan
from app.mailbox.base import EmailProvider
from app.mailbox.criteria import DEFAULT_STATEMENT_KEYWORDS, SearchCriteria
from app.mailbox.errors import MailboxError, RateLimited
from app.mailbox.types import MessageSummary
from app.statements import signals
from app.statements.body_extractor import (
    extract_body_statement,
    mentions_external_statement_link,
)
from app.statements.classifier import DocumentClassification, classify_content
from app.statements.document_text import read_document

logger = logging.getLogger(__name__)

#: Second search pass: messages that *carry a document* but whose subject never
#: says "statement". "Your July document", "Monthly report", "Account update" —
#: these are how a good share of statements actually arrive.
ATTACHMENT_PASS_KEYWORDS = [
    "account", "transaction", "bank", "card", "loan", "portfolio", "folio",
    "balance", "monthly", "quarterly", "document", "report", "summary",
    "statement", "e-statement", "estatement", "passbook", "wallet",
]


@dataclass
class ScanLimits:
    """Bounds that keep a scan finite on a mailbox of any size."""

    #: messages examined per search pass
    max_messages_per_pass: int = 150
    #: messages opened in full
    max_candidates: int = 60
    #: attachments downloaded
    max_downloads: int = 40
    #: bytes downloaded in total
    max_download_bytes: int = 120 * 1024 * 1024
    #: bytes for any single attachment
    max_attachment_bytes: int = 30 * 1024 * 1024
    #: how far back to look
    lookback_days: int = 365


@dataclass
class DiscoveredDocument:
    """One document found in a mailbox, before it is written to the database."""

    message_id: str
    attachment_ref: Optional[str]
    filename: str
    mime_type: str
    data: bytes
    source_kind: str
    subject: str = ""
    sender: str = ""
    sender_name: str = ""
    received_at: Optional[_dt.datetime] = None
    discovery_score: float = 0.0
    discovery_reason: str = ""
    classification: Optional[DocumentClassification] = None
    local_path: Optional[str] = None
    file_hash: str = ""

    @property
    def dedupe_key(self) -> str:
        return self.file_hash


@dataclass
class ScanOutcome:
    """Everything a completed scan wants to report."""

    messages_scanned: int = 0
    candidates_found: int = 0
    documents_downloaded: int = 0
    statements_found: int = 0
    duplicates_skipped: int = 0
    failures: int = 0
    external_link_only: int = 0
    notes: List[str] = field(default_factory=list)
    attachment_ids: List[str] = field(default_factory=list)
    error: Optional[MailboxError] = None


def _storage_dir() -> str:
    path = os.path.join(settings.UPLOAD_DIR, "email")
    os.makedirs(path, exist_ok=True)
    return path


def _safe_filename(name: str, fallback: str = "document") -> str:
    """Strip path separators and control characters from a provider-supplied name.

    The filename comes from a third party. It is used to build a path on disk,
    which makes ``../../etc/passwd`` a live concern rather than a theoretical
    one; only the basename's safe characters survive.
    """
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._ \-()]+", "_", base).strip(" .")
    if not base:
        base = fallback
    return base[:180]


class StatementDiscoveryEngine:
    """Finds financial documents inside any mailbox reachable through a provider."""

    def __init__(self, limits: Optional[ScanLimits] = None):
        self.limits = limits or ScanLimits()

    # ---- stage 1: candidate selection ------------------------------------

    async def collect_candidates(
        self, provider: EmailProvider, *, lookback_days: Optional[int] = None,
    ) -> Tuple[List[Tuple[MessageSummary, signals.SignalScore]], int]:
        """Two cheap search passes, scored on metadata alone."""
        since = _dt.datetime.utcnow() - _dt.timedelta(days=lookback_days or self.limits.lookback_days)
        seen: Dict[str, Tuple[MessageSummary, signals.SignalScore]] = {}
        examined = 0

        passes = (
            SearchCriteria(keywords=list(DEFAULT_STATEMENT_KEYWORDS), require_attachment=False,
                           since=since, limit=self.limits.max_messages_per_pass),
            SearchCriteria(keywords=list(ATTACHMENT_PASS_KEYWORDS), require_attachment=True,
                           since=since, limit=self.limits.max_messages_per_pass),
        )

        for criteria in passes:
            try:
                async for summary in provider.search_messages(criteria):
                    examined += 1
                    if summary.id in seen:
                        continue
                    score = signals.score_message_metadata(
                        subject=summary.subject,
                        sender=summary.sender,
                        snippet=summary.snippet,
                        attachment_filenames=summary.attachment_filenames,
                        has_attachments=summary.has_attachments,
                    )
                    if score.score >= signals.CANDIDATE_THRESHOLD:
                        seen[summary.id] = (summary, score)
            except RateLimited as exc:
                logger.warning("[Discovery] provider rate-limited during search: %s", exc.detail)
                break
            except MailboxError:
                raise

        ranked = sorted(seen.values(), key=lambda pair: pair[1].score, reverse=True)
        return ranked[: self.limits.max_candidates], examined

    # ---- stage 2: message inspection -------------------------------------

    async def inspect_message(
        self,
        provider: EmailProvider,
        summary: MessageSummary,
        metadata_score: signals.SignalScore,
        budget: Dict[str, int],
    ) -> Tuple[List[DiscoveredDocument], bool]:
        """Fetch one message, decide what to download, and download it.

        Returns the documents found and whether the message only pointed at an
        externally hosted statement.
        """
        detail = await provider.get_message(summary.id)
        content_score = signals.score_message_content(detail.combined_text())
        total = metadata_score.score + content_score.score
        reason = "; ".join(filter(None, [metadata_score.summary, content_score.summary]))

        documents: List[DiscoveredDocument] = []

        for attachment in detail.attachments:
            if budget["downloads"] >= self.limits.max_downloads:
                break
            if budget["bytes"] >= self.limits.max_download_bytes:
                break

            ok, why = signals.attachment_is_candidate(
                attachment.filename, attachment.mime_type, attachment.size
            )
            if not ok:
                logger.debug("[Discovery] skipping attachment: %s", why)
                continue
            if attachment.size and attachment.size > self.limits.max_attachment_bytes:
                logger.info("[Discovery] skipping '%s': %s bytes exceeds the per-file limit",
                            attachment.filename, attachment.size)
                continue

            try:
                payload = await provider.download_attachment(summary.id, attachment)
            except MailboxError as exc:
                logger.info("[Discovery] could not download '%s' from %s: %s",
                            attachment.filename, summary.id, exc.detail)
                continue

            budget["downloads"] += 1
            budget["bytes"] += len(payload.data)

            documents.append(DiscoveredDocument(
                message_id=summary.id,
                attachment_ref=attachment.ref,
                filename=_safe_filename(payload.filename or attachment.filename, "attachment"),
                mime_type=payload.mime_type or attachment.mime_type,
                data=payload.data,
                source_kind=EmailAttachment.SOURCE_ATTACHMENT,
                subject=detail.subject,
                sender=detail.sender,
                sender_name=detail.sender_name,
                received_at=detail.received_at or summary.received_at,
                discovery_score=round(total, 3),
                discovery_reason=reason or why,
            ))

        # A statement in the body itself. Only pursued when the message actually
        # reads like one — otherwise every mail with a three-column table would
        # become a candidate statement.
        external_only = False
        if total >= signals.CONTENT_THRESHOLD:
            body = extract_body_statement(detail)
            if body is not None:
                csv_bytes = body.to_csv_bytes()
                documents.append(DiscoveredDocument(
                    message_id=summary.id,
                    attachment_ref=None,
                    filename=_safe_filename(
                        f"{(detail.subject or 'email statement')[:60]}.csv", "email_statement.csv"),
                    mime_type="text/csv",
                    data=csv_bytes,
                    source_kind=EmailAttachment.SOURCE_BODY,
                    subject=detail.subject,
                    sender=detail.sender,
                    sender_name=detail.sender_name,
                    received_at=detail.received_at or summary.received_at,
                    discovery_score=round(total, 3),
                    discovery_reason=f"{reason}; transaction table found in the {body.source} body",
                ))
            elif not documents and mentions_external_statement_link(detail):
                # The statement lives behind a link. Recorded, never followed.
                external_only = True

        return documents, external_only

    # ---- stage 3: classification ----------------------------------------

    def classify(self, document: DiscoveredDocument, path: str) -> DocumentClassification:
        content = read_document(path, filename=document.filename)
        sender_domain = ""
        address = (document.sender or "")
        if "@" in address:
            sender_domain = address.rsplit("@", 1)[-1].strip("> ").lower()
        return classify_content(
            content,
            filename=document.filename,
            sender_domain=sender_domain,
            sender_name=document.sender_name,
            subject=document.subject,
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def compute_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_existing(
    db: Session, user_id, *, file_hash: str, message_id: str, attachment_ref: Optional[str],
) -> Optional[EmailAttachment]:
    """Locate a previously discovered copy of this document for this user.

    Two independent identities are checked, because the same statement legitimately
    arrives more than once in more than one shape:

    * identical bytes anywhere in the mailbox (forwarded, re-sent, renamed) —
      matched on the SHA-256 of the content;
    * the same attachment of the same message seen on an earlier scan — matched
      on (message id, attachment reference).

    The attachment reference is part of the second key on purpose: matching on
    message id alone, as the previous implementation did, made the *second*
    attachment of a two-statement email look like a duplicate of the first and
    silently discarded it.
    """
    query = db.query(EmailAttachment).filter(EmailAttachment.user_id == user_id)

    by_hash = query.filter(EmailAttachment.file_hash == file_hash).first()
    if by_hash is not None:
        return by_hash

    same_message = query.filter(EmailAttachment.email_message_id == message_id)
    if attachment_ref:
        return same_message.filter(
            EmailAttachment.provider_attachment_id == attachment_ref
        ).first()
    return same_message.filter(
        EmailAttachment.source_kind == EmailAttachment.SOURCE_BODY
    ).first()


def persist_document(
    db: Session,
    document: DiscoveredDocument,
    *,
    user_id,
    connection: ConnectedAccount,
    scan: Optional[MailboxScan] = None,
) -> Tuple[Optional[EmailAttachment], str]:
    """Write a discovered document to disk and record it. Returns (row, status).

    ``status`` is one of ``created``, ``duplicate`` or ``failed``.
    """
    document.file_hash = compute_hash(document.data)

    existing = find_existing(
        db, user_id,
        file_hash=document.file_hash,
        message_id=document.message_id,
        attachment_ref=document.attachment_ref,
    )
    if existing is not None:
        return existing, "duplicate"

    unique_name = f"{user_id}_{uuid.uuid4().hex[:8]}_{document.filename}"
    path = os.path.join(_storage_dir(), unique_name)
    try:
        with open(path, "wb") as handle:
            handle.write(document.data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        logger.exception("[Discovery] could not write '%s' to disk", document.filename)
        return None, "failed"

    document.local_path = path
    engine = StatementDiscoveryEngine()
    verdict = engine.classify(document, path)
    document.classification = verdict

    record = EmailAttachment(
        user_id=user_id,
        connected_account_id=connection.id,
        scan_id=scan.id if scan is not None else None,
        email_message_id=document.message_id,
        provider_attachment_id=document.attachment_ref,
        bank_name=verdict.institution_name,
        subject=(document.subject or "")[:500],
        sender=(document.sender or "")[:255],
        received_at=document.received_at,
        filename=document.filename[:255],
        file_size_bytes=len(document.data),
        mime_type=(document.mime_type or "")[:100],
        file_hash=document.file_hash,
        dedupe_key=document.file_hash,
        local_path=path,
        is_duplicate=False,
        import_status="PENDING" if verdict.is_transactional else "SKIPPED",
        classification=verdict.legacy_classification,
        classification_reason=verdict.reason,
        document_type=verdict.document_type,
        institution_name=verdict.institution_name,
        institution_confidence=verdict.institution.confidence,
        statement_period_start=verdict.period_start,
        statement_period_end=verdict.period_end,
        currency=verdict.currency,
        source_kind=document.source_kind,
        discovery_score=document.discovery_score,
        detected_account_number=verdict.account_identifier,
    )
    db.add(record)
    try:
        db.commit()
        db.refresh(record)
    except Exception:
        db.rollback()
        # A concurrent scan of the same mailbox can insert the same document
        # first. That is the idempotency guarantee working, not an error.
        logger.info("[Discovery] '%s' was already recorded by a concurrent scan",
                    document.filename)
        again = find_existing(db, user_id, file_hash=document.file_hash,
                              message_id=document.message_id,
                              attachment_ref=document.attachment_ref)
        return (again, "duplicate") if again is not None else (None, "failed")

    return record, "created"


# ---------------------------------------------------------------------------
# Whole-scan orchestration
# ---------------------------------------------------------------------------

async def run_discovery(
    db: Session,
    provider: EmailProvider,
    connection: ConnectedAccount,
    *,
    user_id,
    scan: Optional[MailboxScan] = None,
    limits: Optional[ScanLimits] = None,
    progress=None,
) -> ScanOutcome:
    """Scan one mailbox end to end and record everything found.

    ``progress`` is called with ``(stage, percent, counters)`` so a caller can
    surface live progress; it is optional and any exception it raises is
    swallowed, because a UI concern must never fail a scan.
    """
    engine = StatementDiscoveryEngine(limits)
    outcome = ScanOutcome()

    def report(stage: str, percent: int) -> None:
        if progress is None:
            return
        try:
            progress(stage, percent, outcome)
        except Exception:
            logger.debug("[Discovery] progress callback failed", exc_info=True)

    report("Searching the mailbox", 5)
    candidates, examined = await engine.collect_candidates(provider)
    outcome.messages_scanned = examined
    outcome.candidates_found = len(candidates)
    logger.info("[Discovery] %s message(s) examined, %s candidate(s) for %s",
                examined, len(candidates), connection.email_address)
    report("Reading candidate emails", 20)

    budget = {"downloads": 0, "bytes": 0}
    total = max(1, len(candidates))

    for index, (summary, score) in enumerate(candidates, start=1):
        try:
            documents, external_only = await engine.inspect_message(provider, summary, score, budget)
        except RateLimited as exc:
            outcome.notes.append(
                f"The provider began rate-limiting after {index - 1} of {len(candidates)} "
                "candidate emails; the rest were not read. Run the scan again shortly."
            )
            logger.warning("[Discovery] rate limited: %s", exc.detail)
            break
        except MailboxError as exc:
            outcome.failures += 1
            logger.info("[Discovery] message %s failed: %s", summary.id, exc.detail)
            continue
        except Exception:
            outcome.failures += 1
            logger.exception("[Discovery] unexpected failure on message %s", summary.id)
            continue

        if external_only:
            outcome.external_link_only += 1

        for document in documents:
            record, status = persist_document(
                db, document, user_id=user_id, connection=connection, scan=scan,
            )
            if status == "duplicate":
                outcome.duplicates_skipped += 1
                continue
            if status == "failed" or record is None:
                outcome.failures += 1
                continue
            outcome.documents_downloaded += 1
            outcome.attachment_ids.append(str(record.id))
            if document.classification and document.classification.is_transactional:
                outcome.statements_found += 1

        report("Reading candidate emails", 20 + int(60 * index / total))

    if outcome.external_link_only:
        outcome.notes.append(
            f"{outcome.external_link_only} email(s) link to a statement hosted on the "
            "provider's own website rather than attaching it. Those are not downloaded — "
            "sign in to that institution and upload the file, or use the direct bank "
            "connection if one is available."
        )

    report("Finishing", 90)
    return outcome


def run_discovery_sync(*args, **kwargs) -> ScanOutcome:
    """Run :func:`run_discovery` from synchronous code (a worker thread)."""
    return asyncio.run(run_discovery(*args, **kwargs))
