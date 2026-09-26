"""Add missing classification and detected_account_number columns to email_attachments.

Revision ID: 002
Revises: 001
Create Date: 2026-08-11
"""

from alembic import op
import sqlalchemy as sa

# Revision identifiers
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add missing classification and detected_account_number columns to email_attachments table using batch_alter_table
    with op.batch_alter_table("email_attachments") as batch_op:
        batch_op.add_column(
            sa.Column(
                "classification",
                sa.String(length=50),
                nullable=True,
                server_default="BANK_STATEMENT_CONFIRMED"
            )
        )
        batch_op.add_column(
            sa.Column(
                "detected_account_number",
                sa.String(length=100),
                nullable=True
            )
        )

    # Populate classification from classification_code if present in existing rows
    op.execute(
        "UPDATE email_attachments SET classification = COALESCE(classification_code, 'BANK_STATEMENT_CONFIRMED') WHERE classification IS NULL;"
    )
    # Populate detected_account_number from account_number_masked if present in existing rows
    op.execute(
        "UPDATE email_attachments SET detected_account_number = account_number_masked WHERE detected_account_number IS NULL AND account_number_masked IS NOT NULL;"
    )


def downgrade() -> None:
    with op.batch_alter_table("email_attachments") as batch_op:
        batch_op.drop_column("detected_account_number")
        batch_op.drop_column("classification")
