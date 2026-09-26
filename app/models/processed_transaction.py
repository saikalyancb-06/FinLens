import datetime
import uuid
from sqlalchemy import Column, String, DateTime, Numeric, Float, ForeignKey, Text, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base

class ProcessedTransaction(Base):
    __tablename__ = "processed_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id = Column(UUID(as_uuid=True), ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)

    # 1. Original transaction
    original_raw_text = Column(Text, nullable=True)

    # 2. Normalized transaction fields
    date = Column(DateTime, nullable=True, index=True)
    description = Column(Text, nullable=False)
    debit = Column(Numeric(12, 2), default=0.0)
    credit = Column(Numeric(12, 2), default=0.0)
    amount = Column(Numeric(12, 2), nullable=False)
    balance = Column(Numeric(12, 2), default=0.0)
    reference_number = Column(String(100), nullable=True)
    transaction_type = Column(String(50), nullable=True)

    # 3. Rule prediction
    rule_category = Column(String(100), nullable=True)
    rule_confidence = Column(Float, default=0.0)
    rule_matched = Column(String(255), nullable=True)

    # 4. ML prediction
    ml_category = Column(String(100), nullable=True)
    ml_confidence = Column(Float, default=0.0)
    ml_top_three = Column(JSON, nullable=True)

    # 5. Final category & confidence
    final_category = Column(String(100), nullable=False)
    confidence = Column(Float, nullable=False)
    prediction_source = Column(String(50), nullable=False)
    reasoning = Column(Text, nullable=True)

    # 6. Model version & Processing timestamp
    model_version = Column(String(50), default="v1.0.0", nullable=False)
    processing_timestamp = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    # Relationships
    uploaded_file = relationship("UploadedFile", back_populates="processed_transactions")
    user = relationship("User", back_populates="processed_transactions")
