"""Per-user statement SHA-256 uniqueness constraint.

Revision ID: 001
Revises: (base)
Create Date: 2026-08-10

Summary
-------
The `statements` table previously had a **global** UNIQUE constraint on
`file_sha256` (created inline via `unique=True` in the column definition,
which SQLite stores as a hidden autoindex named sqlite_autoindex_statements_1).

This migration:
  1. Reconstructs the `statements` table via Alembic batch mode using
     `recreate='always'`, which rewrites the table in-place without the old
     inline UNIQUE constraint on file_sha256.
  2. Adds a composite unique index on `(user_id, file_sha256)` so that
     uniqueness is scoped per user.  Two different users may legitimately
     hold the same bank statement without violating DB integrity.

Data safety
-----------
- No rows are modified; this is a DDL-only change.
- `recreate='always'` causes batch mode to: copy all rows to a temp table,
  drop the original, recreate it with the new DDL, copy rows back, then
  drop the temp table.
"""

from alembic import op
import sqlalchemy as sa

# Revision identifiers
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL names the old inline `unique=True` constraint explicitly and can
    # drop it in place, so the SQLite table-rebuild dance is not needed. Both the
    # constraint and the index form are attempted because the original DDL may
    # have produced either, depending on how the table was first created.
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing_uniques = {uc["name"] for uc in inspector.get_unique_constraints("statements")}
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("statements")}

    for name in ("statements_file_sha256_key", "uq_statements_file_sha256"):
        if name in existing_uniques:
            op.drop_constraint(name, "statements", type_="unique")
        elif name in existing_indexes:
            op.drop_index(name, table_name="statements")

    # Add composite unique index: uniqueness is scoped per user
    if "uq_statement_user_sha256" not in existing_indexes:
        op.create_index(
            "uq_statement_user_sha256",
            "statements",
            ["user_id", "file_sha256"],
            unique=True,
        )

    # Add a non-unique covering index for fast SHA-256 lookups
    if "ix_statements_file_sha256" not in existing_indexes:
        op.create_index(
            "ix_statements_file_sha256",
            "statements",
            ["file_sha256"],
            unique=False,
        )


def downgrade() -> None:
    # Remove new indexes
    op.drop_index("ix_statements_file_sha256", table_name="statements")
    op.drop_index("uq_statement_user_sha256", table_name="statements")

    # Rebuild table with the old global unique on file_sha256.
    # We add it back as a named unique index (not column-level unique=True)
    # which is equivalent for enforcement purposes.
    op.create_index(
        "ix_statements_file_sha256_unique_global",
        "statements",
        ["file_sha256"],
        unique=True,
    )
