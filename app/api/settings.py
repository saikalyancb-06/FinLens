import uuid
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.user import User
from app.models.settings import EmailSchedule, UserPreference, calculate_next_send_at
from app.utils.security import get_current_user
from app.api.settings_schemas import (
    EmailScheduleCreate,
    EmailScheduleUpdate,
    EmailScheduleResponse,
    UserPreferenceUpdate,
    UserPreferenceResponse
)

router = APIRouter(prefix="/settings", tags=["Settings & Preferences"])


# ---------------------------------------------------------------------------
# 1. User Preferences Endpoints
# ---------------------------------------------------------------------------

@router.get("/preferences", response_model=UserPreferenceResponse)
def get_user_preferences(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Retrieve settings preferences for the current authenticated user."""
    pref = db.query(UserPreference).filter(UserPreference.user_id == current_user.id).first()
    if not pref:
        # Create default preference for user
        pref = UserPreference(
            user_id=current_user.id,
            default_currency="INR - ₹",
            date_format="MMM DD, YYYY"
        )
        db.add(pref)
        db.commit()
        db.refresh(pref)
    return pref


@router.put("/preferences", response_model=UserPreferenceResponse)
def update_user_preferences(
    payload: UserPreferenceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update settings preferences (currency, date format) for current user."""
    pref = db.query(UserPreference).filter(UserPreference.user_id == current_user.id).first()
    if not pref:
        pref = UserPreference(user_id=current_user.id)
        db.add(pref)

    pref.default_currency = payload.default_currency
    pref.date_format = payload.date_format
    db.commit()
    db.refresh(pref)
    return pref


# ---------------------------------------------------------------------------
# 2. Email Schedules Endpoints
# ---------------------------------------------------------------------------

def seed_default_schedules_if_empty(db: Session, user_id: uuid.UUID) -> List[EmailSchedule]:
    count = db.query(EmailSchedule).filter(EmailSchedule.user_id == user_id).count()
    if count == 0:
        # Seed the 2 required initial default schedules
        sched1 = EmailSchedule(
            user_id=user_id,
            report_type="Daily Treasury Summary",
            frequency="Daily",
            scheduled_time="09:00",
            recipients="cfo@kredo.in, finance@kredo.in",
            is_active=True
        )
        sched1.next_send_at = calculate_next_send_at("Daily", "09:00", is_active=True)

        sched2 = EmailSchedule(
            user_id=user_id,
            report_type="Weekly Compliance",
            frequency="Weekly",
            scheduled_time="09:00",
            day_of_week="Monday",
            recipients="board@kredo.in, cfo@kredo.in",
            is_active=True
        )
        sched2.next_send_at = calculate_next_send_at("Weekly", "09:00", day_of_week="Monday", is_active=True)

        db.add_all([sched1, sched2])
        db.commit()
        return [sched1, sched2]
    return []


@router.get("/email-schedules", response_model=List[EmailScheduleResponse])
def get_email_schedules(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all scheduled email reports for current user."""
    seed_default_schedules_if_empty(db, current_user.id)

    schedules = db.query(EmailSchedule).filter(
        EmailSchedule.user_id == current_user.id
    ).order_by(EmailSchedule.created_at.asc()).all()

    # Dynamically update next_send_at calculation
    for s in schedules:
        s.next_send_at = calculate_next_send_at(
            frequency=s.frequency,
            scheduled_time=s.scheduled_time,
            day_of_week=s.day_of_week,
            day_of_month=s.day_of_month,
            is_active=s.is_active
        )
    db.commit()

    return schedules


@router.post("/email-schedules", response_model=EmailScheduleResponse, status_code=status.HTTP_201_CREATED)
def create_email_schedule(
    payload: EmailScheduleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new scheduled email report."""
    next_send = calculate_next_send_at(
        frequency=payload.frequency,
        scheduled_time=payload.scheduled_time,
        day_of_week=payload.day_of_week,
        day_of_month=payload.day_of_month,
        is_active=payload.is_active
    )

    schedule = EmailSchedule(
        user_id=current_user.id,
        report_type=payload.report_type.strip(),
        frequency=payload.frequency,
        scheduled_time=payload.scheduled_time,
        day_of_week=payload.day_of_week.strip().capitalize() if payload.day_of_week else None,
        day_of_month=payload.day_of_month,
        recipients=payload.recipients,
        is_active=payload.is_active,
        next_send_at=next_send
    )

    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.put("/email-schedules/{schedule_id}", response_model=EmailScheduleResponse)
def update_email_schedule(
    schedule_id: uuid.UUID,
    payload: EmailScheduleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing scheduled email report."""
    schedule = db.query(EmailSchedule).filter(
        EmailSchedule.id == schedule_id,
        EmailSchedule.user_id == current_user.id
    ).first()

    if not schedule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Scheduled email report not found"
        )

    if payload.report_type is not None:
        schedule.report_type = payload.report_type.strip()
    if payload.frequency is not None:
        schedule.frequency = payload.frequency
    if payload.scheduled_time is not None:
        schedule.scheduled_time = payload.scheduled_time
    if payload.day_of_week is not None:
        schedule.day_of_week = payload.day_of_week.strip().capitalize() if payload.day_of_week else None
    if payload.day_of_month is not None:
        schedule.day_of_month = payload.day_of_month
    if payload.recipients is not None:
        schedule.recipients = payload.recipients
    if payload.is_active is not None:
        schedule.is_active = payload.is_active

    schedule.next_send_at = calculate_next_send_at(
        frequency=schedule.frequency,
        scheduled_time=schedule.scheduled_time,
        day_of_week=schedule.day_of_week,
        day_of_month=schedule.day_of_month,
        is_active=schedule.is_active
    )

    db.commit()
    db.refresh(schedule)
    return schedule


@router.delete("/email-schedules/{schedule_id}", status_code=status.HTTP_200_OK)
def delete_email_schedule(
    schedule_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a scheduled email report."""
    schedule = db.query(EmailSchedule).filter(
        EmailSchedule.id == schedule_id,
        EmailSchedule.user_id == current_user.id
    ).first()

    if not schedule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Scheduled email report not found"
        )

    db.delete(schedule)
    db.commit()
    return {"status": "success", "message": "Scheduled email report deleted successfully", "id": str(schedule_id)}
