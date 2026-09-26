"""Add `kind` to counterparty_memory: is this a trading party, or a charge type?

Revision ID: 009
Revises: 008
Create Date: 2026-08-18

WHY THIS IS A SEPARATE REVISION AND NOT AN EDIT TO 008.

`kind` was originally added by editing migration 008 in place. That does not
work, and the failure is silent in the worst way: alembic records a revision in
`alembic_version` once it has run and never inspects that file again. A database
that had already applied 008 therefore never saw the new column, `alembic
upgrade head` reported "nothing to do", and the mismatch only surfaced later as

    psycopg2.errors.UndefinedColumn: column counterparty_memory.kind does not exist

from inside an endpoint, in production, as a 500.

An applied migration is history. Every subsequent schema change gets its own
revision, however small, so alembic has something new to run.

WHAT `kind` MEANS
    'counterparty' — a party the user trades with, keyed on their name
    'pattern'      — a shape of narration with no counterparty at all (a bank
                     charge, POS terminal rent, interest collected), keyed on
                     the narration with reference numbers and dates stripped

Existing rows are all counterparties: nothing else could have been stored before
shape grouping existed. That is why the backfill is a plain server_default
rather than a data migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "counterparty_memory" not in set(inspector.get_table_names()):
        # 008 creates it, including this column via the model. Nothing to do.
        return

    columns = {c["name"] for c in inspector.get_columns("counterparty_memory")}

    # Also repairs a database that ran an early build of 008 in which the column
    # was still called `purpose`. Guarded so it is a no-op on a correct schema.
    if "purpose" in columns and "category" not in columns:
        op.alter_column("counterparty_memory", "purpose",
                        new_column_name="category")

    if "kind" not in columns:
        op.add_column(
            "counterparty_memory",
            sa.Column("kind", sa.String(16), nullable=False,
                      server_default="counterparty"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "counterparty_memory" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("counterparty_memory")}
    if "kind" in columns:
        op.drop_column("counterparty_memory", "kind")
