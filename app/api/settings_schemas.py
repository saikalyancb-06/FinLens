import uuid
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, ConfigDict


# --- Email Schedule Schemas ---

class EmailScheduleBase(BaseModel):
    report_type: str = Field(..., description="Report type e.g. Daily Treasury Summary, Weekly Compliance")
    frequency: str = Field(..., description="Daily, Weekly, or Monthly")
    scheduled_time: str = Field("09:00", description="HH:MM format in 24-hour time")
    day_of_week: Optional[str] = Field(None, description="Monday, Tuesday, etc. for Weekly frequency")
    day_of_month: Optional[int] = Field(None, description="1-31 for Monthly frequency")
    recipients: str = Field(..., description="Comma-separated list of valid recipient email addresses")
    is_active: bool = Field(True, description="Active or Inactive status")

    @field_validator("frequency")
    @classmethod
    def validate_frequency(cls, v: str) -> str:
        freq = (v or "").strip().capitalize()
        if freq not in ["Daily", "Weekly", "Monthly"]:
            raise ValueError("Frequency must be one of: Daily, Weekly, Monthly")
        return freq

    @field_validator("scheduled_time")
    @classmethod
    def validate_time(cls, v: str) -> str:
        s = (v or "").strip()
        parts = s.split(":")
        if len(parts) != 2:
            raise ValueError("Time must be in HH:MM format")
        try:
            h = int(parts[0])
            m = int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError("Time must be valid 24-hour HH:MM (00:00 to 23:59)")
        except ValueError:
            raise ValueError("Time must be valid HH:MM numbers")
        return f"{h:02d}:{m:02d}"

    @field_validator("recipients")
    @classmethod
    def validate_recipients(cls, v: str) -> str:
        raw = (v or "").strip()
        if not raw:
            raise ValueError("At least one recipient email address is required")
        emails = [e.strip() for e in raw.split(",") if e.strip()]
        if not emails:
            raise ValueError("At least one valid recipient email address is required")
        for e in emails:
            if "@" not in e or "." not in e.split("@")[-1]:
                raise ValueError(f"Invalid recipient email address: '{e}'")
        return ", ".join(emails)


class EmailScheduleCreate(EmailScheduleBase):
    pass


class EmailScheduleUpdate(BaseModel):
    report_type: Optional[str] = None
    frequency: Optional[str] = None
    scheduled_time: Optional[str] = None
    day_of_week: Optional[str] = None
    day_of_month: Optional[int] = None
    recipients: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("frequency")
    @classmethod
    def validate_frequency(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        freq = (v or "").strip().capitalize()
        if freq not in ["Daily", "Weekly", "Monthly"]:
            raise ValueError("Frequency must be one of: Daily, Weekly, Monthly")
        return freq

    @field_validator("scheduled_time")
    @classmethod
    def validate_time(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = (v or "").strip()
        parts = s.split(":")
        if len(parts) != 2:
            raise ValueError("Time must be in HH:MM format")
        try:
            h = int(parts[0])
            m = int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError("Time must be valid 24-hour HH:MM (00:00 to 23:59)")
        except ValueError:
            raise ValueError("Time must be valid HH:MM numbers")
        return f"{h:02d}:{m:02d}"

    @field_validator("recipients")
    @classmethod
    def validate_recipients(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        raw = (v or "").strip()
        if not raw:
            raise ValueError("At least one recipient email address is required")
        emails = [e.strip() for e in raw.split(",") if e.strip()]
        if not emails:
            raise ValueError("At least one valid recipient email address is required")
        for e in emails:
            if "@" not in e or "." not in e.split("@")[-1]:
                raise ValueError(f"Invalid recipient email address: '{e}'")
        return ", ".join(emails)


class EmailScheduleResponse(EmailScheduleBase):
    id: uuid.UUID
    user_id: uuid.UUID
    next_send_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Preferences Schemas ---

class UserPreferenceBase(BaseModel):
    default_currency: str = Field("INR - ₹", description="Preferred default currency")
    date_format: str = Field("MMM DD, YYYY", description="Preferred date format")

    @field_validator("default_currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        allowed = ["INR - ₹", "USD - $", "EUR - €", "GBP - £"]
        val = (v or "").strip()
        if val not in allowed:
            raise ValueError(f"Currency must be one of: {', '.join(allowed)}")
        return val

    @field_validator("date_format")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        allowed = ["DD/MM/YYYY", "MMM DD, YYYY", "YYYY-MM-DD"]
        val = (v or "").strip()
        if val not in allowed:
            raise ValueError(f"Date format must be one of: {', '.join(allowed)}")
        return val


class UserPreferenceUpdate(UserPreferenceBase):
    pass


class UserPreferenceResponse(UserPreferenceBase):
    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
