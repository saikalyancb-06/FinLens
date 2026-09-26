"""Compatibility shim for the original Gmail-only OAuth handler.

The implementation moved to ``app/mailbox/oauth/google.py`` when Microsoft was
added, so that both providers share one flow, one error taxonomy and one set of
logging rules. This module keeps the old names working and forwards to it; there
is no second copy of the logic.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.mailbox.oauth.google import (  # noqa: F401  (re-exported)
    AUTHORIZATION_URL as GOOGLE_AUTHORIZATION_URL,
    GMAIL_SCOPES,
    REVOKE_URL as GOOGLE_REVOKE_URL,
    TOKEN_URL as GOOGLE_TOKEN_URL,
    USERINFO_URL as GOOGLE_USERINFO_URL,
    GoogleOAuthClient,
    google_oauth_client,
)


class GoogleOAuthHandler:
    """Thin adapter preserving the previous method names."""

    def __init__(self, client: Optional[GoogleOAuthClient] = None):
        self._client = client or google_oauth_client

    @property
    def client_id(self) -> str:
        return self._client.client_id

    @property
    def client_secret(self) -> str:
        return self._client.client_secret

    @property
    def redirect_uri(self) -> str:
        return self._client.default_redirect_uri

    @property
    def is_demo(self) -> bool:
        return self._client.is_demo

    def get_authorization_url(self, state: str = "state_demo",
                              redirect_uri: Optional[str] = None) -> str:
        return self._client.authorization_url(state, redirect_uri=redirect_uri)

    async def exchange_code_for_tokens(self, code: str,
                                       redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        return await self._client.exchange_code(code, redirect_uri=redirect_uri)

    async def refresh_access_token(self, refresh_token: str) -> Dict[str, Any]:
        return await self._client.refresh(refresh_token)

    async def revoke(self, token: str) -> bool:
        return await self._client.revoke(token)


google_oauth_handler = GoogleOAuthHandler()

__all__ = [
    "GoogleOAuthHandler",
    "google_oauth_handler",
    "GoogleOAuthClient",
    "google_oauth_client",
    "GMAIL_SCOPES",
    "GOOGLE_AUTHORIZATION_URL",
    "GOOGLE_TOKEN_URL",
    "GOOGLE_USERINFO_URL",
    "GOOGLE_REVOKE_URL",
]
