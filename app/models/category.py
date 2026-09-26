import uuid

from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database.session import Base


class Category(Base):
    """One node of the category tree.

    The table was a flat list with a `parent_id` nobody set. It now holds the
    real tree (see `app/categorization/hierarchy.py`), which changes one thing
    about its identity: **`name` is no longer unique.**

    It cannot be. `Interest` is a node under `Financial` and a different node
    under `Loans & Credit > Credit Card`; `Payment`, `Late Fee`, `Mobile` and
    every `Other ...` repeat across branches too. A name is only meaningful with
    its ancestors, so identity moved to `slug`, which carries the whole path:

        financial/interest
        loans-and-credit/credit-card/interest

    Consequence for callers: a lookup by name alone can return more than one
    row and is only safe for level-1 nodes. Anything resolving a full path must
    go through `slug`.
    """

    __tablename__ = "categories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Display name of THIS node only - "Fast Food", not the whole path.
    name = Column(String(100), nullable=False, index=True)

    parent_id = Column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # ---- Tree position -------------------------------------------------------
    # Stable identity of the node, derived from its full path. Survives a change
    # of casing or punctuation in `name`, which is why the API and the stored
    # transaction reference this rather than the display name.
    slug = Column(String(255), nullable=True, unique=True, index=True)

    # 1 for a top-level category, 2 for its child, and so on. Denormalised from
    # the parent chain so "give me the top level" is an indexed filter instead
    # of a recursive query on every dashboard load.
    level = Column(Integer, nullable=True, index=True)

    # Human-readable full path, "Food & Dining > Restaurants > Fast Food".
    # A cache of the parent chain, kept for display and for the transaction's
    # denormalised copy. The parent links remain the source of truth.
    path = Column(String(500), nullable=True)

    # Ordering within a parent. Alphabetical ordering puts "Other Food" in the
    # middle of a list where it belongs at the end, and reorders the level-1
    # categories away from the sequence people expect.
    sort_order = Column(Integer, nullable=True)

    description = Column(String(255), nullable=True)
    is_custom = Column(Boolean, default=False)

    # NOTE on the levels that come from a narration - an employer under Salary,
    # a supplier under Inventory. Those do NOT get rows here. This table has no
    # user_id, so a row named after one user's supplier would appear in every
    # other user's category tree. They live only in Transaction.category_path,
    # which is user-scoped by construction, and the drill-down aggregates them
    # from there.
    #
    # Transaction.category_id does NOT point into the tree - it carries the flat
    # category, which is what existing readers resolve a name through. The tree
    # is reached through Transaction.category_path.

    is_active = Column(Boolean, nullable=False, default=True, server_default="true")

    # ---- Relationships -------------------------------------------------------
    parent = relationship("Category", remote_side=[id], backref="subcategories")
    predictions = relationship("Prediction", back_populates="category_rel")
    transactions = relationship("Transaction", back_populates="category_node")

    __table_args__ = (
        Index("ix_category_parent_sort", "parent_id", "sort_order"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Category {self.path or self.name!r}>"
