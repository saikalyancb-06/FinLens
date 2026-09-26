"""Compatibility shim for the original Gmail-only scan engine.

Scanning is now provider-agnostic and lives in ``app/statements``:
``discovery.py`` finds documents through any ``EmailProvider``, and
``scan_runner.py`` drives a scan and records its progress. This module forwards
``scanInbox`` to that machinery so existing callers keep working.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class EmailScannerEngine:
    """Kept for its ``scanInbox`` entry point; the work happens elsewhere."""

    async def scanInbox(self, db: Session, user_id: str) -> Dict[str, Any]:
        """Scan every active mailbox belonging to ``user_id``.

        Returns the same summary shape the previous implementation did.
        """
        import uuid as _uuid

        from app.email.models import ConnectedAccount, MailboxScan
        from app.statements.scan_runner import create_scan, execute_scan

        user_uuid = user_id if isinstance(user_id, _uuid.UUID) else _uuid.UUID(str(user_id))
        connections = db.query(ConnectedAccount).filter(
            ConnectedAccount.user_id == user_uuid,
            ConnectedAccount.is_active.is_(True),
        ).all()

        if not connections:
            return {
                "status": "error",
                "message": "No active connected email account found. Please connect a mailbox first.",
                "reason": "no_connected_account",
                "statements": [],
                "total_found": 0,
            }

        found = new = duplicates = failures = 0
        for connection in connections:
            scan = create_scan(db, user_id=user_uuid, connection=connection)
            execute_scan(scan.id)
            db.expire_all()
            refreshed = db.query(MailboxScan).filter(MailboxScan.id == scan.id).first()
            if refreshed is None:
                continue
            new += refreshed.documents_downloaded
            duplicates += refreshed.duplicates_skipped
            failures += refreshed.failures
        found = new + duplicates

        from app.email.models import EmailAttachment

        rows = db.query(EmailAttachment).filter(
            EmailAttachment.user_id == user_uuid
        ).order_by(EmailAttachment.created_at.desc()).all()

        return {
            "status": "success",
            "email_address": connections[0].email_address,
            "last_scan": connections[0].last_scan_at.strftime("%Y-%m-%d %H:%M:%S")
            if connections[0].last_scan_at else None,
            "total_found": found,
            "new_found": new,
            "duplicates_found": duplicates,
            "failures": failures,
            "statements": [
                {
                    "id": str(row.id),
                    "bank": row.institution_name or row.bank_name,
                    "filename": row.filename,
                    "subject": row.subject,
                    "sender": row.sender,
                    "import_status": row.import_status,
                    "classification": row.classification,
                }
                for row in rows
            ],
            "reason": "ok" if found else "no_statements_found",
        }


email_scanner_engine = EmailScannerEngine()

__all__ = ["EmailScannerEngine", "email_scanner_engine"]
