import enum
import uuid
from sqlalchemy import (
    Column, String, Date, DateTime, Boolean, Text, ForeignKey, func, BigInteger, Integer,
    Enum as SQLEnum, Index, CheckConstraint, Numeric,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base


class Direction(str, enum.Enum):
    """Money movement direction from the account holder's perspective."""
    DEBIT  = "debit"   # money out
    CREDIT = "credit"  # money in


# Backward-compat alias – existing code imported DebitCreditEnum from here
DebitCreditEnum = Direction


class ReviewStatusEnum(str, enum.Enum):
    """Review status for a transaction (used by legacy schemas layer)."""
    UNREVIEWED = "unreviewed"
    REVIEWED   = "reviewed"
    FLAGGED    = "flagged"


class SourceType(str, enum.Enum):
    """Where this transaction row originated."""
    STATEMENT   = "statement"
    EMAIL_ALERT = "email_alert"
    SMS         = "sms"


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    # ---- Ownership (denormalised for isolation and per‑user indexing) --------
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    account_id = Column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    entity_id = Column(
        UUID(as_uuid=True), ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    statement_id = Column(
        UUID(as_uuid=True), ForeignKey("statements.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    source_channel = Column(String, nullable=True, index=True)


    # ---- Direction ------------------------------------------------------------
    direction = Column(SQLEnum(Direction, name="transaction_direction",
                               values_callable=lambda x: [e.value for e in x]),
                       nullable=False)

    # ---- Money (BigInteger paise) --------------------------------------------
    # A row carries its amount on exactly one side. The unused side is written as
    # either NULL or 0 depending on the ingestion path, and every reader keys off
    # `direction`, so both spellings are accepted. What is never valid is real
    # money on both sides at once — enforced by ck_txn_single_direction_amount.
    debit_paise  = Column(BigInteger, nullable=True)
    credit_paise = Column(BigInteger, nullable=True)

    # ---- Currency ------------------------------------------------------------
    # `booked_currency` is the currency the three `*_paise` columns above are
    # actually denominated in - the account's own currency, INR for every row
    # this system has ingested so far. The column names say "paise" for
    # historical reasons; read them as "minor units of booked_currency".
    #
    # The `original_*` trio records what the bank advice said before conversion.
    # A cross-border payment debits INR from an INR account but the advice reads
    # "USD 13,079.09 @ 88.71". Keeping the foreign leg means the Transactions tab
    # can show the true contracted figure instead of re-deriving USD from INR at
    # today's rate and producing a number that was never on any document.
    booked_currency = Column(String(3), nullable=False, server_default="INR")
    original_currency = Column(String(3), nullable=True, index=True)
    original_amount_minor = Column(BigInteger, nullable=True)
    fx_rate = Column(Numeric(20, 8), nullable=True)   # booked per 1 original

    # ---- Raw narration (verbatim source text) ---------------------------------
    narration_raw = Column(Text, nullable=False, server_default="")
    # ---- Cleaned narration ----------------------------------------------------
    narration_clean = Column(Text, nullable=True)
    payment_method  = Column(String, nullable=True)   # UPI, NEFT, RTGS, IMPS, CHQ …
    counterparty    = Column(String, nullable=True)
    category_id     = Column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ---- Reference number ----------------------------------------------------
    reference_no = Column(String, nullable=True, index=True)

    # ---- Category ------------------------------------------------------------
    # `category` is level 1 of the tree in app/categorization/hierarchy.py, and
    # it is the axis reports and the dashboard group by. It was called `purpose`
    # until the taxonomy became hierarchical; the column now carries the same
    # role under the name everything else already used.
    #
    # `category_path` is the full path — "Food & Dining > Restaurants > Fast
    # Food" — and it is where the hierarchy lives. The drill-down reads this
    # column and nothing else.
    #
    # `category_id` is NOT the tree link. It points at the flat category row,
    # because that is what the review queue, the prediction row and the reports
    # resolve a category NAME through. Pointing it at a leaf node was tried and
    # reverted: it made the review queue report a Swiggy row as "Coffee", and an
    # unclassifiable row as "Other / Uncategorized" instead of the sentinel that
    # means "no decision was made".
    #
    # Depth VARIES BY ROW and that is not a defect. `Transfers > Own Account
    # Transfer` is a complete answer at two levels, `Other / Uncategorized` at
    # one. Nothing may pad a path to a fixed width.
    category = Column(String(80), nullable=True, index=True)
    category_path = Column(String(500), nullable=True, index=True)

    # The pre-hierarchy label — "Cost of Goods", "Sales Income", "Bank Fees" —
    # kept because the review queue's vocabulary, the saved counterparty
    # decisions and the P&L reports were all written against those exact
    # strings. This is the column that used to be called `purpose`; the rename
    # freed `category` for the tree's level 1 without discarding a value the
    # rest of the system still reads. Transitional by intent: once nothing
    # reads it, it goes.
    legacy_category = Column(String(60), nullable=True, index=True)

    # How sure the classifier was about `category_path`, 0..1. Specifically
    # about the category: certainty that a payment travelled by UPI says nothing
    # about certainty over what it bought, so the rail's own confidence is not
    # folded in here.
    category_confidence = Column(Numeric(4, 3), nullable=True)

    # What kind of business event this was (optional — a bank fee has a category
    # but no commercial counterparty).
    event_type = Column(String(60), nullable=True, index=True)

    # ---- Flow and rail -------------------------------------------------------
    # INFLOW / OUTFLOW / TRANSFER / REVERSAL. The distinction that earns its
    # keep is TRANSFER: a credit is not income, and counting an own-account
    # transfer as revenue overstates the business by its full amount.
    flow_type = Column(String(12), nullable=True, index=True)

    # UPI, NEFT, IMPS, RTGS, ECS, ACH, Card, ATM, Cash, Cheque, ... Kept out of
    # the category deliberately: naming the rail as a category is what produced
    # a single "NEFT Transfer" bucket holding 853 transactions across 43
    # unrelated counterparties. `payment_method` above is the parser's older
    # free-text reading of the same thing and is left alone.
    transaction_method = Column(String(20), nullable=True, index=True)

    # Who was paid, as distinct from what for. Amazon is a merchant, not a
    # category; keeping them in separate columns is what stops "merchant =
    # Amazon" from becoming "category = Electronics".
    merchant = Column(String(160), nullable=True, index=True)

    # ---- Source discriminator ------------------------------------------------
    source_type = Column(
        SQLEnum(SourceType, name="transaction_source_type",
                 values_callable=lambda x: [e.value for e in x]),
        nullable=False, server_default=SourceType.STATEMENT.value,
    )

    # ---- Deduplication fields ------------------------------------------------
    hash = Column(String(64), nullable=True, index=True)
    superseded_by_id = Column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # No separate is_duplicate flag – exclusion is done by checking superseded_by_id IS NULL

    # ---- Balance --------------------------------------------------------------
    balance_paise = Column(BigInteger, nullable=True)

    # ---- Date fields ----------------------------------------------------------
    txn_date   = Column(Date, nullable=False, index=True)   # posting date
    value_date = Column(Date, nullable=True,  index=True)   # effective date

    # Position in source file – nullable for email alerts
    row_index = Column(Integer, nullable=True)

    # ---- Timestamps ----------------------------------------------------------
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


    # ---- Relationships -------------------------------------------------------
    user      = relationship("User", back_populates="transactions")
    account   = relationship("Account", back_populates="transactions")
    entity    = relationship("Entity")
    statement = relationship("Statement", back_populates="transactions")

    # Named `category_node` because `category` is now the level-1 name above.
    category_node = relationship("Category", back_populates="transactions")
    prediction = relationship("Prediction", back_populates="transaction", uselist=False)
    superseded_by = relationship(
        "Transaction",
        foreign_keys=[superseded_by_id],
        remote_side="Transaction.id",
        backref="superseded_transactions",
        uselist=False,
    )

    # ---- Composite indexes ----------------------------------------------------
    __table_args__ = (
        # A transaction cannot be both an inflow and an outflow. Enforced at the
        # database so no ingestion path can write a self-contradictory row.
        # NOTE: applies to tables created after this change; existing databases
        # need a migration to pick it up.
        CheckConstraint(
            "NOT (COALESCE(debit_paise, 0) > 0 AND COALESCE(credit_paise, 0) > 0)",
            name="ck_txn_single_direction_amount",
        ),
        # Fast lookup for deduplication scans
        Index("ix_txn_user_date", "user_id", "txn_date"),
        Index("ix_txn_account_date", "account_id", "txn_date"),
        Index(
            "ix_txn_user_account_date_amount",
            "user_id", "account_id", "txn_date", "debit_paise", "credit_paise",
        ),
        # Drill-down reads one level at a time within one user, so both the
        # level-1 filter and the prefix match on the full path are per-user.
        Index("ix_txn_user_category", "user_id", "category"),
        Index("ix_txn_user_category_path", "user_id", "category_path"),
    )
