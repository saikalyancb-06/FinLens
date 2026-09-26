import uuid
import datetime
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from main import app
from app.database.session import get_db
from app.models.user import User
from app.models.account import Account
from app.models.entity import Entity
from app.models.transaction import Transaction, Direction
from tests.conftest import test_engine, Base

client = TestClient(app)


@pytest.fixture(autouse=True)
def init_test_db():
    Base.metadata.create_all(bind=test_engine)
    yield


def register_and_login_user(client: TestClient, prefix: str):
    email = f"{prefix}_{uuid.uuid4().hex[:6]}@example.com"
    password = "StrongPassword123!"
    reg_res = client.post("/auth/register", json={"email": email, "password": password})
    assert reg_res.status_code == 201
    user_id_str = reg_res.json()["id"]

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, token, user_id_str


def seed_transactions_for_user(user_id_str: str):
    db: Session = next(get_db())
    u_uuid = uuid.UUID(user_id_str)

    tx1 = Transaction(
        user_id=u_uuid,
        txn_date=datetime.date(2026, 8, 1),
        narration_raw="Client Invoice Payment",
        credit_paise=15000000, # 150,000 INR
        direction=Direction.CREDIT,
        source_channel="STATEMENT"
    )
    tx2 = Transaction(
        user_id=u_uuid,
        txn_date=datetime.date(2026, 8, 5),
        narration_raw="Office Rent Payment",
        debit_paise=5000000, # 50,000 INR
        direction=Direction.DEBIT,
        source_channel="STATEMENT"
    )
    db.add_all([tx1, tx2])
    db.commit()


class TestReportExportsAuthentication:
    """Test suite ensuring report exports function with Bearer headers and direct token query parameters."""

    def test_export_csv_with_bearer_header(self, client):
        headers, token, user_id = register_and_login_user(client, "exp_csv_h")
        seed_transactions_for_user(user_id)

        res = client.get("/reports/export/csv", headers=headers)
        assert res.status_code == 200
        assert "text/csv" in res.headers["content-type"]
        assert "Client Invoice Payment" in res.text
        assert "Office Rent Payment" in res.text

    def test_export_excel_with_bearer_header(self, client):
        headers, token, user_id = register_and_login_user(client, "exp_xls_h")
        seed_transactions_for_user(user_id)

        res = client.get("/reports/export/excel", headers=headers)
        assert res.status_code == 200
        assert "spreadsheetml" in res.headers["content-type"]
        assert len(res.content) > 0

    def test_export_pdf_with_bearer_header(self, client):
        headers, token, user_id = register_and_login_user(client, "exp_pdf_h")
        seed_transactions_for_user(user_id)

        res = client.get("/reports/export/pdf", headers=headers)
        assert res.status_code == 200
        assert "application/pdf" in res.headers["content-type"]
        assert b"FINANCIAL TRANSACTIONS REPORT" in res.content

    def test_export_treasury_with_bearer_header(self, client):
        headers, token, user_id = register_and_login_user(client, "exp_trs_h")
        seed_transactions_for_user(user_id)

        res = client.get("/reports/export/treasury", headers=headers)
        assert res.status_code == 200
        assert "text/csv" in res.headers["content-type"]
        assert "TREASURY OVERVIEW" in res.text

    def test_export_endpoints_with_query_token(self, client):
        """Verify direct browser link exports with ?token=... succeed without 401."""
        headers, token, user_id = register_and_login_user(client, "exp_token_q")
        seed_transactions_for_user(user_id)

        # 1. CSV
        res_csv = client.get(f"/reports/export/csv?token={token}")
        assert res_csv.status_code == 200
        assert "text/csv" in res_csv.headers["content-type"]

        # 2. Excel
        res_xls = client.get(f"/reports/export/excel?token={token}")
        assert res_xls.status_code == 200
        assert "spreadsheetml" in res_xls.headers["content-type"]

        # 3. PDF
        res_pdf = client.get(f"/reports/export/pdf?token={token}")
        assert res_pdf.status_code == 200
        assert "application/pdf" in res_pdf.headers["content-type"]

        # 4. Treasury
        res_trs = client.get(f"/reports/export/treasury?token={token}")
        assert res_trs.status_code == 200
        assert "text/csv" in res_trs.headers["content-type"]

    def test_export_endpoints_without_auth_return_401(self, client):
        """Unauthenticated requests must be rejected with 401."""
        assert client.get("/reports/export/csv").status_code == 401
        assert client.get("/reports/export/excel").status_code == 401
        assert client.get("/reports/export/pdf").status_code == 401
        assert client.get("/reports/export/treasury").status_code == 401

    def test_export_filtered_by_dates(self, client):
        headers, token, user_id = register_and_login_user(client, "exp_filter")
        seed_transactions_for_user(user_id)

        # Filter only August 1 to August 3 (includes Invoice, excludes Rent)
        res = client.get("/reports/export/csv?start_date=2026-08-01&end_date=2026-08-03", headers=headers)
        assert res.status_code == 200
        assert "Client Invoice Payment" in res.text
        assert "Office Rent Payment" not in res.text
