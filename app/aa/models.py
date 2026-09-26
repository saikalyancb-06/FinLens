import uuid
import datetime
from sqlalchemy import Column, String, DateTime, Boolean, ForeignKey, Integer, Text, Float
from sqlalchemy.dialects.postgresql import UUID
from app.database.session import Base

class AaConsent(Base):
    __tablename__ = "aa_consents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    consent_handle = Column(String(255), unique=True, index=True, nullable=False)
    consent_id = Column(String(255), nullable=True)
    status = Column(String(50), default="PENDING", nullable=False)
    purpose_code = Column(String(50), default="101", nullable=False)
    fi_type = Column(String(50), default="DEPOSIT", nullable=False)
    date_from = Column(DateTime, nullable=True)
    date_to = Column(DateTime, nullable=True)
    approval_url = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class AaDataSession(Base):
    __tablename__ = "aa_data_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    consent_id = Column(String(255), nullable=True)
    session_id = Column(String(255), unique=True, index=True, nullable=False)
    status = Column(String(50), default="PENDING", nullable=False)
    fetched_count = Column(Integer, default=0)
    records_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
