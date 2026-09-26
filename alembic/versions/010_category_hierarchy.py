"""The category becomes a tree, and `purpose` becomes `category`.

Revision ID: 010
Revises: 009
Create Date: 2026-08-19

TWO CHANGES, AND THE FIRST ONE IS THE AWKWARD ONE.

1. `categories.name` LOSES ITS UNIQUE CONSTRAINT.

   It has to. Once the table holds a tree, a name is only meaningful with its
   ancestors: `Interest` is a node under `Financial` and a different node under
   `Loans & Credit > Credit Card`. `Payment`, `Late Fee`, `Mobile` and every
   `Other …` repeat across branches too. Identity moves to `slug`, which
   carries the whole path — `financial/interest` versus
   `loans-and-credit/credit-card/interest` — and `slug` is unique instead.

   Existing rows keep their ids. The seeder upgrades a legacy row in place when
   its name matches a level-1 node, so every transaction already pointing at
   `Food & Dining` still points at the same row afterwards. That is the whole
   reason this is an ALTER and not a rebuild.

2. `transactions.purpose` IS RENAMED TO `transactions.legacy_category`, AND A
   NEW `transactions.category` IS ADDED.

   `category` holds the tree's level 1 and is the axis the drill-down and the
   new breakdowns use. The old vocabulary — "Cost of Goods", "Sales Income",
   "Bank Fees" — does not vanish: the review queue validates against it, saved
   counterparty decisions are stored in it, and the P&L reports group by it. A
   rename preserves those 1,800 rows of labelling; recreating the column would
   drop them.

   `category` is left EMPTY by this migration. Backfilling it in SQL would mean
   mapping the old vocabulary onto the tree with no narration to go on, which is
   exactly the guessing this taxonomy exists to avoid. Run the re-categorisation
   afterwards and the classifier fills it from the evidence.

   `Transaction.category` (the relationship to this table) is renamed to
   `category_node` in the model to make room. That is a Python-side change with
   no SQL.

Everything else is additive and nullable, so a database mid-deploy is readable
by both the old and the new code.

Idempotent throughout: every step checks the live schema first. That is not
belt-and-braces, it is the lesson from two incidents where a migration was
edited after it had already been applied and alembic then refused to re-run it.
"""
from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def _columns(inspector, table):
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ---- categories: tree columns -----------------------------------------
    if "categories" in tables:
        cols = _columns(inspector, "categories")

        if "slug" not in cols:
            op.add_column("categories", sa.Column("slug", sa.String(255), nullable=True))
            op.create_index("ix_categories_slug", "categories", ["slug"], unique=True)
        if "level" not in cols:
            op.add_column("categories", sa.Column("level", sa.Integer(), nullable=True))
            op.create_index("ix_categories_level", "categories", ["level"])
        if "path" not in cols:
            op.add_column("categories", sa.Column("path", sa.String(500), nullable=True))
        if "sort_order" not in cols:
            op.add_column("categories", sa.Column("sort_order", sa.Integer(), nullable=True))
        if "is_active" not in cols:
            op.add_column(
                "categories",
                sa.Column("is_active", sa.Boolean(), nullable=False,
                          server_default=sa.text("true")),
            )

        # Drop uniqueness on `name`, whatever the database happens to call the
        # constraint. It arrives as a UNIQUE INDEX when the table was built by
        # create_all and as a UNIQUE CONSTRAINT when built by a migration, and
        # the two are dropped by different statements.
        for uc in inspector.get_unique_constraints("categories"):
            if uc.get("column_names") == ["name"] and uc.get("name"):
                op.drop_constraint(uc["name"], "categories", type_="unique")
        for ix in inspector.get_indexes("categories"):
            if ix.get("unique") and ix.get("column_names") == ["name"] and ix.get("name"):
                op.drop_index(ix["name"], table_name="categories")

        existing_index_names = {ix["name"] for ix in inspector.get_indexes("categories")}
        if "ix_categories_name" not in existing_index_names:
            op.create_index("ix_categories_name", "categories", ["name"])
        if "ix_category_parent_sort" not in existing_index_names:
            op.create_index("ix_category_parent_sort", "categories",
                            ["parent_id", "sort_order"])

    # ---- transactions ------------------------------------------------------
    if "transactions" in tables:
        cols = _columns(inspector, "transactions")

        # `purpose` becomes `legacy_category` — same column, same 1,800 rows of
        # labelling, a name that says what it now is. `category` is then added
        # empty, for the tree's level 1, and backfilled by a re-categorisation
        # rather than by a guess in SQL: mapping the old vocabulary onto the
        # tree is the classifier's job and it wants the narration to do it well.
        if "purpose" in cols and "legacy_category" not in cols:
            op.alter_column("transactions", "purpose",
                            new_column_name="legacy_category",
                            existing_type=sa.String(60))
            cols = _columns(sa.inspect(bind), "transactions")
        elif "legacy_category" not in cols:
            op.add_column("transactions",
                          sa.Column("legacy_category", sa.String(60), nullable=True))
            cols.add("legacy_category")

        if "category" not in cols:
            op.add_column("transactions", sa.Column("category", sa.String(80), nullable=True))
            cols.add("category")

        if "category_path" not in cols:
            op.add_column("transactions", sa.Column("category_path", sa.String(500), nullable=True))
        if "category_confidence" not in cols:
            op.add_column("transactions",
                          sa.Column("category_confidence", sa.Numeric(4, 3), nullable=True))
        if "flow_type" not in cols:
            op.add_column("transactions", sa.Column("flow_type", sa.String(12), nullable=True))
        if "transaction_method" not in cols:
            op.add_column("transactions",
                          sa.Column("transaction_method", sa.String(20), nullable=True))
        if "merchant" not in cols:
            op.add_column("transactions", sa.Column("merchant", sa.String(160), nullable=True))

        existing = {ix["name"] for ix in sa.inspect(bind).get_indexes("transactions")}
        for name, columns in (
            ("ix_transactions_category", ["category"]),
            ("ix_transactions_legacy_category", ["legacy_category"]),
            ("ix_transactions_category_path", ["category_path"]),
            ("ix_transactions_flow_type", ["flow_type"]),
            ("ix_transactions_transaction_method", ["transaction_method"]),
            ("ix_transactions_merchant", ["merchant"]),
            ("ix_txn_user_category", ["user_id", "category"]),
            ("ix_txn_user_category_path", ["user_id", "category_path"]),
        ):
            if name not in existing:
                op.create_index(name, "transactions", columns)

        # The old index followed the old column name. Postgres keeps an index
        # working across a column rename but the name then lies about what it
        # covers, and the model declares the new one, so alembic autogenerate
        # would propose dropping and recreating it on every future revision.
        for ix in sa.inspect(bind).get_indexes("transactions"):
            if ix["name"] == "ix_transactions_purpose":
                op.drop_index("ix_transactions_purpose", table_name="transactions")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "transactions" in tables:
        cols = _columns(inspector, "transactions")
        existing = {ix["name"] for ix in inspector.get_indexes("transactions")}
        for name in ("ix_txn_user_category_path", "ix_txn_user_category",
                     "ix_transactions_legacy_category",
                     "ix_transactions_merchant", "ix_transactions_transaction_method",
                     "ix_transactions_flow_type", "ix_transactions_category_path",
                     "ix_transactions_category"):
            if name in existing:
                op.drop_index(name, table_name="transactions")
        for name in ("merchant", "transaction_method", "flow_type",
                     "category_confidence", "category_path", "category"):
            if name in cols:
                op.drop_column("transactions", name)
        if "legacy_category" in cols and "purpose" not in cols:
            op.alter_column("transactions", "legacy_category",
                            new_column_name="purpose", existing_type=sa.String(60))

    if "categories" in tables:
        cols = _columns(inspector, "categories")
        existing = {ix["name"] for ix in inspector.get_indexes("categories")}
        for name in ("ix_category_parent_sort", "ix_categories_level", "ix_categories_slug"):
            if name in existing:
                op.drop_index(name, table_name="categories")
        for name in ("is_active", "sort_order", "path", "level", "slug"):
            if name in cols:
                op.drop_column("categories", name)
        # `name` uniqueness is NOT restored: by now the table may legitimately
        # hold two `Interest` rows, and recreating the constraint would fail on
        # real data. Removing the tree rows is a data decision, not a schema one.
