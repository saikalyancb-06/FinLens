import enum
import uuid
from sqlalchemy import (
    Column, String, Float, ForeignKey, DateTime, func,
    Enum as SQLEnum, Index, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base


class DuplicateTier(str, enum.Enum):
    """Which matching tier produced this pair."""
    TIER_1 = "tier_1"   # reference_no + account_id + direction + amount_paise
    TIER_2 = "tier_2"   # amount + direction + date window + narration similarity


class MatchStatus(str, enum.Enum):
    """
    Lifecycle of a duplicate‑match record.

    Transitions:
        auto_merged    -- matcher merged immediately (Tier‑1, or Tier‑2 ≥ 0.85,
                         single candidate).  No human action needed.
        pending_review -- matcher is unsure (Tier‑2 0.60–0.85, or multiple
                         candidates).  Awaits human decision.
        confirmed      -- a reviewer confirmed this is a real duplicate.
                         kept_txn_id is the canonical row.
        rejected       -- a reviewer said these are distinct transactions.
                         superseded_by_id reset to NULL on duplicate_txn_id.

    No 'no_match' value: a non‑matching pair does not create a record.
    """
    AUTO_MERGED    = "auto_merged"
    PENDING_REVIEW = "pending_review"
    CONFIRMED      = "confirmed"
    REJECTED       = "rejected"


class DuplicateMatch(Base):
    """
    One row per (duplicate_txn_id, kept_txn_id) candidate pair evaluated by
    the deduplication service.

    duplicate_txn_id  -- the row that lost the match (superseded_by_id is set
                         on the Transaction row when status is auto_merged or
                         confirmed).
    kept_txn_id       -- the canonical transaction that survives aggregation.

    Nothing is deleted. Losers are flagged and excluded by aggregates that
    filter on Transaction.superseded_by_id IS NULL.
    """
    __tablename__ = "duplicate_matches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    # Ownership – every table carries user_id for isolation
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    duplicate_txn_id = Column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    kept_txn_id = Column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    tier = Column(SQLEnum(DuplicateTier, name="duplicate_tier", values_callable=lambda x: [e.value for e in x]), nullable=False)
    confidence = Column(Float, nullable=False)
    status = Column(SQLEnum(MatchStatus, name="match_status", values_callable=lambda x: [e.value for e in x]), nullable=False)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    # ---- Relationships -------------------------------------------------------
    duplicate_txn = relationship(
        "Transaction",
        foreign_keys=[duplicate_txn_id],
        backref="outgoing_matches",
    )
    kept_txn = relationship(
        "Transaction",
        foreign_keys=[kept_txn_id],
        backref="incoming_matches",
    )

    # ---- Constraints / indexes ------------------------------------------------
    __table_args__ = (
        UniqueConstraint("duplicate_txn_id", "kept_txn_id", name="uq_duplicate_pair"),
        Index("ix_dup_user_pair", "user_id", "duplicate_txn_id", "kept_txn_id"),
    )
