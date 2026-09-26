"""Attachment helpers, retained as the compatibility face of the new engine.

The logic that used to live here — extension allow-lists, keyword heuristics over
PDF text, duplicate lookup — now lives in ``app/statements`` and ``app/mailbox``
so that every provider and every document format goes through one implementation.
This module keeps its original public surface for callers that still import it,
and delegates.

What changed underneath, and why:

* ``is_valid_attachment_extension`` no longer *decides* anything. Type is settled
  by content (``app.statements.document_text.sniff_kind``); the extension is a
  hint. A statement attached as ``application/pdf`` with no extension used to be
  invisible.
* ``inspect_attachment_content`` returns the same four-tuple, but the verdict now
  comes from ``app.statements.classifier``, which asks whether the document
  contains a transaction ledger rather than whether it contains the right words.
* ``check_duplicate`` matches on (message id **+ attachment reference**) or on
  content hash. Matching on message id alone, as this used to, treated the second
  statement in a two-attachment email as a duplicate of the first and dropped it.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.email.models import EmailAttachment
from app.email.utils import compute_file_sha256
from app.statements.classifier import classify_file
from app.statements.signals import (
    NON_STATEMENT_EXTENSIONS,
    STATEMENT_EXTENSIONS,
    attachment_is_candidate,
)

logger = logging.getLogger(__name__)

#: Retained for callers that import these names.
ALLOWED_EXTENSIONS = {".pdf", ".csv", ".xlsx", ".xls"}
DISALLOWED_EXTENSIONS = set(NON_STATEMENT_EXTENSIONS)


class AttachmentHandler:
    """Validation, deduplication and storage for downloaded documents."""

    def __init__(self, email_upload_dir: Optional[str] = None):
        self.email_upload_dir = email_upload_dir or os.path.join(settings.UPLOAD_DIR, "email")
        os.makedirs(self.email_upload_dir, exist_ok=True)

    def is_valid_attachment_extension(self, filename: str) -> bool:
        """True when the filename suggests a document worth inspecting.

        A hint, not a gate — the scan engine calls ``attachment_is_candidate``,
        which also considers the MIME type and lets unknown types through to
        content sniffing.
        """
        if not filename:
            return False
        return os.path.splitext(filename.lower())[1] in ALLOWED_EXTENSIONS

    def is_candidate(self, filename: str, mime_type: str = "", size: int = 0) -> Tuple[bool, str]:
        """The real test: filename *or* MIME type may qualify an attachment."""
        return attachment_is_candidate(filename, mime_type, size)

    def check_duplicate(
        self,
        db: Session,
        user_id,
        message_id: str,
        file_hash: str,
        attachment_ref: Optional[str] = None,
    ) -> Tuple[bool, Optional[EmailAttachment]]:
        """Has this user already got this document?"""
        from app.statements.discovery import find_existing

        user_uuid = uuid.UUID(str(user_id)) if not isinstance(user_id, uuid.UUID) else user_id
        existing = find_existing(db, user_uuid, file_hash=file_hash,
                                 message_id=message_id, attachment_ref=attachment_ref)
        if existing is not None:
            logger.info("[Duplicate] '%s' (hash %s…) is already recorded",
                        existing.filename, (file_hash or "")[:8])
            return True, existing
        return False, None

    def save_attachment_file(self, user_id, filename: str, file_bytes: bytes) -> Tuple[str, str]:
        """Write bytes to the per-user email upload directory. Returns (path, sha256)."""
        from app.statements.discovery import _safe_filename

        if not file_bytes:
            raise ValueError(f"Attachment payload for '{filename}' is 0 bytes.")

        file_hash = compute_file_sha256(file_bytes)
        safe = _safe_filename(filename, "attachment")
        path = os.path.join(self.email_upload_dir, f"{user_id}_{uuid.uuid4().hex[:8]}_{safe}")
        with open(path, "wb") as handle:
            handle.write(file_bytes)
            handle.flush()
            os.fsync(handle.fileno())

        written = os.path.getsize(path) if os.path.exists(path) else 0
        if written != len(file_bytes):
            raise ValueError(
                f"Failed writing '{filename}' to disk ({written} of {len(file_bytes)} bytes)."
            )
        return path, file_hash

    def inspect_attachment_content(
        self,
        file_path: str,
        filename: str,
        pdf_password: Optional[str] = None,
    ) -> Tuple[bool, str, str, Optional[str]]:
        """Classify a document on disk.

        Returns ``(is_statement, classification, reason, detected_account_number)``
        in the original shape. ``classification`` uses the legacy vocabulary
        (``BANK_STATEMENT_CONFIRMED`` / ``NOT_BANK_STATEMENT`` /
        ``PDF_PASSWORD_REQUIRED`` / ``PASSWORD_INVALID`` /
        ``UNSUPPORTED_ATTACHMENT``); the richer verdict is available directly
        from ``app.statements.classifier.classify_file``.
        """
        verdict = classify_file(file_path, filename, password=pdf_password)
        return (
            verdict.is_transactional,
            verdict.legacy_classification,
            verdict.reason,
            verdict.account_identifier,
        )


attachment_handler = AttachmentHandler()

__all__ = [
    "AttachmentHandler",
    "attachment_handler",
    "ALLOWED_EXTENSIONS",
    "DISALLOWED_EXTENSIONS",
    "STATEMENT_EXTENSIONS",
]
