import sys
import os
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

import pytest
from sqlalchemy.orm import sessionmaker

# The test database has to be resolved *before* app.database.session is
# imported, because that module builds its engine at import time and would
# otherwise connect to the real development database.
from tests.pgtestdb import ensure_database, reset_schema, test_database_url

os.environ["DATABASE_URL"] = ensure_database(test_database_url())
os.environ.setdefault("DB_AUTO_CREATE", "false")

# The test suite must never reach out to rbi.org.in or a rate API. Without this
# the refresher starts with every TestClient and the suite's behaviour depends on
# whether a poll happens to fire before the test finishes.
os.environ["FX_REFRESH_ENABLED"] = "false"
# Same for the B2B housekeeping loop: it would purge/reap rows in the shared
# test database on its own schedule. Its jobs are tested directly instead
# (tests/b2b/test_maintenance.py).
os.environ["B2B_MAINTENANCE_ENABLED"] = "false"
# Mailbox routing must not call Microsoft's tenant lookup from tests.
os.environ["MAILBOX_REALM_LOOKUP"] = "false"
# Tests drive scans themselves; the automatic first scan has its own test.
os.environ["MAILBOX_SCAN_ON_CONNECT"] = "false"

from sqlalchemy import create_engine  # noqa: E402

from app.database.session import Base, get_db  # noqa: E402
import app.database.session as db_session_module  # noqa: E402
import app.services.parsing_queue as pq  # noqa: E402
import app.models  # noqa: E402 — import models before create_all
import app.aa.models  # noqa: E402 — AA tables are registered by this module
from main import app  # noqa: E402

test_engine = create_engine(os.environ["DATABASE_URL"], future=True, pool_pre_ping=True)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

# Captured before anything is patched: the session factory the application built
# at import time. Any module still holding *this* object would bypass the test
# wiring below, so it is the identity we hunt for when re-pointing.
REAL_SESSION_LOCAL = db_session_module.SessionLocal

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    # Dropping and recreating the schema means a run never inherits rows,
    # sequences or ENUM types from the previous one.
    reset_schema(test_engine)
    Base.metadata.create_all(bind=test_engine)
    db_session_module.engine = test_engine




    # Any module that did `from app.database.session import SessionLocal` bound the
    # REAL session factory at import time and is unaffected by rebinding the
    # attribute on app.database.session — it keeps writing into backend_sqlite.db
    # for the whole test run. Test modules are imported during collection, which
    # happens before this session fixture, so by now every such binding exists and
    # can be swept in one pass.
    #
    # This is not hypothetical: app/rpa/runner.py, app/services/parsing_queue.py and
    # tests/test_deduplication_regression.py all held real bindings, and the suite
    # was silently adding rows to the developer database on every run.
    original_session_local = db_session_module.SessionLocal
    for module in list(sys.modules.values()):
        if module is None:
            continue
        if getattr(module, "SessionLocal", None) is original_session_local:
            module.SessionLocal = TestingSessionLocal

    db_session_module.SessionLocal = TestingSessionLocal
    pq.SessionLocal = TestingSessionLocal

    # Seed Bank Master system records
    from app.models import Bank
    import uuid

    db = TestingSessionLocal()
    if db.query(Bank).count() == 0:
        db.add_all([
            Bank(id=uuid.uuid4(), name="HDFC Bank", code="HDFC", is_active=True),
            Bank(id=uuid.uuid4(), name="ICICI Bank", code="ICICI", is_active=True),
            Bank(id=uuid.uuid4(), name="State Bank of India", code="SBI", is_active=True)
        ])
        db.commit()
    db.close()

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield
    Base.metadata.drop_all(bind=test_engine)




from fastapi.testclient import TestClient

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def register_bank_account(client, headers, account_number="502000123456", bank_code="HDFC"):
    """Register a Bank Master account for the authenticated user.

    /files/upload rejects statements with NO_BANK_ACCOUNT until the user has at
    least one registered account (app/api/files.py, documented in README under
    Ingestion → Pre-Upload Validation). Any test that exercises the upload
    pipeline has to satisfy that precondition the same way the UI does.

    Returns the created account payload.
    """
    res = client.post(
        "/v1/bank-master/accounts",
        headers=headers,
        json={
            "account_number": account_number,
            "bank_code": bank_code,
            "account_type": "CURRENT",
            "currency": "INR",
        },
    )
    assert res.status_code == 201, f"Bank account setup failed: {res.text}"
    return res.json()


@pytest.fixture
def bank_account():
    """Fixture wrapper around register_bank_account for tests that prefer injection."""
    return register_bank_account

@pytest.fixture(autouse=True)
def _enforce_test_database():
    """Re-point any real SessionLocal binding at the test engine, before every test.

    The session-scoped sweep in setup_test_db only catches modules imported by
    the time it runs. Anything imported later — lazily, inside a function, or by
    a test module collected afterwards — captures the real session factory and
    writes into backend_sqlite.db. Re-applying per test makes the outcome
    independent of import order, which is the only way to guarantee the suite
    cannot touch the developer database.
    """
    for module in list(sys.modules.values()):
        if module is None:
            continue
        # Identity match only. Several test modules build their own private
        # in-memory session factory; matching on name would stomp those and
        # break legitimate isolation.
        if getattr(module, "SessionLocal", None) is REAL_SESSION_LOCAL:
            try:
                module.SessionLocal = TestingSessionLocal
            except Exception:
                pass
    yield


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Clear the in-memory rate limit records before every test.

    The rate limiter in main.py uses a module-level defaultdict keyed by client IP.
    Without resetting it, the 21 auth calls in test_canonical_migration.py exhaust
    the 120-req/min budget and cause 429s in tests that run afterwards.
    """
    import main as main_module
    main_module.rate_limit_records.clear()
    yield
    main_module.rate_limit_records.clear()
