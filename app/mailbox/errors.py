"""Error taxonomy shared by every mailbox provider.

The scan engine and the API layer must react to *what went wrong* without
knowing *which provider* it happened to. A Gmail 401, a Microsoft Graph
``InvalidAuthenticationToken`` and an IMAP ``AUTHENTICATIONFAILED`` all mean the
same thing to a user — "reconnect your mailbox" — so every connector maps its
own vocabulary onto the classes below and nothing above this layer ever inspects
a provider-specific status code.
"""
from __future__ import annotations

from typing import Optional


class MailboxError(Exception):
    """Base class for every failure raised by a provider connector.

    ``reason`` is a stable machine-readable slug that the API surfaces to the
    front-end; ``detail`` is the human-facing sentence.
    """

    reason = "mailbox_error"
    #: connection status to persist when this error escapes a scan
    connection_status = "ERROR"

    def __init__(self, detail: str = "", *, provider: Optional[str] = None):
        self.detail = detail or self.__doc__ or self.reason
        self.provider = provider
        super().__init__(self.detail)

    def as_dict(self) -> dict:
        return {"reason": self.reason, "detail": self.detail, "provider": self.provider}


class AuthenticationExpired(MailboxError):
    """The stored access token has expired and could not be refreshed silently."""

    reason = "auth_expired"
    connection_status = "NEEDS_REAUTH"


class AuthenticationRevoked(MailboxError):
    """The user (or their administrator) revoked this application's access."""

    reason = "auth_revoked"
    connection_status = "REVOKED"


class InsufficientPermissions(MailboxError):
    """The granted scopes do not allow reading mail or attachments."""

    reason = "insufficient_permissions"
    connection_status = "NEEDS_REAUTH"


class CredentialsUnreadable(MailboxError):
    """Stored credentials could not be decrypted; the mailbox must be reconnected."""

    reason = "credentials_unreadable"
    connection_status = "NEEDS_REAUTH"


class RateLimited(MailboxError):
    """The provider is throttling us; the scan should back off and resume later."""

    reason = "rate_limited"
    connection_status = "CONNECTED"

    def __init__(self, detail: str = "", *, provider: Optional[str] = None,
                 retry_after: Optional[float] = None):
        super().__init__(detail, provider=provider)
        self.retry_after = retry_after

    def as_dict(self) -> dict:
        payload = super().as_dict()
        payload["retry_after"] = self.retry_after
        return payload


class ProviderUnavailable(MailboxError):
    """The provider returned a server-side error or could not be reached."""

    reason = "provider_unavailable"
    connection_status = "CONNECTED"


class MessageNotFound(MailboxError):
    """The requested message or attachment no longer exists in the mailbox."""

    reason = "message_not_found"
    connection_status = "CONNECTED"


class ProviderNotSupported(MailboxError):
    """No connector is registered for the requested provider key."""

    reason = "provider_not_supported"


class ConnectionConfigurationError(MailboxError):
    """The stored connection is missing information the connector needs."""

    reason = "connection_misconfigured"
    connection_status = "ERROR"
