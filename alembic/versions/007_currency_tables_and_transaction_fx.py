"""Currency reference tables and the FX columns on transactions.

Revision ID: 007
Revises: 006
Create Date: 2026-08-18

Adds `currencies` + `currency_rates`, and four columns on `transactions` that
record what a cross-border row looked like before conversion.

`booked_currency` backfills to 'INR' and is NOT NULL, which is safe because every
row already in this database was posted by an Indian bank in rupees. The other
three stay nullable: a domestic UPI transfer has no foreign leg, and writing
'INR'/rate 1.0 onto 36,000 domestic rows would make "has an FX leg" impossible to
query for.
"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


_TXN_COLUMNS = {
    "booked_currency": sa.Column("booked_currency", sa.String(3), nullable=False,
                                 server_default="INR"),
    "original_currency": sa.Column("original_currency", sa.String(3), nullable=True),
    "original_amount_minor": sa.Column("original_amount_minor", sa.BigInteger(),
                                       nullable=True),
    "fx_rate": sa.Column("fx_rate", sa.Numeric(20, 8), nullable=True),
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # Guarded throughout: DB_AUTO_CREATE=true in development may already have
    # built these from the models before this migration runs.
    if "currencies" not in tables:
        op.create_table(
            "currencies",
            sa.Column("code", sa.String(3), primary_key=True),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("symbol", sa.String(8), nullable=False, server_default=""),
            sa.Column("decimals", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("display_order", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.text("now()"), nullable=False),
        )

    if "currency_rates" not in tables:
        op.create_table(
            "currency_rates",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("code", sa.String(3),
                      sa.ForeignKey("currencies.code", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("as_of", sa.Date(), nullable=False),
            sa.Column("inr_per_unit", sa.Numeric(20, 8), nullable=False),
            sa.Column("source", sa.String(32), nullable=False, server_default="seed"),
            sa.Column("note", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.text("now()"), nullable=False),
            sa.UniqueConstraint("code", "as_of", name="uq_currency_rate_code_asof"),
        )
        op.create_index("ix_currency_rates_code", "currency_rates", ["code"])
        op.create_index("ix_currency_rates_as_of", "currency_rates", ["as_of"])

    existing = {c["name"] for c in inspector.get_columns("transactions")}
    for name, column in _TXN_COLUMNS.items():
        if name not in existing:
            op.add_column("transactions", column)
    if "original_currency" not in existing:
        op.create_index("ix_transactions_original_currency", "transactions",
                        ["original_currency"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing = {c["name"] for c in inspector.get_columns("transactions")}
    indexes = {i["name"] for i in inspector.get_indexes("transactions")}
    if "ix_transactions_original_currency" in indexes:
        op.drop_index("ix_transactions_original_currency", table_name="transactions")
    for name in _TXN_COLUMNS:
        if name in existing:
            op.drop_column("transactions", name)

    tables = set(inspector.get_table_names())
    if "currency_rates" in tables:
        op.drop_table("currency_rates")
    if "currencies" in tables:
        op.drop_table("currencies")
