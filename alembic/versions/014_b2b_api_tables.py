"""B2B Financial Analysis API tables (/v1/analyze, /internal/clients).

Revision ID: 014
Revises: 013
Create Date: 2026-09-28

The B2B module (app/b2b/models.py) shipped without a migration. Locally that
went unnoticed because DB_AUTO_CREATE defaults to true outside production and
create_all() built the tables on first start. In production DB_AUTO_CREATE is
false and Alembic owns the schema, so on any database that was bootstrapped
before the B2B module existed, `alembic upgrade head` left these five tables
missing and every /v1 call and every admin call failed with a 500
("relation api_clients does not exist").

Idempotent in both directions, like 012: a table that already exists (because
the database was bootstrapped with create_all after the B2B module landed) is
skipped, and the downgrade drops only tables that are present.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
NOW = sa.text("now()")


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _tables()

    if "api_clients" not in existing:
        op.create_table(
            "api_clients",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("slug", sa.String(80), nullable=False),
            sa.Column("contact_email", sa.String(255), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("plan", sa.String(40), nullable=False),
            sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False),
            sa.Column("rate_limit_per_day", sa.Integer(), nullable=False),
            sa.Column("rate_limit_per_month", sa.Integer(), nullable=False),
            sa.Column("max_file_size_bytes", sa.Integer(), nullable=True),
            sa.Column("result_retention_hours", sa.Integer(), nullable=False),
            sa.Column("webhook_url", sa.String(500), nullable=True),
            sa.Column("webhook_secret", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_api_clients_slug", "api_clients", ["slug"], unique=True)

    if "api_keys" not in existing:
        op.create_table(
            "api_keys",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("client_id", UUID,
                      sa.ForeignKey("api_clients.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("name", sa.String(120), nullable=True),
            sa.Column("key_prefix", sa.String(24), nullable=False),
            sa.Column("key_hash", sa.String(64), nullable=False),
            sa.Column("scopes", sa.String(255), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rotated_from_id", UUID,
                      sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_ip", sa.String(64), nullable=True),
            sa.Column("use_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_api_keys_client_id", "api_keys", ["client_id"])
        op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])
        op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)

    if "analysis_requests" not in existing:
        status_enum = postgresql.ENUM(
            "QUEUED", "PROCESSING", "COMPLETED", "FAILED",
            name="b2b_request_status", create_type=False)
        status_enum.create(op.get_bind(), checkfirst=True)
        op.create_table(
            "analysis_requests",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("request_id", sa.String(64), nullable=False),
            sa.Column("client_id", UUID,
                      sa.ForeignKey("api_clients.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("api_key_id", UUID,
                      sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("idempotency_key", sa.String(128), nullable=True),
            sa.Column("status", status_enum, nullable=False),
            sa.Column("filename", sa.String(255), nullable=True),
            sa.Column("detected_format", sa.String(32), nullable=True),
            sa.Column("file_size_bytes", sa.Integer(), nullable=True),
            sa.Column("file_sha256", sa.String(64), nullable=True),
            sa.Column("country", sa.String(8), nullable=True),
            sa.Column("currency", sa.String(8), nullable=True),
            sa.Column("transaction_count", sa.Integer(), nullable=True),
            sa.Column("overall_confidence", sa.Float(), nullable=True),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("result_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("client_id", "idempotency_key",
                                name="uq_b2b_client_idempotency"),
        )
        op.create_index("ix_analysis_requests_request_id", "analysis_requests",
                        ["request_id"], unique=True)
        op.create_index("ix_analysis_requests_client_id", "analysis_requests",
                        ["client_id"])
        op.create_index("ix_analysis_requests_status", "analysis_requests",
                        ["status"])
        op.create_index("ix_analysis_requests_file_sha256", "analysis_requests",
                        ["file_sha256"])
        op.create_index("ix_analysis_requests_result_expires_at",
                        "analysis_requests", ["result_expires_at"])
        op.create_index("ix_analysis_requests_created_at", "analysis_requests",
                        ["created_at"])
        op.create_index("ix_b2b_req_client_created", "analysis_requests",
                        ["client_id", "created_at"])

    if "api_usage_records" not in existing:
        op.create_table(
            "api_usage_records",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("client_id", UUID,
                      sa.ForeignKey("api_clients.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("api_key_id", UUID, nullable=True),
            sa.Column("request_id", sa.String(64), nullable=True),
            sa.Column("endpoint", sa.String(120), nullable=False),
            sa.Column("method", sa.String(10), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=False),
            sa.Column("succeeded", sa.Boolean(), nullable=False),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("file_processed", sa.Boolean(), nullable=False),
            sa.Column("file_size_bytes", sa.Integer(), nullable=True),
            sa.Column("detected_format", sa.String(32), nullable=True),
            sa.Column("transaction_count", sa.Integer(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_api_usage_records_client_id", "api_usage_records",
                        ["client_id"])
        op.create_index("ix_api_usage_records_request_id", "api_usage_records",
                        ["request_id"])
        op.create_index("ix_api_usage_records_occurred_at", "api_usage_records",
                        ["occurred_at"])
        op.create_index("ix_b2b_usage_client_time", "api_usage_records",
                        ["client_id", "occurred_at"])

    if "api_webhook_deliveries" not in existing:
        op.create_table(
            "api_webhook_deliveries",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("event_id", sa.String(64), nullable=False),
            sa.Column("client_id", UUID,
                      sa.ForeignKey("api_clients.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("request_id", sa.String(64), nullable=True),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("url", sa.String(500), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("delivered", sa.Boolean(), nullable=False),
            sa.Column("last_status_code", sa.Integer(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_api_webhook_deliveries_event_id",
                        "api_webhook_deliveries", ["event_id"], unique=True)
        op.create_index("ix_api_webhook_deliveries_client_id",
                        "api_webhook_deliveries", ["client_id"])
        op.create_index("ix_api_webhook_deliveries_request_id",
                        "api_webhook_deliveries", ["request_id"])


def downgrade() -> None:
    existing = _tables()
    for table in ("api_webhook_deliveries", "api_usage_records",
                  "analysis_requests", "api_keys", "api_clients"):
        if table in existing:
            op.drop_table(table)
    postgresql.ENUM(name="b2b_request_status").drop(op.get_bind(), checkfirst=True)
