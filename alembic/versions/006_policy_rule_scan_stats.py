"""Cache the last scan's per-rule stats on policy_rules.

Revision ID: 006
Revises: 005
Create Date: 2026-08-17

The dashboard needs a compliance percentage on every page load. Recomputing it
meant a GET re-evaluated every rule against every transaction and wrote
violation rows as a side effect. Storing what the last scan found turns that
into a plain SELECT.
"""
from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None

_COLUMNS = {
    "last_applicable": sa.Column("last_applicable", sa.Integer(), nullable=True),
    "last_violations": sa.Column("last_violations", sa.Integer(), nullable=True),
    "last_evaluated_at": sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
}


def upgrade() -> None:
    # Guarded: DB_AUTO_CREATE=true in development may already have built the
    # table from the current models before this migration runs.
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("policy_rules")}
    for name, column in _COLUMNS.items():
        if name not in existing:
            op.add_column("policy_rules", column)


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("policy_rules")}
    for name in _COLUMNS:
        if name in existing:
            op.drop_column("policy_rules", name)
