"""Persistence for the B2B API itself — not for the financial data.

The analysis is stateless: a statement is parsed, analysed and answered without
any of its transactions reaching the database. What *is* stored is the API's own
operational record — who called, whether it worked, how long it took — plus the
result payload for the polling endpoint, under a configurable retention.

Five tables:

  api_clients   one row per integrating company (Credit Lens is one)
  api_keys      many keys per client; only a SHA-256 of the secret is stored
  analysis_requests  one row per POST /v1/analyze, the unit of idempotency
  usage_records      one row per billable/countable event, for metering
  webhook_deliveries attempt log for outbound callbacks

The separation of `analysis_requests` (what happened) from `usage_records`
(what to count) is deliberate: retention can delete results while keeping the
counts a bill would be built from.
"""
from __future__ import annotations

import datetime
import enum
import uuid

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey, Index, Integer,
    JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import relationship

from app.database.session import Base


class RequestStatus(str, enum.Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ApiClient(Base):
    """An integrating company."""
    __tablename__ = "api_clients"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(160), nullable=False)
    slug = Column(String(80), nullable=False, unique=True, index=True)
    contact_email = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    # Plan shapes the limits. Kept as plain columns rather than a plans table:
    # there is one dimension of variation today and inventing the join before
    # there are plans to join to would be infrastructure ahead of need.
    plan = Column(String(40), nullable=False, default="free")
    rate_limit_per_minute = Column(Integer, nullable=False, default=30)
    rate_limit_per_day = Column(Integer, nullable=False, default=1000)
    rate_limit_per_month = Column(Integer, nullable=False, default=10000)
    max_file_size_bytes = Column(Integer, nullable=True)   # None -> global default

    # Retention, per client, in hours. 0 means "discard the result as soon as
    # it has been returned"; the request row survives for metering either way.
    result_retention_hours = Column(Integer, nullable=False, default=24)

    webhook_url = Column(String(500), nullable=True)
    webhook_secret = Column(String(128), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.datetime.now(datetime.timezone.utc),
                        onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    keys = relationship("ApiKey", back_populates="client",
                        cascade="all, delete-orphan")


class ApiKey(Base):
    """A bearer credential.

    The secret is shown once, at creation, and never again — only
    `key_hash` (SHA-256 of the full secret) is stored, so a database
    disclosure does not hand over working credentials. `key_prefix` is the
    first characters of the secret, kept in clear specifically so a human can
    identify a key in a list and in an audit trail without it being usable.
    """
    __tablename__ = "api_keys"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(PGUUID(as_uuid=True),
                       ForeignKey("api_clients.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    name = Column(String(120), nullable=True)
    key_prefix = Column(String(24), nullable=False, index=True)
    key_hash = Column(String(64), nullable=False, unique=True, index=True)
    scopes = Column(String(255), nullable=False, default="analyze:write,analyze:read")

    is_active = Column(Boolean, nullable=False, default=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    # Rotation links the replacement to what it replaced, so revoking the old
    # key later is an auditable act rather than an orphaned deletion.
    rotated_from_id = Column(PGUUID(as_uuid=True),
                             ForeignKey("api_keys.id", ondelete="SET NULL"),
                             nullable=True)

    last_used_at = Column(DateTime(timezone=True), nullable=True)
    last_used_ip = Column(String(64), nullable=True)
    use_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.datetime.now(datetime.timezone.utc))

    client = relationship("ApiClient", back_populates="keys")

    @property
    def is_usable(self) -> bool:
        if not self.is_active or self.revoked_at is not None:
            return False
        if self.expires_at is not None:
            now = datetime.datetime.now(datetime.timezone.utc)
            exp = self.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=datetime.timezone.utc)
            if exp <= now:
                return False
        return True


class AnalysisRequest(Base):
    """One POST /v1/analyze.

    `idempotency_key` is unique per client, which is what makes a retry with
    the same key return the first answer instead of parsing the file twice.
    """
    __tablename__ = "analysis_requests"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id = Column(String(64), nullable=False, unique=True, index=True)
    client_id = Column(PGUUID(as_uuid=True),
                       ForeignKey("api_clients.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    api_key_id = Column(PGUUID(as_uuid=True),
                        ForeignKey("api_keys.id", ondelete="SET NULL"),
                        nullable=True)

    idempotency_key = Column(String(128), nullable=True)
    status = Column(Enum(RequestStatus, name="b2b_request_status"),
                    nullable=False, default=RequestStatus.QUEUED, index=True)

    # About the input, never the input itself. The filename is stored because
    # an integrator debugging a failure needs to know which file it was; the
    # bytes are not.
    filename = Column(String(255), nullable=True)
    detected_format = Column(String(32), nullable=True)
    file_size_bytes = Column(Integer, nullable=True)
    file_sha256 = Column(String(64), nullable=True, index=True)

    country = Column(String(8), nullable=True)
    currency = Column(String(8), nullable=True)

    transaction_count = Column(Integer, nullable=True)
    overall_confidence = Column(Float, nullable=True)

    result = Column(JSON, nullable=True)          # cleared by retention
    result_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)

    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)

    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.datetime.now(datetime.timezone.utc),
                        index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key",
                         name="uq_b2b_client_idempotency"),
        Index("ix_b2b_req_client_created", "client_id", "created_at"),
    )


class UsageRecord(Base):
    """One countable event. Kept even when the result is discarded."""
    __tablename__ = "api_usage_records"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(PGUUID(as_uuid=True),
                       ForeignKey("api_clients.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    api_key_id = Column(PGUUID(as_uuid=True), nullable=True)
    request_id = Column(String(64), nullable=True, index=True)

    endpoint = Column(String(120), nullable=False)
    method = Column(String(10), nullable=False, default="POST")
    status_code = Column(Integer, nullable=False)
    succeeded = Column(Boolean, nullable=False, default=False)
    error_code = Column(String(64), nullable=True)

    file_processed = Column(Boolean, nullable=False, default=False)
    file_size_bytes = Column(Integer, nullable=True)
    detected_format = Column(String(32), nullable=True)
    transaction_count = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    occurred_at = Column(DateTime(timezone=True), nullable=False,
                         default=lambda: datetime.datetime.now(datetime.timezone.utc),
                         index=True)

    __table_args__ = (
        Index("ix_b2b_usage_client_time", "client_id", "occurred_at"),
    )


class WebhookDelivery(Base):
    """An outbound callback attempt."""
    __tablename__ = "api_webhook_deliveries"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(String(64), nullable=False, unique=True, index=True)
    client_id = Column(PGUUID(as_uuid=True),
                       ForeignKey("api_clients.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    request_id = Column(String(64), nullable=True, index=True)

    event_type = Column(String(64), nullable=False)
    url = Column(String(500), nullable=False)
    payload = Column(JSON, nullable=True)

    attempts = Column(Integer, nullable=False, default=0)
    delivered = Column(Boolean, nullable=False, default=False)
    last_status_code = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.datetime.now(datetime.timezone.utc))
    delivered_at = Column(DateTime(timezone=True), nullable=True)
