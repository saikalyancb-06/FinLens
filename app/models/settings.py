import uuid
import datetime
from typing import Optional
from sqlalchemy import Column, String, DateTime, Boolean, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base


class EmailSchedule(Base):
    __tablename__ = "email_schedules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    report_type = Column(String(100), nullable=False)  # e.g. "Daily Treasury Summary", "Weekly Compliance", "Monthly CFO Pack"
    frequency = Column(String(50), nullable=False)      # "Daily", "Weekly", "Monthly"
    scheduled_time = Column(String(20), nullable=False, default="09:00")  # "HH:MM"
    day_of_week = Column(String(20), nullable=True)     # "Monday", "Tuesday", etc.
    day_of_month = Column(Integer, nullable=True)       # 1 - 31
    recipients = Column(Text, nullable=False)           # Comma-separated emails
    is_active = Column(Boolean, default=True, nullable=False)
    next_send_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    user = relationship("User", backref="email_schedules")


class UserPreference(Base):
    __tablename__ = "user_preferences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    default_currency = Column(String(20), nullable=False, default="INR - ₹")
    date_format = Column(String(50), nullable=False, default="MMM DD, YYYY")

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    user = relationship("User", backref="preference", uselist=False)


def calculate_next_send_at(
    frequency: str,
    scheduled_time: str,
    day_of_week: Optional[str] = None,
    day_of_month: Optional[int] = None,
    is_active: bool = True,
    now: Optional[datetime.datetime] = None
) -> Optional[datetime.datetime]:
    if not is_active:
        return None

    if now is None:
        now = datetime.datetime.utcnow()

    # Parse HH:MM
    try:
        parts = scheduled_time.strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        hour, minute = 9, 0

    freq_upper = (frequency or "").upper()

    if freq_upper == "DAILY":
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += datetime.timedelta(days=1)
        return candidate

    elif freq_upper == "WEEKLY":
        days_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2,
            "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6
        }
        target_day = days_map.get((day_of_week or "monday").strip().lower(), 0)
        current_day = now.weekday()
        days_ahead = target_day - current_day
        if days_ahead < 0 or (days_ahead == 0 and (now.hour > hour or (now.hour == hour and now.minute >= minute))):
            days_ahead += 7

        candidate = (now + datetime.timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return candidate

    elif freq_upper == "MONTHLY":
        target_dom = int(day_of_month) if day_of_month and 1 <= int(day_of_month) <= 31 else 1
        
        # Try current month first
        year = now.year
        month = now.month
        
        import calendar
        max_days = calendar.monthrange(year, month)[1]
        dom = min(target_dom, max_days)
        candidate = datetime.datetime(year, month, dom, hour, minute, 0, 0)
        
        if candidate <= now:
            # Advance to next month
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1
            max_days = calendar.monthrange(year, month)[1]
            dom = min(target_dom, max_days)
            candidate = datetime.datetime(year, month, dom, hour, minute, 0, 0)

        return candidate

    else:
        # Fallback daily
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += datetime.timedelta(days=1)
        return candidate
