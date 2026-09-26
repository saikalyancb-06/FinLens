import enum
import uuid
import datetime

from sqlalchemy import Column, String, DateTime, Enum, LargeBinary, Text
from sqlalchemy.dialects.postgresql import UUID

from app.database.session import Base


class RpaJobStatus(str, enum.Enum):
    QUEUED = "queued"
    LAUNCHING_BROWSER = "launching_browser"
    LOGGING_IN = "logging_in"
    AWAITING_INPUT = "awaiting_input"
    NAVIGATING = "navigating"
    AWAITING_OTP = "awaiting_otp"
    AWAITING_PDF_PASSWORD = "awaiting_pdf_password"
    DOWNLOADING = "downloading"
    UPLOADING = "uploading"
    PARSING = "parsing"
    IMPORTING = "importing"
    SUCCESS = "success"
    FAILED = "failed"


class RpaJob(Base):
    __tablename__ = "rpa_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    bank_name = Column(String(100), nullable=False)
    date_range_start = Column(DateTime, nullable=False)
    date_range_end = Column(DateTime, nullable=False)

    # Job lifecycle
    status = Column(
        Enum(RpaJobStatus, name="rpa_job_status_enum"),
        default=RpaJobStatus.QUEUED,
        nullable=False,
    )
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
    )

    # Legacy column retained for compatibility; workflow now keeps credentials
    # only in the in-memory RPA session store.
    encrypted_credentials = Column(LargeBinary, nullable=True)

    # Output
    file_path = Column(String(512), nullable=True)
    statement_id = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)
    screenshot_path = Column(String(512), nullable=True)

    # Audit
    user_acknowledged = Column(String(5), default="yes")  # user confirmed consent
