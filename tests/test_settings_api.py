import uuid
import pytest
from fastapi.testclient import TestClient
from main import app
from app.database.session import get_db
from app.models.user import User
from app.models.settings import EmailSchedule, UserPreference
from app.utils.security import hash_password, create_access_token

client = TestClient(app)


def create_test_user_and_headers(email_prefix: str, db_session):
    email = f"{email_prefix}_{uuid.uuid4().hex[:8]}@example.com"
    pwd = "TestPassword123!"
    hashed = hash_password(pwd)
    user = User(email=email, hashed_password=hashed)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    token = create_access_token(data={"sub": str(user.id)})
    headers = {"Authorization": f"Bearer {token}"}
    return user, headers


def test_get_and_update_preferences():
    # Setup test user via database
    db = next(get_db())
    user, headers = create_test_user_and_headers("pref_user", db)

    try:
        # GET Preferences (initial default should be returned)
        res = client.get("/settings/preferences", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["default_currency"] == "INR - ₹"
        assert data["date_format"] == "MMM DD, YYYY"

        # UPDATE Preferences
        update_payload = {
            "default_currency": "USD - $",
            "date_format": "YYYY-MM-DD"
        }
        put_res = client.put("/settings/preferences", json=update_payload, headers=headers)
        assert put_res.status_code == 200
        updated = put_res.json()
        assert updated["default_currency"] == "USD - $"
        assert updated["date_format"] == "YYYY-MM-DD"

        # GET Preferences again to verify persistence
        get_res2 = client.get("/settings/preferences", headers=headers)
        assert get_res2.status_code == 200
        assert get_res2.json()["default_currency"] == "USD - $"
        assert get_res2.json()["date_format"] == "YYYY-MM-DD"

    finally:
        db.query(UserPreference).filter(UserPreference.user_id == user.id).delete()
        db.query(User).filter(User.id == user.id).delete()
        db.commit()


def test_email_schedules_crud_and_validation():
    db = next(get_db())
    user1, headers1 = create_test_user_and_headers("sched_u1", db)
    user2, headers2 = create_test_user_and_headers("sched_u2", db)

    try:
        # GET Schedules for User 1 (seeds 2 defaults)
        res1 = client.get("/settings/email-schedules", headers=headers1)
        assert res1.status_code == 200
        schedules1 = res1.json()
        assert len(schedules1) == 2
        assert schedules1[0]["report_type"] == "Daily Treasury Summary"
        assert schedules1[0]["next_send_at"] is not None

        # CREATE Schedule for User 1
        create_payload = {
            "report_type": "Monthly CFO Pack",
            "frequency": "Monthly",
            "scheduled_time": "10:30",
            "day_of_month": 15,
            "recipients": "cfo@company.com, board@company.com",
            "is_active": True
        }
        post_res = client.post("/settings/email-schedules", json=create_payload, headers=headers1)
        assert post_res.status_code == 201
        created = post_res.json()
        sched_id = created["id"]
        assert created["report_type"] == "Monthly CFO Pack"
        assert created["frequency"] == "Monthly"
        assert created["next_send_at"] is not None

        # UPDATE Schedule
        update_payload = {
            "scheduled_time": "14:00",
            "is_active": False
        }
        put_res = client.put(f"/settings/email-schedules/{sched_id}", json=update_payload, headers=headers1)
        assert put_res.status_code == 200
        updated = put_res.json()
        assert updated["scheduled_time"] == "14:00"
        assert updated["is_active"] is False
        assert updated["next_send_at"] is None

        # ISOLATION: User 2 cannot access or edit User 1's schedule
        user2_put = client.put(f"/settings/email-schedules/{sched_id}", json=update_payload, headers=headers2)
        assert user2_put.status_code == 404

        user2_del = client.delete(f"/settings/email-schedules/{sched_id}", headers=headers2)
        assert user2_del.status_code == 404

        # DELETE Schedule
        del_res = client.delete(f"/settings/email-schedules/{sched_id}", headers=headers1)
        assert del_res.status_code == 200

        # VALIDATION TESTS
        invalid_freq = {
            "report_type": "Invalid Report",
            "frequency": "Hourly",
            "scheduled_time": "09:00",
            "recipients": "test@company.com"
        }
        err_res1 = client.post("/settings/email-schedules", json=invalid_freq, headers=headers1)
        assert err_res1.status_code == 422

        invalid_time = {
            "report_type": "Invalid Report",
            "frequency": "Daily",
            "scheduled_time": "25:99",
            "recipients": "test@company.com"
        }
        err_res2 = client.post("/settings/email-schedules", json=invalid_time, headers=headers1)
        assert err_res2.status_code == 422

        invalid_recip = {
            "report_type": "Invalid Report",
            "frequency": "Daily",
            "scheduled_time": "09:00",
            "recipients": "not-an-email"
        }
        err_res3 = client.post("/settings/email-schedules", json=invalid_recip, headers=headers1)
        assert err_res3.status_code == 422

    finally:
        db.query(EmailSchedule).filter(EmailSchedule.user_id.in_([user1.id, user2.id])).delete()
        db.query(User).filter(User.id.in_([user1.id, user2.id])).delete()
        db.commit()
