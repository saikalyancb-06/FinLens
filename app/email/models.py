import uuid
import datetime
from sqlalchemy import (
    Column, String, DateTime, Date, Boolean, ForeignKey, Integer, Text, Float,
    Index,
)
from sqlalchemy.dialects.postgresql import UUID
from app.database.session import Base


# ---------------------------------------------------------------------------
# Connection status vocabulary
#
# Persisted as plain strings rather than a database ENUM: adding a provider or a
# state must not require a type migration, and every consumer already treats the
# value as an opaque token.
# ---------------------------------------------------------------------------
CONNECTION_CONNECTED = "CONNECTED"
CONNECTION_NEEDS_REAUTH = "NEEDS_REAUTH"
CONNECTION_REVOKED = "REVOKED"
CONNECTION_ERROR = "ERROR"
CONNECTION_DISCONNECTED = "DISCONNECTED"


class ConnectedAccount(Base):
    """One mailbox a user has authorised this application to read.

    A user may hold several: two Gmail accounts and an Outlook mailbox is an
    ordinary configuration, so nothing here assumes a single row per user. Every
    query that reaches a mailbox filters on ``user_id`` — credentials are never
    shared, looked up globally, or resolved from anything the client sends.
    """

    __tablename__ = "connected_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(50), nullable=False, default="gmail")  # gmail, microsoft, imap
    email_address = Column(String(255), nullable=False)

    # Provider-side stable identity (Google `sub`, Graph `id`, host:user for
    # IMAP). Lets a re-connect update the existing row instead of creating a
    # duplicate when the user changes the display address on their account.
    provider_account_id = Column(String(255), nullable=True, index=True)
    display_name = Column(String(255), nullable=True)

    # "oauth" or "app_password". Determines which credential column is read.
    auth_type = Column(String(32), nullable=False, default="oauth")

    # ---- credentials (encrypted at rest, see app/email/utils.py) ----------
    encrypted_refresh_token = Column(Text, nullable=True)
    access_token = Column(Text, nullable=True)          # historical name; holds ciphertext
    encrypted_secret = Column(Text, nullable=True)      # IMAP application password
    token_expires_at = Column(DateTime, nullable=True)
    scopes = Column(Text, nullable=True)

    # ---- IMAP transport ---------------------------------------------------
    imap_host = Column(String(255), nullable=True)
    imap_port = Column(Integer, nullable=True)
    imap_use_ssl = Column(Boolean, nullable=True, default=True)
    imap_username = Column(String(255), nullable=True)
    imap_mailbox = Column(String(128), nullable=True, default="INBOX")

    # ---- lifecycle --------------------------------------------------------
    is_active = Column(Boolean, default=True)
    status = Column(String(32), nullable=True, default=CONNECTION_CONNECTED)
    status_detail = Column(Text, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    last_scan_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    __table_args__ = (
        # One row per (user, provider, mailbox address). Re-connecting the same
        # mailbox updates in place rather than accumulating dead rows, and two
        # users connecting the same shared address remain independent.
        Index("ix_connected_accounts_user_provider", "user_id", "provider"),
    )

    @property
    def is_oauth(self) -> bool:
        return (self.auth_type or "oauth") == "oauth"


class MailboxScan(Base):
    """One run of the statement discovery engine over one connection.

    Persisted rather than held in memory so the UI can poll progress, a scan
    survives the request that started it, and a failure leaves an explanation
    behind instead of a spinner that never stops.
    """

    __tablename__ = "mailbox_scans"

    STATUS_QUEUED = "QUEUED"
    STATUS_RUNNING = "RUNNING"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_FAILED = "FAILED"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    connection_id = Column(UUID(as_uuid=True),
                           ForeignKey("connected_accounts.id", ondelete="CASCADE"),
                           nullable=True, index=True)
    provider = Column(String(50), nullable=True)
    email_address = Column(String(255), nullable=True)

    status = Column(String(32), nullable=False, default=STATUS_QUEUED)
    stage = Column(String(64), nullable=True)
    progress_pct = Column(Integer, nullable=False, default=0)

    messages_scanned = Column(Integer, nullable=False, default=0)
    candidates_found = Column(Integer, nullable=False, default=0)
    documents_downloaded = Column(Integer, nullable=False, default=0)
    statements_found = Column(Integer, nullable=False, default=0)
    duplicates_skipped = Column(Integer, nullable=False, default=0)
    transactions_extracted = Column(Integer, nullable=False, default=0)
    failures = Column(Integer, nullable=False, default=0)

    auto_import = Column(Boolean, nullable=False, default=False)
    error_reason = Column(String(64), nullable=True)
    error_detail = Column(Text, nullable=True)

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class EmailAttachment(Base):
    """A financial document discovered inside a mailbox.

    Despite the table name (kept for compatibility with existing data and
    endpoints) a row here may describe either a real file attachment or a
    statement that arrived as the body of the email itself — see ``source_kind``.
    """

    __tablename__ = "email_attachments"

    SOURCE_ATTACHMENT = "ATTACHMENT"
    SOURCE_BODY = "BODY"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    connected_account_id = Column(UUID(as_uuid=True), ForeignKey("connected_accounts.id", ondelete="SET NULL"), nullable=True)
    scan_id = Column(UUID(as_uuid=True), ForeignKey("mailbox_scans.id", ondelete="SET NULL"),
                     nullable=True, index=True)
    email_message_id = Column(String(255), nullable=False, index=True)
    provider_attachment_id = Column(String(512), nullable=True)
    bank_name = Column(String(100), nullable=False, default="Unknown Bank")
    subject = Column(String(500), nullable=True)
    sender = Column(String(255), nullable=True)
    received_at = Column(DateTime, nullable=True)
    filename = Column(String(255), nullable=False)
    file_size_bytes = Column(Integer, default=0)
    mime_type = Column(String(100), nullable=True)
    file_hash = Column(String(64), nullable=False, index=True)  # SHA-256
    #: content-addressed idempotency key; see app/statements/discovery.py
    dedupe_key = Column(String(128), nullable=True, index=True)
    local_path = Column(Text, nullable=True)
    is_duplicate = Column(Boolean, default=False)
    import_status = Column(String(50), default="PENDING")  # PENDING, IMPORTED, SKIPPED, FAILED
    file_id = Column(UUID(as_uuid=True), ForeignKey("uploaded_files.id", ondelete="SET NULL"), nullable=True)

    # Legacy coarse verdict, retained because the existing UI and endpoints key
    # off it: BANK_STATEMENT_CONFIRMED / NOT_BANK_STATEMENT / NEEDS_ACCOUNT_MAPPING /
    # PDF_PASSWORD_REQUIRED / PASSWORD_INVALID / UNSUPPORTED_ATTACHMENT / DUPLICATE.
    classification = Column(String(50), default="BANK_STATEMENT_CONFIRMED")
    classification_reason = Column(Text, nullable=True)

    # ---- richer classification produced by app/statements/classifier.py ----
    document_type = Column(String(40), nullable=True)          # BANK_STATEMENT, CREDIT_CARD_STATEMENT, ...
    institution_name = Column(String(200), nullable=True)      # "UNKNOWN" rather than NULL when undetermined
    institution_confidence = Column(Float, nullable=True)
    statement_period_start = Column(Date, nullable=True)
    statement_period_end = Column(Date, nullable=True)
    currency = Column(String(8), nullable=True)
    source_kind = Column(String(20), nullable=True, default=SOURCE_ATTACHMENT)
    discovery_score = Column(Float, nullable=True)

    detected_account_number = Column(String(100), nullable=True)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_email_attachments_user_dedupe", "user_id", "dedupe_key"),
    )

    @property
    def bank_account_id(self):
        return self.account_id

    @bank_account_id.setter
    def bank_account_id(self, value):
        self.account_id = value


class ImportHistory(Base):
    __tablename__ = "import_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    attachment_id = Column(UUID(as_uuid=True), ForeignKey("email_attachments.id", ondelete="SET NULL"), nullable=True, index=True)
    file_id = Column(UUID(as_uuid=True), ForeignKey("uploaded_files.id", ondelete="SET NULL"), nullable=True, index=True)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    status = Column(String(50), nullable=False)  # SUCCESS, FAILED, DUPLICATE_SKIPPED
    bank_name = Column(String(100), nullable=True)
    filename = Column(String(255), nullable=False)
    total_extracted = Column(Integer, default=0)
    total_valid = Column(Integer, default=0)
    total_stored = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    @property
    def bank_account_id(self):
        return self.account_id

    @bank_account_id.setter
    def bank_account_id(self, value):
        self.account_id = value
