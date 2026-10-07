import uuid
import enum
from sqlalchemy import (
    Column, String, Integer, BigInteger, Boolean, DateTime, Date,
    ForeignKey, Float, JSON, CheckConstraint, Index, func,
    Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base


class ImportTemplate(Base):
    """Saved column mapping templates per user/client."""
    __tablename__ = "import_templates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    column_mapping_json = Column(JSON, nullable=False)
    date_format = Column(String, nullable=True, default="%Y-%m-%d")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User")


class ImportBatch(Base):
    """Import batch representing one uploaded book ledger file."""
    __tablename__ = "import_batches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String, nullable=False)
    file_sha256 = Column(String(64), nullable=False)
    template_id = Column(UUID(as_uuid=True), ForeignKey("import_templates.id", ondelete="SET NULL"), nullable=True)
    column_mapping_json = Column(JSON, nullable=False)
    row_count = Column(Integer, nullable=False, default=0)
    period_from = Column(Date, nullable=True)
    period_to = Column(Date, nullable=True)
    # Book balance at the start of the file, when known: typed by the user on
    # the import screen (source "manual") or read from the file's own
    # "Opening Balance" row (source "ledger_file"). NULL means not supplied —
    # zero is a real balance and must not be confused with "missing".
    book_opening_paise = Column(BigInteger, nullable=True)
    book_opening_source = Column(String(20), nullable=True)
    book_closing_paise = Column(BigInteger, nullable=False, default=0)
    imported_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User")
    account = relationship("Account")
    template = relationship("ImportTemplate")
    book_entries = relationship("BookEntry", back_populates="import_batch", cascade="all, delete-orphan")


class ReconciliationStatusEnum(str, enum.Enum):
    UNMATCHED = "unmatched"
    MATCHED = "matched"
    PENDING_REVIEW = "pending_review"
    WRITTEN_OFF = "written_off"


class BookEntry(Base):
    """Internal company ledger book entry parsed from Tally/ERP."""
    __tablename__ = "book_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    import_batch_id = Column(UUID(as_uuid=True), ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    
    entry_date = Column(Date, nullable=False)
    value_date = Column(Date, nullable=True)
    voucher_no = Column(String, nullable=True)
    voucher_type = Column(String, nullable=True)
    narration = Column(String, nullable=True)
    party_name = Column(String, nullable=True)
    ledger_name = Column(String, nullable=True)
    instrument_no = Column(String, nullable=True, index=True)
    instrument_date = Column(Date, nullable=True)
    
    # Strictly money_in vs money_out, never debit/credit
    money_in_paise = Column(BigInteger, nullable=False, default=0)
    money_out_paise = Column(BigInteger, nullable=False, default=0)
    
    row_index = Column(Integer, nullable=False)
    source_row_hash = Column(String(64), nullable=False)
    reconciliation_status = Column(String, nullable=False, default=ReconciliationStatusEnum.UNMATCHED.value)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User")
    account = relationship("Account")
    import_batch = relationship("ImportBatch", back_populates="book_entries")

    __table_args__ = (
        CheckConstraint(
            "((money_in_paise > 0 AND money_out_paise = 0) OR (money_in_paise = 0 AND money_out_paise > 0))",
            name="check_book_entry_single_direction"
        ),
        Index("idx_book_entries_user_account_date", "user_id", "account_id", "entry_date"),
        Index("uq_batch_row_hash", "import_batch_id", "source_row_hash", unique=True),
    )


class RunVerdictEnum(str, enum.Enum):
    RECONCILED_CLEAN = "reconciled_clean"
    RECONCILED_WITH_EXCEPTIONS = "reconciled_with_exceptions"
    UNRECONCILED = "unreconciled"


class ReconciliationRun(Base):
    """A reconciliation execution run for a given account and date range."""
    __tablename__ = "reconciliation_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    import_batch_id = Column(UUID(as_uuid=True), ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=True)
    
    period_from = Column(Date, nullable=False)
    period_to = Column(Date, nullable=False)
    
    book_opening_paise = Column(BigInteger, nullable=False, default=0)
    # Where book_opening_paise came from: manual | previous_run | ledger_file.
    book_opening_source = Column(String(20), nullable=True)
    # The run whose outstanding items were carried into this one.
    carried_from_run_id = Column(UUID(as_uuid=True), ForeignKey("reconciliation_runs.id", ondelete="SET NULL"), nullable=True)
    book_closing_paise = Column(BigInteger, nullable=False, default=0)
    bank_opening_paise = Column(BigInteger, nullable=False, default=0)
    bank_closing_paise = Column(BigInteger, nullable=False, default=0)
    computed_bank_closing_paise = Column(BigInteger, nullable=False, default=0)
    residual_paise = Column(BigInteger, nullable=False, default=0)
    
    verdict = Column(String, nullable=False, default=RunVerdictEnum.UNRECONCILED.value)
    forced = Column(Boolean, nullable=False, default=False)
    
    matched_count = Column(Integer, nullable=False, default=0)
    unmatched_bank_count = Column(Integer, nullable=False, default=0)
    unmatched_book_count = Column(Integer, nullable=False, default=0)
    pending_review_count = Column(Integer, nullable=False, default=0)
    
    status = Column(String, nullable=False, default="completed")
    engine_version = Column(String, nullable=False, server_default="v2.0")
    version = Column(Integer, nullable=False, default=1, server_default="1")
    supersedes_run_id = Column(UUID(as_uuid=True), ForeignKey("reconciliation_runs.id", ondelete="SET NULL"), nullable=True)
    debug_log_json = Column(JSON, nullable=True)
    is_archived = Column(Boolean, nullable=False, default=False, server_default="0")
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    account = relationship("Account")
    import_batch = relationship("ImportBatch")
    superseded_run = relationship("ReconciliationRun", remote_side=[id], foreign_keys=[supersedes_run_id])
    carried_from_run = relationship("ReconciliationRun", remote_side=[id], foreign_keys=[carried_from_run_id])
    matches = relationship("ReconciliationMatch", back_populates="run", cascade="all, delete-orphan")
    items = relationship("ReconciliationItem", back_populates="run", cascade="all, delete-orphan")


class MatchTierEnum(str, enum.Enum):
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"
    TIER_3_GROUP = "tier_3_group"
    TIER_4_NET_OF_CHARGES = "tier_4_net_of_charges"


class MatchStatusEnum(str, enum.Enum):
    AUTO_MATCHED = "auto_matched"
    PENDING_REVIEW = "pending_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ReconciliationMatch(Base):
    """Group match produced by the matching engine."""
    __tablename__ = "reconciliation_matches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    match_group_id = Column(UUID(as_uuid=True), default=uuid.uuid4, nullable=False, index=True)
    
    tier = Column(String, nullable=False)
    confidence = Column(Float, nullable=False, default=1.0)
    status = Column(String, nullable=False, default=MatchStatusEnum.AUTO_MATCHED.value)
    reason = Column(String, nullable=True)
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    run = relationship("ReconciliationRun", back_populates="matches")
    user = relationship("User", foreign_keys=[user_id])
    reviewer = relationship("User", foreign_keys=[reviewed_by])
    lines = relationship("ReconciliationMatchLine", back_populates="match", cascade="all, delete-orphan")


class ReconciliationMatchLine(Base):
    """Line item contained within a reconciliation match group."""
    __tablename__ = "reconciliation_match_lines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    match_id = Column(UUID(as_uuid=True), ForeignKey("reconciliation_matches.id", ondelete="CASCADE"), nullable=False, index=True)
    
    bank_txn_id = Column(UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True)
    book_entry_id = Column(UUID(as_uuid=True), ForeignKey("book_entries.id", ondelete="SET NULL"), nullable=True)

    match = relationship("ReconciliationMatch", back_populates="lines")
    bank_transaction = relationship("Transaction")
    book_entry = relationship("BookEntry")

    __table_args__ = (
        CheckConstraint(
            "((bank_txn_id IS NOT NULL AND book_entry_id IS NULL) OR (bank_txn_id IS NULL AND book_entry_id IS NOT NULL))",
            name="check_match_line_single_fk"
        ),
    )


class BRSSideEnum(str, enum.Enum):
    BANK = "bank"
    BOOK = "book"


class DirectionEnum(str, enum.Enum):
    ADD = "add"
    SUBTRACT = "subtract"


class ReconciliationItem(Base):
    """Classified unmatched item forming the Bank Reconciliation Statement (BRS) bridge."""
    __tablename__ = "reconciliation_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    
    side = Column(SQLEnum(BRSSideEnum, name="brs_side_enum", values_callable=lambda x: [e.value for e in x]), nullable=False)  # 'bank' or 'book'
    bank_txn_id = Column(UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True)
    book_entry_id = Column(UUID(as_uuid=True), ForeignKey("book_entries.id", ondelete="SET NULL"), nullable=True)
    
    brs_category = Column(String, nullable=False)
    amount_paise = Column(BigInteger, nullable=False)
    direction = Column(SQLEnum(DirectionEnum, name="direction_enum", values_callable=lambda x: [e.value for e in x]), nullable=False)  # 'add' or 'subtract'
    
    age_days = Column(Integer, nullable=False, default=0)
    exception_flag = Column(Boolean, nullable=False, default=False)
    exception_reason = Column(String, nullable=True)
    overridden_by_user = Column(Boolean, nullable=False, default=False)
    overridden_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    overridden_at = Column(DateTime(timezone=True), nullable=True)

    run = relationship("ReconciliationRun", back_populates="items")
    user = relationship("User", foreign_keys=[user_id])
    overrider = relationship("User", foreign_keys=[overridden_by])
    bank_transaction = relationship("Transaction")
    book_entry = relationship("BookEntry")

    __table_args__ = (
        Index("idx_items_run_side", "run_id", "side"),
    )
