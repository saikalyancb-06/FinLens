"""Provider-agnostic mailbox access.

    EmailProvider
      ├── GmailProvider              (Gmail REST API)
      ├── MicrosoftGraphProvider     (Microsoft Graph)
      └── ImapProvider               (IMAP over TLS, XOAUTH2 or app password)

Everything above this package — statement discovery, document classification,
transaction extraction, the API and the UI — is written against the interface,
never against a connector. Adding a fourth provider means writing one module and
registering it; no statement-detection code changes.

Reading only. There is no send path here, and SMTP is not used anywhere in the
mailbox flow: it is a protocol for delivering mail, not for retrieving it.
"""
from app.mailbox.base import EmailProvider
from app.mailbox.criteria import DEFAULT_STATEMENT_KEYWORDS, SearchCriteria
from app.mailbox.errors import (
    AuthenticationExpired,
    AuthenticationRevoked,
    ConnectionConfigurationError,
    CredentialsUnreadable,
    InsufficientPermissions,
    MailboxError,
    MessageNotFound,
    ProviderNotSupported,
    ProviderUnavailable,
    RateLimited,
)
from app.mailbox.registry import (
    PROVIDER_CATALOG,
    PROVIDER_CLASSES,
    is_supported,
    mark_connection_failed,
    mark_connection_healthy,
    normalise_provider,
    open_provider,
)
from app.mailbox.types import (
    AttachmentPayload,
    AttachmentRef,
    MailboxAccountInfo,
    MessageDetail,
    MessageSummary,
)

__all__ = [
    "EmailProvider",
    "SearchCriteria",
    "DEFAULT_STATEMENT_KEYWORDS",
    "MailboxError",
    "AuthenticationExpired",
    "AuthenticationRevoked",
    "InsufficientPermissions",
    "CredentialsUnreadable",
    "ConnectionConfigurationError",
    "MessageNotFound",
    "ProviderNotSupported",
    "ProviderUnavailable",
    "RateLimited",
    "PROVIDER_CATALOG",
    "PROVIDER_CLASSES",
    "open_provider",
    "normalise_provider",
    "is_supported",
    "mark_connection_failed",
    "mark_connection_healthy",
    "MailboxAccountInfo",
    "MessageSummary",
    "MessageDetail",
    "AttachmentRef",
    "AttachmentPayload",
]
