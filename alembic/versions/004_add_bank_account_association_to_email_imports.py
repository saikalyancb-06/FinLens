"""Add bank account association columns and indexes to email_attachments and import_history.

Revision ID: 004
Revises: 003
Create Date: 2026-08-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# Revision identifiers
revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Ensure account_id exists on email_attachments
    email_att_cols = [c["name"] for c in inspector.get_columns("email_attachments")]
    if "account_id" not in email_att_cols:
        with op.batch_alter_table("email_attachments") as batch_op:
            batch_op.add_column(
                sa.Column("account_id", UUID(as_uuid=True), nullable=True)
            )

    # 2. Add account_id to import_history table if missing
    import_hist_cols = [c["name"] for c in inspector.get_columns("import_history")]
    if "account_id" not in import_hist_cols:
        with op.batch_alter_table("import_history") as batch_op:
            batch_op.add_column(
                sa.Column("account_id", UUID(as_uuid=True), nullable=True)
            )


def downgrade() -> None:
    with op.batch_alter_table("import_history") as batch_op:
        try:
            batch_op.drop_column("account_id")
        except Exception:
            pass
