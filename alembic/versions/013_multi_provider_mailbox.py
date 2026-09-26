"""Multi-provider mailboxes, scan progress, and richer document classification.

Revision ID: 013
Revises: 012
Create Date: 2026-08-19

Statement discovery used to mean one thing: one Gmail account per user, read
through the Gmail API, with everything the connector needed stored in a handful
of Google-shaped columns. It now means "a mailbox", reached through Gmail,
Microsoft Graph or plain IMAP over TLS, and a user may hold several at once.
This migration is the schema half of that change:

* `connected_accounts` gains the columns a non-Google connection needs — which
  credential shape it uses (`auth_type`), the IMAP transport settings, the
  provider-side stable identity so a re-connect updates the existing row instead
  of accumulating a duplicate, and a `status` the UI can explain to the user
  instead of a bare `is_active` boolean;
* `mailbox_scans` is new. A scan of a large mailbox is not one long HTTP
  request, so its progress has to live somewhere both the worker and the polling
  UI can see — and a failure has to leave an explanation behind rather than a
  spinner that never stops;
* `email_attachments` gains the fields the content classifier produces
  (document type, institution, period, currency) plus the identity columns
  discovery deduplicates on.

Idempotent throughout. `DB_AUTO_CREATE=true` means a development database may
already have been built from the models by `create_all` before this ever runs,
so every step checks the live schema first and does nothing if the object is
already there. That is what lets such a database be stamped and upgraded rather
than rebuilt.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


CONNECTED_ACCOUNTS = "connected_accounts"
EMAIL_ATTACHMENTS = "email_attachments"
MAILBOX_SCANS = "mailbox_scans"


# Columns are declared as data rather than as a sequence of op.add_column calls
# so that upgrade, downgrade and the "is it already there?" check all read from
# the same list and cannot drift apart.
_CONNECTION_COLUMNS = {
    # Google `sub`, Graph `id`, or host:user for IMAP. Stable across a change of
    # the account's display address, which the email address itself is not.
    "provider_account_id": lambda: sa.Column("provider_account_id", sa.String(255), nullable=True),
    "display_name": lambda: sa.Column("display_name", sa.String(255), nullable=True),
    # "oauth" or "app_password" — decides which credential column the registry
    # reads. NOT NULL with a server default because every row that exists today
    # is, by definition, a Google OAuth connection.
    "auth_type": lambda: sa.Column("auth_type", sa.String(32), nullable=False,
                                   server_default="oauth"),
    # IMAP application-specific password, encrypted at rest exactly like the
    # OAuth refresh tokens beside it.
    "encrypted_secret": lambda: sa.Column("encrypted_secret", sa.Text(), nullable=True),
    "scopes": lambda: sa.Column("scopes", sa.Text(), nullable=True),
    "imap_host": lambda: sa.Column("imap_host", sa.String(255), nullable=True),
    "imap_port": lambda: sa.Column("imap_port", sa.Integer(), nullable=True),
    "imap_use_ssl": lambda: sa.Column("imap_use_ssl", sa.Boolean(), nullable=True,
                                      server_default=sa.text("true")),
    "imap_username": lambda: sa.Column("imap_username", sa.String(255), nullable=True),
    "imap_mailbox": lambda: sa.Column("imap_mailbox", sa.String(128), nullable=True,
                                      server_default="INBOX"),
    # `is_active` could only ever say "on" or "off". A connection can also be
    # revoked at the provider, out of scopes, or failing for a reason worth
    # showing; those need a vocabulary, and a place to keep the detail.
    "status": lambda: sa.Column("status", sa.String(32), nullable=True,
                                server_default="CONNECTED"),
    "status_detail": lambda: sa.Column("status_detail", sa.Text(), nullable=True),
    "last_error_at": lambda: sa.Column("last_error_at", sa.DateTime(), nullable=True),
}

_ATTACHMENT_COLUMNS = {
    "provider_attachment_id": lambda: sa.Column("provider_attachment_id", sa.String(512),
                                                nullable=True),
    "dedupe_key": lambda: sa.Column("dedupe_key", sa.String(128), nullable=True),
    "document_type": lambda: sa.Column("document_type", sa.String(40), nullable=True),
    "institution_name": lambda: sa.Column("institution_name", sa.String(200), nullable=True),
    "institution_confidence": lambda: sa.Column("institution_confidence",
                                                sa.Float(precision=53), nullable=True),
    "statement_period_start": lambda: sa.Column("statement_period_start", sa.Date(),
                                                nullable=True),
    "statement_period_end": lambda: sa.Column("statement_period_end", sa.Date(), nullable=True),
    "currency": lambda: sa.Column("currency", sa.String(8), nullable=True),
    # A statement pasted into the body of an email is still a statement. The
    # row shape is identical, so the distinction is a column and not a table.
    "source_kind": lambda: sa.Column("source_kind", sa.String(20), nullable=True,
                                     server_default="ATTACHMENT"),
    "discovery_score": lambda: sa.Column("discovery_score", sa.Float(precision=53),
                                         nullable=True),
}


def _columns(inspector, table: str) -> set:
    return {c["name"] for c in inspector.get_columns(table)}


def _indexes(inspector, table: str) -> set:
    return {i["name"] for i in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ---- connected_accounts ------------------------------------------------
    if CONNECTED_ACCOUNTS in tables:
        existing = _columns(inspector, CONNECTED_ACCOUNTS)
        for name, build in _CONNECTION_COLUMNS.items():
            if name not in existing:
                op.add_column(CONNECTED_ACCOUNTS, build())

        indexes = _indexes(inspector, CONNECTED_ACCOUNTS)
        if "ix_connected_accounts_provider_account_id" not in indexes:
            op.create_index("ix_connected_accounts_provider_account_id",
                            CONNECTED_ACCOUNTS, ["provider_account_id"])
        # Every mailbox lookup is "this user's connections, optionally of this
        # provider" — the connection id is never trusted from the request on its
        # own. This index is what makes that mandatory user filter free.
        if "ix_connected_accounts_user_provider" not in indexes:
            op.create_index("ix_connected_accounts_user_provider",
                            CONNECTED_ACCOUNTS, ["user_id", "provider"])

        # Backfill. Rows written before this migration are Google OAuth
        # connections whose only lifecycle signal was `is_active`; translate it
        # once here so no consumer has to keep reading both.
        op.execute(sa.text(
            f"UPDATE {CONNECTED_ACCOUNTS} SET auth_type = 'oauth' WHERE auth_type IS NULL"
        ))
        op.execute(sa.text(
            f"UPDATE {CONNECTED_ACCOUNTS} SET status = 'CONNECTED' "
            "WHERE is_active IS TRUE AND status IS NULL"
        ))
        op.execute(sa.text(
            f"UPDATE {CONNECTED_ACCOUNTS} SET status = 'DISCONNECTED' "
            "WHERE is_active IS FALSE AND status IS NULL"
        ))

    # ---- mailbox_scans -----------------------------------------------------
    # Created before the email_attachments.scan_id column below, which carries a
    # foreign key into it.
    if MAILBOX_SCANS not in tables:
        op.create_table(
            MAILBOX_SCANS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            # CASCADE, not SET NULL: a scan is a record of reading one specific
            # mailbox, and once that connection is gone the row describes
            # nothing anyone can act on. The documents it found survive on
            # email_attachments, whose scan_id is SET NULL for exactly that
            # reason.
            sa.Column("connection_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey(f"{CONNECTED_ACCOUNTS}.id", ondelete="CASCADE"),
                      nullable=True),
            sa.Column("provider", sa.String(50), nullable=True),
            sa.Column("email_address", sa.String(255), nullable=True),

            sa.Column("status", sa.String(32), nullable=False, server_default="QUEUED"),
            sa.Column("stage", sa.String(64), nullable=True),
            sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),

            # Counters are NOT NULL DEFAULT 0 so the UI can render a scan that
            # has only just been queued without special-casing nulls.
            sa.Column("messages_scanned", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidates_found", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("documents_downloaded", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("statements_found", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duplicates_skipped", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("transactions_extracted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),

            sa.Column("auto_import", sa.Boolean(), nullable=False,
                      server_default=sa.text("false")),
            # A machine-readable reason plus the human detail behind it: the
            # first drives what the UI offers (reconnect, retry, nothing), the
            # second is what a support conversation actually needs.
            sa.Column("error_reason", sa.String(64), nullable=True),
            sa.Column("error_detail", sa.Text(), nullable=True),

            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_mailbox_scans_user_id", MAILBOX_SCANS, ["user_id"])
        op.create_index("ix_mailbox_scans_connection_id", MAILBOX_SCANS, ["connection_id"])

    # ---- email_attachments -------------------------------------------------
    if EMAIL_ATTACHMENTS in tables:
        existing = _columns(inspector, EMAIL_ATTACHMENTS)
        for name, build in _ATTACHMENT_COLUMNS.items():
            if name not in existing:
                op.add_column(EMAIL_ATTACHMENTS, build())

        if "scan_id" not in existing:
            op.add_column(EMAIL_ATTACHMENTS,
                          sa.Column("scan_id", postgresql.UUID(as_uuid=True), nullable=True))
            # SET NULL rather than CASCADE: deleting a scan record must never
            # take the discovered documents with it. The scan is provenance,
            # not ownership.
            op.create_foreign_key("fk_email_attachments_scan_id", EMAIL_ATTACHMENTS,
                                  MAILBOX_SCANS, ["scan_id"], ["id"], ondelete="SET NULL")

        indexes = _indexes(inspector, EMAIL_ATTACHMENTS)
        if "ix_email_attachments_scan_id" not in indexes:
            op.create_index("ix_email_attachments_scan_id", EMAIL_ATTACHMENTS, ["scan_id"])
        if "ix_email_attachments_dedupe_key" not in indexes:
            op.create_index("ix_email_attachments_dedupe_key", EMAIL_ATTACHMENTS, ["dedupe_key"])
        # Deduplication is always asked per user — two users may hold the same
        # statement and neither is a duplicate of the other.
        #
        # Deliberately NOT a unique index. Rows written before this migration
        # have never been checked for duplicates, so there is no reason to
        # believe (user_id, dedupe_key) is already unique on live data; a
        # CREATE UNIQUE INDEX that hits one collision aborts the whole
        # migration and leaves the deployment stuck. Uniqueness is therefore
        # enforced in the application — app/statements/discovery.py
        # find_existing() looks a document up before persisting it — and this
        # index exists to make that lookup cheap rather than to police it.
        if "ix_email_attachments_user_dedupe" not in indexes:
            op.create_index("ix_email_attachments_user_dedupe", EMAIL_ATTACHMENTS,
                            ["user_id", "dedupe_key"])

        # Backfill.
        #
        # dedupe_key comes from file_hash because that is exactly what the old
        # code deduplicated on: the SHA-256 of the document's bytes. Seeding it
        # this way means the first scan after the upgrade recognises everything
        # already discovered instead of re-importing the user's entire history
        # as new.
        op.execute(sa.text(
            f"UPDATE {EMAIL_ATTACHMENTS} SET dedupe_key = file_hash WHERE dedupe_key IS NULL"
        ))
        # Every pre-existing row was found as a real file attachment; the
        # body-extraction path did not exist yet.
        op.execute(sa.text(
            f"UPDATE {EMAIL_ATTACHMENTS} SET source_kind = 'ATTACHMENT' WHERE source_kind IS NULL"
        ))
        # bank_name was the old, coarser answer to the same question the
        # classifier now answers as institution_name. Carrying it across keeps
        # the existing rows visible in a UI that reads the new column.
        op.execute(sa.text(
            f"UPDATE {EMAIL_ATTACHMENTS} SET institution_name = bank_name "
            "WHERE institution_name IS NULL"
        ))
        # Only the confirmed verdict maps onto a document type without guessing.
        # Anything else stays NULL: an unclassified row is honest, an invented
        # document_type is not.
        op.execute(sa.text(
            f"UPDATE {EMAIL_ATTACHMENTS} SET document_type = 'BANK_STATEMENT' "
            "WHERE classification = 'BANK_STATEMENT_CONFIRMED' AND document_type IS NULL"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if EMAIL_ATTACHMENTS in tables:
        indexes = _indexes(inspector, EMAIL_ATTACHMENTS)
        for name in ("ix_email_attachments_user_dedupe",
                     "ix_email_attachments_dedupe_key",
                     "ix_email_attachments_scan_id"):
            if name in indexes:
                op.drop_index(name, table_name=EMAIL_ATTACHMENTS)

        existing = _columns(inspector, EMAIL_ATTACHMENTS)
        # scan_id first: it holds the foreign key into mailbox_scans, which is
        # dropped below. Dropping the column takes its constraint with it.
        if "scan_id" in existing:
            op.drop_column(EMAIL_ATTACHMENTS, "scan_id")
        for name in _ATTACHMENT_COLUMNS:
            if name in existing:
                op.drop_column(EMAIL_ATTACHMENTS, name)

    if MAILBOX_SCANS in tables:
        op.drop_table(MAILBOX_SCANS)

    if CONNECTED_ACCOUNTS in tables:
        indexes = _indexes(inspector, CONNECTED_ACCOUNTS)
        for name in ("ix_connected_accounts_user_provider",
                     "ix_connected_accounts_provider_account_id"):
            if name in indexes:
                op.drop_index(name, table_name=CONNECTED_ACCOUNTS)

        existing = _columns(inspector, CONNECTED_ACCOUNTS)
        for name in _CONNECTION_COLUMNS:
            if name in existing:
                op.drop_column(CONNECTED_ACCOUNTS, name)
