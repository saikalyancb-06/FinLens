"""`Other / Uncategorized` stops being a category.

Revision ID: 011
Revises: 010
Create Date: 2026-08-19

WHY A MIGRATION AND NOT JUST A CODE CHANGE.

Deleting the node from `hierarchy.py` stops NEW rows landing there. It does
nothing for the ones already stamped with it — on the statement that prompted
this, that was better than a quarter of the ledger — and those rows would keep
rendering a branch the code no longer knows how to open. So the data has to
move too, and it has to move by the same rule the classifier now uses, or the
drill-down and the classifier will disagree about the same transaction.

THE RULE, WHICH INVENTS NOTHING.

"Uncategorized" was never a category. It was an admission, filed as though it
were an answer, and it could not be totalled, reconciled or drilled into. Every
row it held still states two facts: the rail the money took and the direction it
went. Those are printed on the statement.

    ATM, money out          ->  Cash > ATM Withdrawal
    ATM / cash, money in    ->  Cash > Cash Deposit
    cash, money out         ->  Cash > Other Cash Transaction
    card, money out         ->  Business & Professional > Vendor / Business Transaction
    anything else           ->  Transfers > External Transfer

None of these claims a purpose. `Cash > ATM Withdrawal` is simply what a row
reading `SELF 4471` says about itself. What remains unknown — what the cash was
spent on — is carried by `category_confidence`, which is left at or below the
review threshold so the row still surfaces in the Review Queue. That is a status
on the transaction, which is where it always belonged.

This mirrors `hierarchy.residual_path` exactly. If that function changes, change
the CASE below with it.

DOWNGRADE puts the node back and returns the rows to it, but only the rows this
migration moved — identified by their confidence being the residual 0.30 — so a
later human decision is not undone by rolling back.
"""
from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None

OLD_ROOT = "Other / Uncategorized"
OLD_SLUG = "other-uncategorized"

# Kept identical to the branches in `app/categorization/hierarchy.py`.
_CASE = """
    CASE
        WHEN transaction_method = 'ATM' AND COALESCE(credit_paise, 0) > 0
            THEN 'Cash > Cash Deposit'
        WHEN transaction_method = 'ATM'
            THEN 'Cash > ATM Withdrawal'
        WHEN transaction_method = 'Cash' AND COALESCE(credit_paise, 0) > 0
            THEN 'Cash > Cash Deposit'
        WHEN transaction_method = 'Cash'
            THEN 'Cash > Other Cash Transaction'
        WHEN transaction_method = 'Card' AND COALESCE(credit_paise, 0) = 0
            THEN 'Business & Professional > Vendor / Business Transaction'
        ELSE 'Transfers > External Transfer'
    END
"""


def _has(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Move the transactions. Idempotent: after this runs there are no rows
    #    left matching the WHERE, so a second run is a no-op.
    if _has("transactions", "category_path"):
        bind.execute(sa.text(f"""
            UPDATE transactions
               SET category_path = {_CASE},
                   category = split_part({_CASE}, ' > ', 1),
                   category_confidence = LEAST(COALESCE(category_confidence, 0.30), 0.30)
             WHERE category = :old OR category_path LIKE :prefix
        """), {"old": OLD_ROOT, "prefix": OLD_ROOT + "%"})

    # 2. Retire the tree node. Deleted rather than deactivated: leaving it with
    #    is_active = false would still let a stale picker offer it, and nothing
    #    references it — `transactions.category_id` is the FLAT link and never
    #    pointed here.
    if _has("categories", "slug"):
        bind.execute(
            sa.text("DELETE FROM categories WHERE slug = :slug AND "
                    "NOT EXISTS (SELECT 1 FROM transactions t "
                    "WHERE t.category_id = categories.id)"),
            {"slug": OLD_SLUG},
        )
        # If a transaction really does point at it through the flat link, the
        # row cannot be deleted without orphaning that FK. Park it instead, so
        # the migration never fails on real data.
        bind.execute(
            sa.text("UPDATE categories SET is_active = false WHERE slug = :slug"),
            {"slug": OLD_SLUG},
        )


def downgrade() -> None:
    bind = op.get_bind()

    if _has("categories", "slug"):
        bind.execute(sa.text("""
            INSERT INTO categories (name, slug, level, path, sort_order, is_active)
            SELECT :name, :slug, 1, :name, 999, true
             WHERE NOT EXISTS (SELECT 1 FROM categories WHERE slug = :slug)
        """), {"name": OLD_ROOT, "slug": OLD_SLUG})
        bind.execute(
            sa.text("UPDATE categories SET is_active = true WHERE slug = :slug"),
            {"slug": OLD_SLUG},
        )

    # Only the rows this migration placed, recognised by the residual
    # confidence. A row a person has since decided carries a higher number and
    # is left exactly where they put it.
    if _has("transactions", "category_path"):
        bind.execute(sa.text("""
            UPDATE transactions
               SET category = :old, category_path = :old
             WHERE category_confidence <= 0.30
               AND category_path IN (
                    'Cash > Cash Deposit', 'Cash > ATM Withdrawal',
                    'Cash > Other Cash Transaction',
                    'Business & Professional > Vendor / Business Transaction',
                    'Transfers > External Transfer')
        """), {"old": OLD_ROOT})
