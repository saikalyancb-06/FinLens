"""oauth_states: record the outcome of each mailbox sign-in.

The page that opened the Google / Microsoft sign-in window polls
``GET /email/oauth/result?state=...`` for this, instead of depending on the
popup reporting back.

Idempotent like 014/015.

Revision ID: 016
Revises: 015
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None

NEW = [
    ("result_status", sa.String(20)),
    ("result_detail", sa.String(1000)),
    ("result_email", sa.String(255)),
    ("connection_id", postgresql.UUID(as_uuid=True)),
    ("scan_id", postgresql.UUID(as_uuid=True)),
]


def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("oauth_states")}
    for name, type_ in NEW:
        if name not in have:
            op.add_column("oauth_states", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("oauth_states")}
    for name, _ in reversed(NEW):
        if name in have:
            op.drop_column("oauth_states", name)
