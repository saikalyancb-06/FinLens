import datetime
import uuid
from sqlalchemy import Column, String, DateTime, ForeignKey, Boolean
from sqlalchemy.dialects.postgresql import UUID
from app.database.session import Base

class OAuthState(Base):
    __tablename__ = "oauth_states"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    state_value = Column(String(255), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False)
    consumed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # What happened at the callback, so the page that opened the sign-in window
    # can ask the server instead of relying on the popup to report back (a
    # popup can be blocked, or cut off from its opener by the provider's
    # Cross-Origin-Opener-Policy).
    result_status = Column(String(20), nullable=True)      # connected | failed
    result_detail = Column(String(1000), nullable=True)
    result_email = Column(String(255), nullable=True)
    connection_id = Column(UUID(as_uuid=True), nullable=True)
    scan_id = Column(UUID(as_uuid=True), nullable=True)
