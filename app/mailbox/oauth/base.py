"""Shared shape for the provider OAuth 2.0 clients.

Only the backend ever holds a client secret or a token. The front-end receives
an authorization URL and nothing else; the callback lands on the server, the
exchange happens server-side, and the resulting refresh token is encrypted
before it touches the database.
"""
from __future__ import annotations

import abc
import logging
from typing import Any, Dict, List, Optional

from app.mailbox.types import MailboxAccountInfo

logger = logging.getLogger(__name__)


class OAuthClient(abc.ABC):
    """Authorization-code flow against one identity provider."""

    #: registry key of the mailbox provider this client authenticates
    provider_key: str = ""
    #: scopes requested — read-only mail plus the minimum identity claim
    scopes: List[str] = []

    # ---- configuration ---------------------------------------------------

    @property
    @abc.abstractmethod
    def client_id(self) -> str: ...

    @property
    @abc.abstractmethod
    def client_secret(self) -> str: ...

    @property
    @abc.abstractmethod
    def default_redirect_uri(self) -> str: ...

    @property
    def is_configured(self) -> bool:
        """True when real credentials are present.

        An unconfigured provider is hidden from the connect UI rather than
        offered and then failing at the consent screen.
        """
        cid = (self.client_id or "").strip()
        secret = (self.client_secret or "").strip()
        return bool(cid and secret and not cid.startswith("demo-") and not secret.startswith("demo-"))

    # ---- flow ------------------------------------------------------------

    @abc.abstractmethod
    def authorization_url(self, state: str, redirect_uri: Optional[str] = None,
                          login_hint: Optional[str] = None) -> str: ...

    @abc.abstractmethod
    async def exchange_code(self, code: str, redirect_uri: Optional[str] = None) -> Dict[str, Any]: ...

    @abc.abstractmethod
    async def refresh(self, refresh_token: str) -> Dict[str, Any]: ...

    @abc.abstractmethod
    async def account_info(self, access_token: str) -> MailboxAccountInfo: ...

    async def revoke(self, token: str) -> bool:
        """Best-effort credential revocation at the provider. Never raises."""
        return False


def redact(value: Optional[str], keep: int = 4) -> str:
    """Render a credential safe for a log line.

    Used at every logging site that is anywhere near a token. Tokens must never
    appear in logs, and "I'll just log the first 30 characters while debugging"
    is exactly how they end up there.
    """
    if not value:
        return "<none>"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{'*' * (len(value) - keep)}{value[-keep:]}"
