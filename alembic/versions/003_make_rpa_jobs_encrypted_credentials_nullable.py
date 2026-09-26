"""Make rpa_jobs.encrypted_credentials nullable for memory-only RPA credential design.

Revision ID: 003
Revises: 002
Create Date: 2026-08-11
"""

from alembic import op
import sqlalchemy as sa

# Revision identifiers
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("rpa_jobs") as batch_op:
        batch_op.alter_column(
            "encrypted_credentials",
            existing_type=sa.LargeBinary(),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("rpa_jobs") as batch_op:
        batch_op.alter_column(
            "encrypted_credentials",
            existing_type=sa.LargeBinary(),
            nullable=False,
        )
