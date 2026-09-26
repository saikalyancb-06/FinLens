import asyncio
import datetime as _dt
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import httpx

from app.config import settings
from app.mailbox.oauth.microsoft import MicrosoftOAuthClient, microsoft_oauth_client
from app.mailbox.microsoft import MicrosoftGraphProvider
from app.mailbox.errors import MailboxError, AuthenticationRevoked
from app.email.models import ConnectedAccount
from app.email.utils import encrypt_token, decrypt_token


def test_microsoft_oauth_env_driven():
    client = MicrosoftOAuthClient()
    assert client.tenant == "common"
    assert client.authorization_endpoint == "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    assert client.token_endpoint == "https://login.microsoftonline.com/common/oauth2/v2.0/token"


def test_microsoft_disabled_when_credentials_absent():
    with patch.object(settings, "MICROSOFT_CLIENT_ID", ""), \
         patch.object(settings, "MICROSOFT_CLIENT_SECRET", ""), \
         patch.object(settings, "DEMO_MODE", False):
        client = MicrosoftOAuthClient()
        assert not client.is_configured


def test_admin_consent_error_in_exchange_code():
    client = MicrosoftOAuthClient()
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.json.return_value = {
        "error": "invalid_grant",
        "error_description": "AADSTS65001: The user or administrator has not consented to use the application with ID ...",
    }

    with patch.object(settings, "DEMO_MODE", False), \
         patch.object(settings, "MICROSOFT_CLIENT_ID", "test-client-id"), \
         patch.object(settings, "MICROSOFT_CLIENT_SECRET", "test-secret"), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        with pytest.raises(MailboxError) as exc_info:
            asyncio.run(client.exchange_code("test_code"))

        assert "admin needs to approve this app once" in str(exc_info.value.detail)


def test_silent_token_refresh_persists_new_tokens():
    """Verify that an artificially expired access token triggers silent refresh and rotation."""
    refreshed_tokens = {
        "access_token": "new_access_token_xyz123",
        "refresh_token": "new_rotated_refresh_token_abc789",
        "expires_in": 3600,
        "token_type": "Bearer",
    }

    persisted_bundle = {}
    def on_refresh(bundle):
        persisted_bundle.update(bundle)

    provider = MicrosoftGraphProvider(
        access_token="artificially_expired_token",
        refresh_token="valid_refresh_token_123",
        on_token_refresh=on_refresh,
    )

    with patch.object(microsoft_oauth_client, "refresh", new_callable=AsyncMock) as mock_refresh:
        mock_refresh.return_value = refreshed_tokens
        result = asyncio.run(provider.refresh_authentication())

        assert result["access_token"] == "new_access_token_xyz123"
        assert provider.access_token == "new_access_token_xyz123"
        assert provider.refresh_token == "new_rotated_refresh_token_abc789"
        assert persisted_bundle["access_token"] == "new_access_token_xyz123"
        assert persisted_bundle["refresh_token"] == "new_rotated_refresh_token_abc789"
