"""Microsoft identity platform (v2.0) OAuth client for Microsoft Graph mailboxes.

Multi-Tenant Architecture:
- The Entra application is registered as Multi-Tenant.
- ``MICROSOFT_TENANT`` defaults to ``common``, allowing both personal accounts
  (Outlook.com / Hotmail / Live) and Microsoft 365 work/school mailboxes.
- Scopes are read-only: ``Mail.Read`` (user-consentable delegated permission),
  ``User.Read`` for profile/email identification, and ``offline_access`` for token refresh.
- Note: In organizations where tenant policies restrict user consent, a tenant
  admin must grant consent once for the application.
- Tokens are encrypted (AES-GCM) and stored per-user in ``connected_accounts``.
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

GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"

MICROSOFT_SCOPES = [
    "offline_access",
    "openid",
    "email",
    "profile",
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/User.Read",
]


class MicrosoftOAuthClient(OAuthClient):
    provider_key = "microsoft"
    scopes = MICROSOFT_SCOPES

    @property
    def tenant(self) -> str:
        from app.config import settings
        val = getattr(settings, "MICROSOFT_TENANT", None)
        if val:
            return val.strip()
        return os.getenv("MICROSOFT_TENANT", "common").strip() or "common"

    @property
    def client_id(self) -> str:
        from app.config import settings
        val = getattr(settings, "MICROSOFT_CLIENT_ID", None)
        if val is not None:
            return val.strip()
        return os.getenv("MICROSOFT_CLIENT_ID", "").strip()

    @property
    def client_secret(self) -> str:
        from app.config import settings
        val = getattr(settings, "MICROSOFT_CLIENT_SECRET", None)
        if val is not None:
            return val.strip()
        return os.getenv("MICROSOFT_CLIENT_SECRET", "").strip()

    @property
    def default_redirect_uri(self) -> str:
        from app.config import settings
        val = getattr(settings, "MICROSOFT_REDIRECT_URI", None)
        if val is not None:
            return val.strip()
        return os.getenv("MICROSOFT_REDIRECT_URI", "http://localhost:8000/email/oauth/microsoft/callback").strip()

    @property
    def authorization_endpoint(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"

    @property
    def is_configured(self) -> bool:
        from app.config import settings

        if settings.DEMO_MODE:
            return True
        return bool(self.client_id and self.client_secret)

    # ---- flow ------------------------------------------------------------

    def authorization_url(self, state: str, redirect_uri: Optional[str] = None) -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": (redirect_uri or self.default_redirect_uri).strip(),
            "response_mode": "query",
            "scope": " ".join(self.scopes),
            "state": state,
            # select_account rather than consent: Microsoft re-issues a refresh
            # token on every authorization-code exchange, so forcing the consent
            # screen on a returning user buys nothing but friction.
            "prompt": "select_account",
        }
        return f"{self.authorization_endpoint}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        from app.config import settings

        if settings.DEMO_MODE or code.startswith("demo_"):
            logger.info("[Microsoft OAuth] demo code accepted; returning fixture token bundle")
            return {
                "access_token": f"demo_access_token_{code}",
                "refresh_token": f"demo_refresh_token_{code}",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": " ".join(self.scopes),
                "email": "user.bank.statements@outlook.com",
                "provider_account_id": f"demo-microsoft-{code}",
            }

        if not self.client_id:
            raise MailboxError(
                "Microsoft sign-in is not configured on this server "
                "(MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET are unset).",
                provider="microsoft",
            )

        r_uri = (redirect_uri or self.default_redirect_uri).strip()
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": r_uri,
            "scope": " ".join(self.scopes),
        }
        logger.info(
            "[Microsoft OAuth] token exchange tenant=%s redirect_uri=%s code=%s",
            self.tenant, r_uri, redact(code),
        )
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(self.token_endpoint, data=data)
            if res.status_code != 200:
                try:
                    res_json = res.json()
                    err = res_json.get("error", "")
                    err_desc = res_json.get("error_description", "")
                except Exception:
                    err = ""
                    err_desc = ""

                # Check for admin consent requirements in work/school tenants
                err_lower = f"{err} {err_desc}".lower()
                if any(code in err_lower for code in ("aadsts65001", "aadsts90094", "aadsts90093", "admin_consent")) or ("admin" in err_lower and "consent" in err_lower):
                    raise MailboxError(
                        "Your organization's admin needs to approve this app once before you can connect your work or school Microsoft account.",
                        provider="microsoft",
                    )

                raise MailboxError(
                    f"Microsoft rejected the authorization code (HTTP {res.status_code}"
                    f"{', ' + err if err else ''}).",
                    provider="microsoft",
                )
            tokens = res.json()

        info = await self.account_info(tokens.get("access_token", ""))
        tokens["email"] = info.email_address
        tokens["provider_account_id"] = info.provider_account_id
        return tokens

    async def refresh(self, refresh_token: str) -> Dict[str, Any]:
        from app.config import settings

        if settings.DEMO_MODE or refresh_token.startswith("demo_"):
            return {
                "access_token": f"demo_refreshed_access_token_{refresh_token[-8:]}",
                "refresh_token": refresh_token,
                "expires_in": 3600,
                "token_type": "Bearer",
            }

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": " ".join(self.scopes),
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                res = await client.post(self.token_endpoint, data=data)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Could not reach Microsoft to refresh the token: {exc}",
                                      provider="microsoft") from exc

        if res.status_code == 200:
            return res.json()

        error = ""
        try:
            error = res.json().get("error", "")
        except Exception:
            pass
        if error in {"invalid_grant", "unauthorized_client", "interaction_required"} or res.status_code in (400, 401):
            raise AuthenticationRevoked(
                "Microsoft no longer accepts the stored credentials for this mailbox. "
                "Please reconnect the account.",
                provider="microsoft",
            )
        raise ProviderUnavailable(f"Microsoft token refresh failed (HTTP {res.status_code}).",
                                  provider="microsoft")

    async def account_info(self, access_token: str) -> MailboxAccountInfo:
        from app.config import settings

        if settings.DEMO_MODE or (access_token or "").startswith("demo_"):
            return MailboxAccountInfo(
                email_address="user.bank.statements@outlook.com",
                provider_account_id="demo-microsoft-account",
                display_name="Demo Outlook",
                provider="microsoft",
            )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                res = await client.get(GRAPH_ME_URL,
                                       headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Could not reach Microsoft Graph: {exc}",
                                      provider="microsoft") from exc

        if res.status_code == 403:
            raise InsufficientPermissions(
                "The Microsoft account did not grant permission to read the mailbox.",
                provider="microsoft",
            )
        if res.status_code != 200:
            raise ProviderUnavailable(f"Microsoft Graph /me failed (HTTP {res.status_code}).",
                                      provider="microsoft")
        body = res.json()
        # Personal accounts populate userPrincipalName with the address; work
        # accounts may only populate mail. Either can be the empty string, so
        # both are tried before giving up.
        address = body.get("mail") or body.get("userPrincipalName") or ""
        return MailboxAccountInfo(
            email_address=address,
            provider_account_id=str(body.get("id") or address),
            display_name=body.get("displayName"),
            provider="microsoft",
            raw=body,
        )

    async def revoke(self, token: str) -> bool:
        # Microsoft has no per-token revocation endpoint for confidential
        # clients; a user revokes access from their account portal. Deleting the
        # stored credential is what actually stops this application reading the
        # mailbox, and that happens on disconnect regardless.
        return False


microsoft_oauth_client = MicrosoftOAuthClient()
