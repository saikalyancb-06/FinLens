import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from app.config import settings
from app.mailbox.oauth.google import GoogleOAuthClient, google_oauth_client
from app.mailbox.gmail import GmailProvider
from app.mailbox.errors import MailboxError, AuthenticationRevoked
from app.mailbox.router import route_email


def test_google_oauth_env_driven():
    client = GoogleOAuthClient()
    with patch.object(settings, "GOOGLE_CLIENT_ID", "custom-google-id.apps.googleusercontent.com"), \
         patch.object(settings, "GOOGLE_CLIENT_SECRET", "custom-google-secret"), \
         patch.object(settings, "GOOGLE_REDIRECT_URI", "http://localhost:8000/email/oauth/callback"):
        assert client.client_id == "custom-google-id.apps.googleusercontent.com"
        assert client.client_secret == "custom-google-secret"
        assert client.default_redirect_uri == "http://localhost:8000/email/oauth/callback"
        assert "custom-google-id" in client.authorization_url(state="test-state")


def test_google_disabled_when_credentials_absent():
    with patch.object(settings, "GOOGLE_CLIENT_ID", ""), \
         patch.object(settings, "GOOGLE_CLIENT_SECRET", ""), \
         patch.object(settings, "DEMO_MODE", False):
        client = GoogleOAuthClient()
        assert not client.is_configured


def test_google_admin_policy_and_unverified_error_translations():
    client = GoogleOAuthClient()

    # 1. Admin policy error
    mock_admin_res = MagicMock()
    mock_admin_res.status_code = 400
    mock_admin_res.json.return_value = {
        "error": "admin_policy_enforced",
        "error_description": "Access blocked by organization policy",
    }
    with patch.object(settings, "DEMO_MODE", False), \
         patch.object(settings, "GOOGLE_CLIENT_ID", "test-client-id"), \
         patch.object(settings, "GOOGLE_CLIENT_SECRET", "test-secret"), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_admin_res
        with pytest.raises(MailboxError) as exc_info:
            asyncio.run(client.exchange_code("test_code"))
        assert "administrator has restricted access" in str(exc_info.value.detail)

    # 2. Testing mode / unverified app error
    mock_unverified_res = MagicMock()
    mock_unverified_res.status_code = 400
    mock_unverified_res.json.return_value = {
        "error": "access_denied",
        "error_description": "App is unverified or user not in test user list",
    }
    with patch.object(settings, "DEMO_MODE", False), \
         patch.object(settings, "GOOGLE_CLIENT_ID", "test-client-id"), \
         patch.object(settings, "GOOGLE_CLIENT_SECRET", "test-secret"), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_unverified_res
        with pytest.raises(MailboxError) as exc_info:
            asyncio.run(client.exchange_code("test_code"))
        assert "still being verified by Google" in str(exc_info.value.detail)


def test_google_silent_token_refresh():
    """Verify that an artificially expired access token triggers silent refresh and callback."""
    refreshed_tokens = {
        "access_token": "new_google_access_token_xyz",
        "expires_in": 3600,
        "token_type": "Bearer",
    }

    persisted_bundle = {}
    def on_refresh(bundle):
        persisted_bundle.update(bundle)

    provider = GmailProvider(
        access_token="artificially_expired_token",
        refresh_token="valid_refresh_token_123",
        on_token_refresh=on_refresh,
    )

    with patch.object(google_oauth_client, "refresh", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = refreshed_tokens
        result = asyncio.run(provider.refresh_authentication())

        assert result["access_token"] == "new_google_access_token_xyz"
        assert provider.access_token == "new_google_access_token_xyz"
        assert persisted_bundle["access_token"] == "new_google_access_token_xyz"


def test_google_workspace_domain_routes_terminally_to_google():
    """Google-MX / Workspace domains route strictly to Google OAuth and never IMAP."""
    with patch("app.mailbox.router.get_mx_records", new_callable=AsyncMock) as mock_mx:
        mock_mx.return_value = ["aspmx.l.google.com", "alt1.aspmx.l.google.com"]
        res = asyncio.run(route_email("finance@myworkspacecompany.com"))
        assert res["provider"] == "gmail"
        assert res["auth_type"] == "oauth"
        assert res["matched_by"] == "mx_lookup"
        assert "imap_settings" not in res
