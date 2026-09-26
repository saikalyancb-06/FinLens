"""What the user has already decided about a counterparty.

The classifier can read a narration's *shape* — that "NEFT-HDFCH...-" is a bank
transfer, that "Int.Coll" is interest. What it cannot read is what the other
party sells. Only the account holder knows that KUMAR FISH is a food supplier
and SHOBHA G RENT is a landlord.

This table is where that knowledge is kept once it has been given. A single
manual categorisation of one KUMAR FISH row teaches every other KUMAR FISH row,
past and future. On the statement this was built against, 599 unresolved
transactions collapsed to 181 counterparties — and the top 100 covered 87% of
them, so the user's decisions compound quickly.

Scoped per user, NOT global. "SHETTY" is a supplier to one account holder and a
personal contact to another; a shared table would leak one business's ledger
structure into another's.
"""

import uuid

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.database.session import Base


class CounterpartyMemory(Base):
    """A learned mapping from a normalised counterparty name to a category."""

    __tablename__ = "counterparty_memory"
    __table_args__ = (
        UniqueConstraint("user_id", "counterparty_key",
                         name="uq_counterparty_memory_user_key"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True),
                     ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)

    # Exact normalised key from counterparty.extract(). Matching is exact —
    # fuzzy_key exists only to SUGGEST a merge to the user, never to auto-apply
    # a category across two names the user has not said are the same party.
    counterparty_key = Column(String(160), nullable=False)
    fuzzy_key = Column(String(24), nullable=True, index=True)

    # Shown in the UI. The key is stripped of punctuation and suffixes and reads
    # badly; this keeps the name the user actually recognises.
    display_name = Column(String(200), nullable=False)

    # 'counterparty' — a party the user trades with, keyed on their name.
    # 'pattern'      — a SHAPE of narration with no counterparty at all (a bank
    #                  charge, POS terminal rent, interest collected), keyed on
    #                  the narration with its reference numbers stripped.
    # Both are decisions the user made and both must carry forward, but they are
    # different kinds of claim and the UI labels them differently.
    kind = Column(String(16), nullable=False, server_default="counterparty")

    category = Column(String(80), nullable=False)
    event_type = Column(String(80), nullable=True)

    # How many times the user has independently confirmed this mapping. One
    # confirmation is enough to apply it; the count is what lets the UI show
    # "learned from 4 of your decisions" and what breaks ties if a future
    # version ever imports mappings from more than one place.
    times_confirmed = Column(Integer, nullable=False, server_default="1")

    # 'manual' when the user set it directly, 'bulk' when it came from applying
    # one decision across similar rows. Never written by the classifier itself —
    # this table only ever holds human decisions.
    source = Column(String(16), nullable=False, server_default="manual")

    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)
    last_applied_at = Column(DateTime(timezone=True), nullable=True)
