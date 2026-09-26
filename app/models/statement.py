import enum
import uuid
from sqlalchemy import (
    Column, String, Date, DateTime, Boolean, ForeignKey, func, Index, BigInteger, Integer,
    Enum as SQLEnum, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base


class SourceChannel(str, enum.Enum):
    """Which delivery channel produced this statement file."""
    UPLOAD = "upload"
    GMAIL  = "gmail"
    RPA    = "rpa"
    AA     = "aa"   # Account Aggregator


class Statement(Base):
    __tablename__ = "statements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)

    # ---- Ownership (denormalised for isolation and indexing) -----------------
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    account_id = Column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    source_channel = Column(String, nullable=True)

    # ---- Link back to the UploadedFile record (set on manual upload) ---------
    uploaded_file_id = Column(
        UUID(as_uuid=True), ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # ---- Content‑addressed storage ------------------------------------------
    file_sha256  = Column(String(64), nullable=True, index=True)
    storage_path = Column(String, nullable=True)
    original_filename = Column(String, nullable=True)

    # ---- Date range ----------------------------------------------------------
    period_from = Column(Date, nullable=True)
    period_to   = Column(Date, nullable=True)

    # ---- Balance figures (BigInteger paise) ----------------------------------
    opening_balance_paise = Column(BigInteger, nullable=True)
    closing_balance_paise = Column(BigInteger, nullable=True)

    # ---- Reconciliation flags ------------------------------------------------
    # server_default must be a genuine boolean literal: PostgreSQL rejects
    # `DEFAULT 0` on a boolean column (SQLite silently accepted it).
    reconciled = Column(Boolean, nullable=False, server_default=text("false"))
    reconciliation_note = Column(String, nullable=True)

    # ---- Lifecycle -----------------------------------------------------------
    uploaded_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    status = Column(String, nullable=False, server_default="pending")

    # ---- Relationships -------------------------------------------------------
    user = relationship("User", back_populates="statements")
    account = relationship("Account", back_populates="statements")
    transactions = relationship(
        "Transaction", back_populates="statement", cascade="all, delete-orphan",
    )

    # ---- Composite indexes ---------------------------------------------------
    __table_args__ = (
        Index("ix_statement_user_period", "user_id", "period_from", "period_to"),
        # Uniqueness is scoped per user: the same file bytes may be imported by
        # different users without violating integrity.
        Index("uq_statement_user_sha256", "user_id", "file_sha256", unique=True),
    )
