import uuid
import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from main import app
from app.database.session import Base, get_db
from app.models.user import User
from app.models.account import Account
from app.models.transaction import Transaction
from app.models.refresh_token import RefreshToken
from app.utils.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    hash_token,
)
from app.config import settings

client = TestClient(app)

from sqlalchemy.pool import StaticPool

@pytest.fixture(scope="module")
def test_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    yield db
    app.dependency_overrides.clear()



# ─────────────────────────────────────────────────────────────────────────────
# 1-5. Registration, Login, Wrong Password Hash Protection
# ─────────────────────────────────────────────────────────────────────────────

def test_1_register_user(test_db):
    payload = {
        "email": "sec_user@example.com",
        "password": "SecurePassword123!",
        "full_name": "Security Test User"
    }
    res = client.post("/auth/register", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["email"] == "sec_user@example.com"
    assert "id" in data


def test_2_login_correct_password(test_db):
    payload = {
        "email": "sec_user@example.com",
        "password": "SecurePassword123!"
    }
    res = client.post("/auth/login", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert "refresh_token" in data


def test_3_and_4_login_wrong_password_does_not_modify_hash(test_db):
    user_before = test_db.query(User).filter(User.email == "sec_user@example.com").first()
    original_hash = user_before.hashed_password

    # Wrong password attempt
    payload = {
        "email": "sec_user@example.com",
        "password": "WrongPassword999!"
    }
    res = client.post("/auth/login", json=payload)
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password"

    # Verify hash in DB was NOT changed
    test_db.refresh(user_before)
    user_after = test_db.query(User).filter(User.email == "sec_user@example.com").first()
    assert user_after.hashed_password == original_hash

    # Unknown user attempt
    unknown_payload = {
        "email": "unknown_ghost_user@example.com",
        "password": "AnyPassword123!"
    }
    res_unknown = client.post("/auth/login", json=unknown_payload)
    assert res_unknown.status_code == 401
    assert res_unknown.json()["detail"] == "Invalid email or password"

    # Verify unknown user was NOT created in DB
    ghost = test_db.query(User).filter(User.email == "unknown_ghost_user@example.com").first()
    assert ghost is None


def test_5_login_again_with_original_password(test_db):
    payload = {
        "email": "sec_user@example.com",
        "password": "SecurePassword123!"
    }
    res = client.post("/auth/login", json=payload)
    assert res.status_code == 200
    assert "access_token" in res.json()


# ─────────────────────────────────────────────────────────────────────────────
# 6-7. Expired Access Token & Refresh Token Revocation
# ─────────────────────────────────────────────────────────────────────────────

def test_6_expired_access_token(test_db):
    user = test_db.query(User).filter(User.email == "sec_user@example.com").first()
    # Create expired token (-10 minutes)
    expired_token = create_access_token(
        data={"sub": str(user.id)},
        expires_delta=timedelta(minutes=-10)
    )

    headers = {"Authorization": f"Bearer {expired_token}"}
    res = client.get("/auth/me", headers=headers)
    assert res.status_code == 401
    assert "expired" in res.json()["detail"].lower()


def test_7_refresh_token_flow(test_db):
    user = test_db.query(User).filter(User.email == "sec_user@example.com").first()
    refresh_token = create_refresh_token(data={"sub": str(user.id)})
    
    # Store hash in DB
    ref_hash = hash_token(refresh_token)
    db_token = RefreshToken(
        user_id=user.id,
        token_hash=ref_hash,
        expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).replace(tzinfo=None)
    )
    test_db.add(db_token)
    test_db.commit()

    # Call /auth/refresh
    res = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert "refresh_token" in data

    # Verify old token was revoked
    test_db.refresh(db_token)
    assert db_token.revoked is True

    # Try refreshing again with revoked token -> 401
    res_revoked = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert res_revoked.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# 8-9. Authenticated Access & User Isolation / IDOR Protection
# ─────────────────────────────────────────────────────────────────────────────

def test_8_and_9_user_isolation_and_idor(test_db):
    # Setup User A and User B
    userA = User(id=uuid.uuid4(), email="userA@sec.com", hashed_password=hash_password("PassA123!"))
    userB = User(id=uuid.uuid4(), email="userB@sec.com", hashed_password=hash_password("PassB123!"))
    test_db.add_all([userA, userB])
    test_db.commit()

    # Create transaction for User A
    from app.models.transaction import Direction, SourceType
    txA = Transaction(
        id=uuid.uuid4(),
        user_id=userA.id,
        txn_date=datetime.now(timezone.utc).date(),
        narration_raw="User A Transaction",
        debit_paise=10000,
        direction=Direction.DEBIT,
        source_type=SourceType.STATEMENT
    )


    test_db.add(txA)
    test_db.commit()

    # Token for User B
    tokenB = create_access_token(data={"sub": str(userB.id)})
    headersB = {"Authorization": f"Bearer {tokenB}"}

    # User B accesses /auth/me -> Returns User B
    res_me = client.get("/auth/me", headers=headersB)
    assert res_me.status_code == 200
    assert res_me.json()["email"] == "userB@sec.com"

    # User B attempts to access User A's transaction directly -> 404 Not Found
    res_tx = client.get(f"/transactions/{txA.id}", headers=headersB)
    assert res_tx.status_code == 404
    assert res_tx.json()["detail"] == "Transaction not found"


# ─────────────────────────────────────────────────────────────────────────────
# 10. PostgreSQL is mandatory — no SQLite fallback
# ─────────────────────────────────────────────────────────────────────────────

def test_10_non_postgres_url_is_rejected():
    """A non-PostgreSQL DATABASE_URL must be refused, in every environment.

    The application previously fell back to a local SQLite file whenever
    PostgreSQL was unreachable, silently splitting the data across two stores.
    Both guards — the config-level scheme check and the session-level one — now
    reject anything that is not PostgreSQL.
    """
    from app.config import Settings
    from app.database.session import _require_postgres

    for bad_url in (
        "sqlite:///./backend_sqlite.db",
        "sqlite:///:memory:",
        "mysql://root@localhost/backend_db",
    ):
        with pytest.raises(RuntimeError, match="(?i)postgres"):
            _require_postgres(bad_url)

        s = Settings()
        s.DATABASE_URL = bad_url
        with pytest.raises(RuntimeError, match="(?i)postgres"):
            s.validate_database()

    # A genuine PostgreSQL URL passes both guards.
    ok = "postgresql://postgres:postgres@localhost:5432/backend_db"
    assert _require_postgres(ok) == ok
    s = Settings()
    s.DATABASE_URL = ok
    s.validate_database()


def test_10b_unreachable_database_raises_rather_than_falling_back():
    """An unreachable PostgreSQL server is a hard failure, not a silent fallback."""
    from sqlalchemy import create_engine
    from app.config import settings
    from app.database.session import wait_for_database

    orig_retries = settings.DB_CONNECT_RETRIES
    settings.DB_CONNECT_RETRIES = 1  # keep the test fast
    try:
        broken = create_engine(
            "postgresql+psycopg2://postgres:postgres@localhost:59999/non_existent_db",
            connect_args={"connect_timeout": 2},
        )
        with pytest.raises(RuntimeError) as exc_info:
            wait_for_database(broken)
        assert "no SQLite fallback" in str(exc_info.value)
    finally:
        settings.DB_CONNECT_RETRIES = orig_retries
