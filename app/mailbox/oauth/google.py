"""Google OAuth 2.0 client for Gmail mailbox access.

Scopes are the read-only pair and nothing else: ``gmail.readonly`` to list
messages and fetch attachments, and ``userinfo.email`` + ``openid`` so we can
label the connection with the address the user actually authorised.
No send scope, no modify scope, no contacts.

Multi-user OAuth 2.0:
- Application client credentials (``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET``)
  are configured in the environment.
- Each user authenticates via their own consent popup with state tied to their user ID.
- Each user's refresh token and access token are encrypted (AES-GCM) and stored
  per-user in ``connected_accounts``.
- Note: ``gmail.readonly`` is a restricted scope requiring CASA (Cloud Application
  Security Assessment) Tier 2/3 verification before production. In testing mode,
  the app works with authorized test users configured in Google Cloud Console.
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Any, Dict, Optional

import httpx

from app.mailbox.errors import (
    AuthenticationRevoked,
    InsufficientPermissions,
    MailboxError,
    ProviderUnavailable,
)
from app.mailbox.oauth.base import OAuthClient, redact
from app.mailbox.types import MailboxAccountInfo

logger = logging.getLogger(__name__)

AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


class GoogleOAuthClient(OAuthClient):
    provider_key = "gmail"
    scopes = GMAIL_SCOPES

    @property
    def client_id(self) -> str:
        from app.config import settings
        val = getattr(settings, "GOOGLE_CLIENT_ID", None)
        if val is not None:
            return val.strip()
        return os.getenv("GOOGLE_CLIENT_ID", "").strip()

    @property
    def client_secret(self) -> str:
        from app.config import settings
        val = getattr(settings, "GOOGLE_CLIENT_SECRET", None)
        if val is not None:
            return val.strip()
        return os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

    @property
    def default_redirect_uri(self) -> str:
        from app.config import settings
        val = getattr(settings, "GOOGLE_REDIRECT_URI", None)
        if val is not None:
            return val.strip()
        return os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/email/oauth/callback").strip()

    @property
    def is_demo(self) -> bool:
        from app.config import settings

        return settings.DEMO_MODE

    @property
    def is_configured(self) -> bool:
        from app.config import settings

        if settings.DEMO_MODE:
            return True
        return bool(self.client_id and self.client_secret and not self.client_id.startswith("demo-"))

    # ---- flow ------------------------------------------------------------

    def authorization_url(self, state: str, redirect_uri: Optional[str] = None) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": (redirect_uri or self.default_redirect_uri).strip(),
            "response_type": "code",
            "scope": " ".join(self.scopes),
            # offline + consent so a refresh token is issued even when the user
            # has authorised this application before; without it a re-connect
            # returns an access token only and the connection dies in an hour.
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTHORIZATION_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        from app.config import settings

        if settings.DEMO_MODE or code.startswith("demo_"):
            logger.info("[Google OAuth] demo code accepted; returning fixture token bundle")
            return {
                "access_token": f"demo_access_token_{code}",
                "refresh_token": f"demo_refresh_token_{code}",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": " ".join(self.scopes),
                "email": "user.bank.statements@gmail.com",
                "provider_account_id": f"demo-google-{code}",
            }

        if not self.client_id or not self.client_secret:
            raise MailboxError(
                "Google sign-in is not configured on this server "
                "(GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are unset).",
                provider="gmail",
            )

        r_uri = (redirect_uri or self.default_redirect_uri).strip()
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": r_uri,
        }
        logger.info(
            "[Google OAuth] token exchange client_id=%s redirect_uri=%s code=%s",
            self.client_id, r_uri, redact(code),
        )
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(TOKEN_URL, data=payload)
            if res.status_code != 200:
                try:
                    res_json = res.json()
                    err = res_json.get("error", "")
                    err_desc = res_json.get("error_description", "")
                except Exception:
                    err = ""
                    err_desc = ""

                err_lower = f"{err} {err_desc}".lower()
                if "admin_policy_enforced" in err_lower or "org_internal" in err_lower:
                    raise MailboxError(
                        "Your Google Workspace administrator has restricted access to this app.",
                        provider="gmail",
                    )
                if "access_denied" in err_lower or "unverified" in err_lower or "test" in err_lower:
                    raise MailboxError(
                        "This app is still being verified by Google — if you're a test user, continue past the warning screen; otherwise access isn't available yet.",
                        provider="gmail",
                    )

                raise MailboxError(
                    f"Google rejected the authorization code (HTTP {res.status_code}"
                    f"{', ' + err if err else ''}).",
                    provider="gmail",
                )
            tokens = res.json()

        info = await self.account_info(tokens.get("access_token", ""))
        tokens["email"] = info.email_address
        tokens["provider_account_id"] = info.provider_account_id
        return tokens

    async def refresh(self, refresh_token: str) -> Dict[str, Any]:
        """Silently refresh an expired access token using the stored refresh token.

        NOTE on Google Testing Mode: In Google Cloud OAuth testing mode, refresh
        tokens expire after 7 days and the user must reconsent — this is expected
        Google policy and automatically resolves once the app is verified/published.
        """
        from app.config import settings

        if settings.DEMO_MODE or refresh_token.startswith("demo_"):
            return {
                "access_token": f"demo_refreshed_access_token_{refresh_token[-8:]}",
                "expires_in": 3600,
                "token_type": "Bearer",
            }

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                res = await client.post(TOKEN_URL, data=data)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Could not reach Google to refresh the token: {exc}",
                                      provider="gmail") from exc

        if res.status_code == 200:
            return res.json()

        error = ""
        try:
            error = res.json().get("error", "")
        except Exception:
            pass
        # invalid_grant is Google's single answer to "revoked", "expired" and
        # "user changed their password". All three need the same user action.
        if error in {"invalid_grant", "unauthorized_client"} or res.status_code in (400, 401):
            raise AuthenticationRevoked(
                "Google no longer accepts the stored credentials for this mailbox. "
                "Please reconnect the account.",
                provider="gmail",
            )
        raise ProviderUnavailable(f"Google token refresh failed (HTTP {res.status_code}).",
                                  provider="gmail")

    async def account_info(self, access_token: str) -> MailboxAccountInfo:
        from app.config import settings

        if settings.DEMO_MODE or (access_token or "").startswith("demo_"):
            return MailboxAccountInfo(
                email_address="user.bank.statements@gmail.com",
                provider_account_id="demo-google-account",
                display_name="Demo Gmail",
                provider="gmail",
            )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                res = await client.get(USERINFO_URL,
                                       headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Could not reach Google userinfo: {exc}",
                                      provider="gmail") from exc

        if res.status_code == 403:
            raise InsufficientPermissions(
                "The Google account did not grant permission to read the mailbox address.",
                provider="gmail",
            )
        if res.status_code != 200:
            raise ProviderUnavailable(f"Google userinfo failed (HTTP {res.status_code}).",
                                      provider="gmail")
        body = res.json()
        return MailboxAccountInfo(
            email_address=body.get("email", ""),
            provider_account_id=str(body.get("id") or body.get("sub") or body.get("email", "")),
            display_name=body.get("name"),
            provider="gmail",
            raw=body,
        )

    async def revoke(self, token: str) -> bool:
        from app.config import settings

        if not token or settings.DEMO_MODE or token.startswith("demo_"):
            return True
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                res = await client.post(REVOKE_URL, data={"token": token})
            # 400 means the token was already invalid, which is the desired end
            # state, so it counts as success.
            return res.status_code in (200, 400)
        except httpx.HTTPError as exc:
            logger.warning("[Google OAuth] revoke call failed (credential is deleted locally regardless): %s", exc)
            return False


google_oauth_client = GoogleOAuthClient()
