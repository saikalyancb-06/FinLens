"""BRS: real book opening balance and carry-forward of outstanding items.

* import_batches.book_opening_paise becomes NULLABLE (NULL = not supplied; the
  old NOT NULL DEFAULT 0 made "zero" and "missing" indistinguishable, and no
  code path ever wrote it, so every existing value is the default 0 -> NULL),
  plus import_batches.book_opening_source.
* reconciliation_runs.book_opening_source and carried_from_run_id.

Idempotent like 014: each step checks the live schema first.

Revision ID: 015
Revises: 014
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def _columns(table: str) -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    cols = _columns("import_batches")
    op.alter_column("import_batches", "book_opening_paise", existing_type=sa.BigInteger(), nullable=True)
    if "book_opening_source" not in cols:
        op.add_column("import_batches", sa.Column("book_opening_source", sa.String(20), nullable=True))
        # Nothing ever wrote an opening balance before this revision.
        op.execute("UPDATE import_batches SET book_opening_paise = NULL WHERE book_opening_source IS NULL")

    cols = _columns("reconciliation_runs")
    if "book_opening_source" not in cols:
        op.add_column("reconciliation_runs", sa.Column("book_opening_source", sa.String(20), nullable=True))
    if "carried_from_run_id" not in cols:
        op.add_column("reconciliation_runs", sa.Column(
            "carried_from_run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reconciliation_runs.id", ondelete="SET NULL"), nullable=True))


def downgrade() -> None:
    op.drop_column("reconciliation_runs", "carried_from_run_id")
    op.drop_column("reconciliation_runs", "book_opening_source")
    op.drop_column("import_batches", "book_opening_source")
    op.execute("UPDATE import_batches SET book_opening_paise = 0 WHERE book_opening_paise IS NULL")
    op.alter_column("import_batches", "book_opening_paise", existing_type=sa.BigInteger(), nullable=False)
