"""
In-memory RPA session state.

Credentials, OTPs, and PDF passwords live here only for the duration of the
job. Nothing in this module is persisted to the database or logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from time import time
from typing import Any, Optional


@dataclass
class RpaSessionContext:
    credentials: dict[str, Any] = field(default_factory=dict)
    otp: Optional[str] = None
    otp_attempts: int = 0
    pdf_password: Optional[str] = None
    created_at: float = field(default_factory=time)


_LOCK = RLock()
_SESSIONS: dict[str, RpaSessionContext] = {}


def set_credentials(job_id: str, credentials: dict[str, Any]) -> None:
    with _LOCK:
        ctx = _SESSIONS.setdefault(str(job_id), RpaSessionContext())
        ctx.credentials = dict(credentials)


def get_credentials(job_id: str) -> dict[str, Any]:
    with _LOCK:
        ctx = _SESSIONS.get(str(job_id))
        return dict(ctx.credentials) if ctx else {}


def set_otp(job_id: str, otp: str) -> int:
    with _LOCK:
        ctx = _SESSIONS.setdefault(str(job_id), RpaSessionContext())
        ctx.otp = otp
        ctx.otp_attempts += 1
        return ctx.otp_attempts


def pop_otp(job_id: str) -> Optional[str]:
    with _LOCK:
        ctx = _SESSIONS.get(str(job_id))
        if not ctx:
            return None
        otp = ctx.otp
        ctx.otp = None
        return otp


def get_otp_attempts(job_id: str) -> int:
    with _LOCK:
        ctx = _SESSIONS.get(str(job_id))
        return ctx.otp_attempts if ctx else 0


def set_pdf_password(job_id: str, pdf_password: str) -> None:
    with _LOCK:
        ctx = _SESSIONS.setdefault(str(job_id), RpaSessionContext())
        ctx.pdf_password = pdf_password


def pop_pdf_password(job_id: str) -> Optional[str]:
    with _LOCK:
        ctx = _SESSIONS.get(str(job_id))
        if not ctx:
            return None
        pdf_password = ctx.pdf_password
        ctx.pdf_password = None
        return pdf_password


def clear_session(job_id: str) -> None:
    with _LOCK:
        _SESSIONS.pop(str(job_id), None)


def has_session(job_id: str) -> bool:
    with _LOCK:
        return str(job_id) in _SESSIONS
