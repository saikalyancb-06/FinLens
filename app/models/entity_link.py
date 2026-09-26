"""STAGE 15: what a person decided about two names that looked alike.

WHY BOTH ANSWERS ARE STORED. Saving "yes, same party" is obvious. Saving "no,
different" is the half that gets forgotten, and it is the one the user notices:
without it the resolver re-derives the same medium-confidence suggestion on
every upload and asks the same question forever. A rejected match is a fact
about the world, exactly like a confirmed one.

Keys are the entity resolver's COMPACT form, not the raw narration. A decision
made about `ABC Pvt Ltd` has to be found again from `NEFT-xxxx-ABC PRIVATE
LIMITED`, and storing the raw string would make this memory as fragile as the
problem it exists to solve.

The pair is stored ORDERED (smaller key first) with a uniqueness constraint, so
one decision cannot be recorded twice in opposite directions and then disagree
with itself.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Index, String,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import UUID

from app.database.session import Base


class EntityLink(Base):
    """One human answer to "are these the same party?"."""

    __tablename__ = "entity_link"
    __table_args__ = (
        UniqueConstraint("user_id", "key_a", "key_b", name="uq_entity_link_pair"),
        Index("ix_entity_link_user_keys", "user_id", "key_a", "key_b"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True),
                     ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)

    # Ordered by `store()` below so a pair has exactly one row.
    key_a = Column(String(120), nullable=False)
    key_b = Column(String(120), nullable=False)

    # True = the user said these are one party. False = the user said they are
    # not, and the resolver must stop suggesting it.
    same = Column(Boolean, nullable=False)

    # What was shown when the decision was made. Kept for the audit trail: a
    # user who later disagrees needs to see what they were actually asked.
    display_a = Column(String(200), nullable=True)
    display_b = Column(String(200), nullable=True)

    created_at = Column(DateTime(timezone=True),
                        default=lambda: datetime.now(timezone.utc),
                        nullable=False)

    @staticmethod
    def ordered(key_a: str, key_b: str):
        """Canonical ordering, so one decision is one row."""
        a, b = (key_a or "").strip().upper(), (key_b or "").strip().upper()
        return (a, b) if a <= b else (b, a)
