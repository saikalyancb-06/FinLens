"""OAuth clients, keyed by mailbox provider."""
from app.mailbox.oauth.base import OAuthClient, redact
from app.mailbox.oauth.google import GoogleOAuthClient, google_oauth_client
from app.mailbox.oauth.microsoft import MicrosoftOAuthClient, microsoft_oauth_client

OAUTH_CLIENTS = {
    "gmail": google_oauth_client,
    "microsoft": microsoft_oauth_client,
}


def get_oauth_client(provider: str) -> OAuthClient:
    """Return the OAuth client for ``provider``.

    Raises ``ProviderNotSupported`` rather than KeyError so route handlers can
    map it straight onto a 400 without a bare-except.
    """
    from app.mailbox.errors import ProviderNotSupported

    client = OAUTH_CLIENTS.get((provider or "").strip().lower())
    if client is None:
        raise ProviderNotSupported(f"'{provider}' is not an OAuth mailbox provider.")
    return client


__all__ = [
    "OAuthClient",
    "GoogleOAuthClient",
    "MicrosoftOAuthClient",
    "google_oauth_client",
    "microsoft_oauth_client",
    "OAUTH_CLIENTS",
    "get_oauth_client",
    "redact",
]
