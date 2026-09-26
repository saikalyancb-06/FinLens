"""Counterparty memory: what the user has decided about each trading partner.

Revision ID: 008
Revises: 007
Create Date: 2026-08-18

Adds `counterparty_memory`. One row per (user, normalised counterparty name),
holding the category that user assigned to that party. The classifier reads it
before falling back to review, so a party categorised once never returns to the
queue.

Scoped by user_id and cascaded on user delete: this is a record of one account
holder's commercial relationships, and it should not outlive their account.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # Guarded: DB_AUTO_CREATE=true in development may already have built this
    # from the model before the migration runs.
    if "counterparty_memory" in tables:
        # An earlier build of this migration named the column `purpose`. If that
        # version already ran, rename in place rather than leaving a table the
        # model cannot read. A column rename is metadata-only in Postgres, so
        # this is safe on a table of any size.
        columns = {c["name"] for c in inspector.get_columns("counterparty_memory")}
        if "purpose" in columns and "category" not in columns:
            op.alter_column("counterparty_memory", "purpose", new_column_name="category")
        return

    op.create_table(
        "counterparty_memory",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("counterparty_key", sa.String(160), nullable=False),
        sa.Column("fuzzy_key", sa.String(24), nullable=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=True),
        sa.Column("times_confirmed", sa.Integer(), nullable=False,
                  server_default="1"),
        sa.Column("source", sa.String(16), nullable=False,
                  server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "counterparty_key",
                            name="uq_counterparty_memory_user_key"),
    )
    op.create_index("ix_counterparty_memory_user_id", "counterparty_memory",
                    ["user_id"])
    op.create_index("ix_counterparty_memory_fuzzy_key", "counterparty_memory",
                    ["fuzzy_key"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "counterparty_memory" in set(inspector.get_table_names()):
        op.drop_table("counterparty_memory")
