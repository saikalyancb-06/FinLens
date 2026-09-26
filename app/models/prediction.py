import datetime
import uuid
from sqlalchemy import Boolean, Column, String, DateTime, Float, ForeignKey, Integer, JSON, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base

class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id = Column(UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    category_id = Column(UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)

    predicted_category = Column(String(100), nullable=False)
    confidence = Column(Float, nullable=False, default=0.0)
    rule_used = Column(String(255), nullable=True)
    model_version = Column(String(50), nullable=True)

    # ---- Classification provenance -----------------------------------------
    # HOW the category was decided — deliberately separate from the
    # transaction's source_channel, which records WHERE the row came from.
    # All nullable so existing rows remain valid without backfill.
    classification_method = Column(String(20), nullable=True, index=True)  # rule|ml|hybrid|manual|none
    model_name = Column(String(100), nullable=True)
    model_confidence = Column(Float, nullable=True)
    rule_score = Column(Integer, nullable=True)
    rule_category = Column(String(100), nullable=True)
    ml_category = Column(String(100), nullable=True)
    top_3 = Column(JSON, nullable=True)
    explanation = Column(Text, nullable=True)

    # ---- Review workflow ----------------------------------------------------
    requires_review = Column(Boolean, nullable=False, default=False, index=True)
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    transaction = relationship("Transaction", back_populates="prediction")
    category_rel = relationship("Category", back_populates="predictions")
