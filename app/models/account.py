import uuid
import datetime
from sqlalchemy import Column, String, DateTime, ForeignKey, func, Index, BigInteger
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base


class Account(Base):
    """Bank account owned by a single user.
    The masked account number is shown to the UI (e.g. ****1234).
    """
    __tablename__ = "accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    entity_id = Column(UUID(as_uuid=True), ForeignKey("entities.id", ondelete="SET NULL"),
                       nullable=True, index=True)
    bank_id = Column(UUID(as_uuid=True), ForeignKey("banks.id", ondelete="SET NULL"),
                     nullable=True, index=True)
    bank_code = Column(String, nullable=False)
    account_number_masked = Column(String, nullable=False, index=True)
    currency = Column(String(3), nullable=False, server_default="INR")
    created_at = Column(DateTime(timezone=True),
                        server_default=func.now(),
                        nullable=False)
    updated_at = Column(DateTime(timezone=True),
                        server_default=func.now(),
                        onupdate=func.now(),
                        nullable=False)
    # Soft‑delete marker – accounts are never hard‑deleted to preserve financial history
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    # Optional descriptive fields
    account_label = Column(String, nullable=True)
    account_type  = Column(String, nullable=True)
    min_balance_paise = Column(BigInteger, nullable=True)

    # Relationships
    user = relationship("User", back_populates="accounts")
    entity = relationship("Entity", back_populates="accounts")
    bank = relationship("Bank", back_populates="accounts")
    statements = relationship("Statement", back_populates="account")
    transactions = relationship("Transaction", back_populates="account")


# No delete‑orphan cascade: financial records are retained even if an account is soft‑deleted.
