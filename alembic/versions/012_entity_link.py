"""Store what a person decided about two names that looked like one party.

Revision ID: 012
Revises: 011
Create Date: 2026-08-19

The entity resolver merges what it can prove and asks about the rest. Both
halves of the answer have to persist or the asking never stops: a confirmed
merge must apply to every future statement, and — the half that is usually
missed — a REJECTED match must never be suggested again. Without the second,
the same medium-confidence pair resurfaces on every upload and the feature
reads as broken.

Idempotent: safe to run twice, and the downgrade drops only what it created.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None

TABLE = "entity_link"


def _exists() -> bool:
    return TABLE in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _exists():
        return
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key_a", sa.String(120), nullable=False),
        sa.Column("key_b", sa.String(120), nullable=False),
        sa.Column("same", sa.Boolean(), nullable=False),
        sa.Column("display_a", sa.String(200), nullable=True),
        sa.Column("display_b", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", "key_a", "key_b",
                            name="uq_entity_link_pair"),
    )
    op.create_index("ix_entity_link_user_id", TABLE, ["user_id"])
    op.create_index("ix_entity_link_user_keys", TABLE,
                    ["user_id", "key_a", "key_b"])


def downgrade() -> None:
    if _exists():
        op.drop_table(TABLE)
