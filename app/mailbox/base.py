"""The single interface every mailbox connector implements.

Nothing outside ``app/mailbox`` may import a concrete connector. Callers ask
``app.mailbox.registry.open_provider(connection)`` for an ``EmailProvider`` and
work through the methods below, so a mailbox reached over Gmail's REST API, over
Microsoft Graph, or over IMAP is indistinguishable to the statement engine.
"""
from __future__ import annotations

import abc
import logging
from typing import AsyncIterator, List, Optional

from app.mailbox.criteria import SearchCriteria
from app.mailbox.types import (
    AttachmentPayload,
    AttachmentRef,
    MailboxAccountInfo,
    MessageDetail,
    MessageSummary,
)

logger = logging.getLogger(__name__)


class EmailProvider(abc.ABC):
    """Read-only access to one user's mailbox.

    Read-only is enforced by omission: there is no send, no move, no delete and
    no flag-setting anywhere in this interface, and the OAuth scopes requested by
    the connectors are the read-only ones. A bug in the statement engine cannot
    modify a user's mail because the vocabulary to do so does not exist here.

    Implementations are constructed with an already-decrypted credential bundle
    (see ``registry.open_provider``); they never touch the database and never see
    an application user id. Ownership is settled before a provider is opened.
    """

    #: registry key, e.g. "gmail"
    key: str = ""
    #: human label for the UI
    label: str = ""
    #: how the mailbox is authenticated: "oauth" or "app_password"
    auth_type: str = "oauth"

    # ---- lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        """Establish whatever session the transport needs.

        HTTP-based providers have nothing to do here; IMAP opens its socket and
        selects a mailbox. Always paired with ``disconnect``.
        """

    @abc.abstractmethod
    async def authenticate(self) -> None:
        """Prove the stored credentials still work.

        Raises ``AuthenticationExpired`` / ``AuthenticationRevoked`` /
        ``InsufficientPermissions`` rather than returning a boolean, so callers
        cannot forget to check.
        """

    @abc.abstractmethod
    async def refresh_authentication(self) -> Optional[dict]:
        """Silently renew the access credential.

        Returns the fields the caller should persist
        (``access_token`` / ``expires_in`` / possibly a rotated
        ``refresh_token``) or ``None`` when the provider has nothing to rotate
        (IMAP with an app password).
        """

    @abc.abstractmethod
    async def get_account_info(self) -> MailboxAccountInfo:
        """Identify the mailbox this connection actually reaches."""

    # ---- reading ---------------------------------------------------------

    @abc.abstractmethod
    def search_messages(self, criteria: SearchCriteria) -> AsyncIterator[MessageSummary]:
        """Yield cheap message summaries matching ``criteria``.

        An async generator, not a list: the scan engine stops consuming as soon
        as it has enough candidates, and a large mailbox is never materialised in
        memory.
        """

    @abc.abstractmethod
    async def get_message(self, message_id: str) -> MessageDetail:
        """Fetch one message in full, with bodies decoded and MIME tree walked."""

    async def get_attachments(self, message_id: str) -> List[AttachmentRef]:
        """List attachment references for a message.

        The default implementation reuses ``get_message``; connectors that expose
        a cheaper dedicated endpoint (Graph does) override it.
        """
        detail = await self.get_message(message_id)
        return detail.attachments

    @abc.abstractmethod
    async def download_attachment(self, message_id: str, attachment: AttachmentRef) -> AttachmentPayload:
        """Fetch the decoded bytes of one attachment."""

    # ---- teardown --------------------------------------------------------

    async def disconnect(self) -> None:
        """Close transport resources. Must be safe to call twice."""

    async def revoke_access(self) -> None:
        """Tell the provider to invalidate our credentials, if it supports that.

        Called on user-initiated disconnect. A provider without a revocation
        endpoint (IMAP) leaves this as a no-op — the credential is deleted
        locally either way.
        """

    # ---- context manager sugar ------------------------------------------

    async def __aenter__(self) -> "EmailProvider":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.disconnect()
