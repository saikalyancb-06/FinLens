"""
test_canonical_migration.py
============================
Verifies that the Dashboard, Analytics, and Reports endpoints now query
exclusively from the canonical Transaction table (not ProcessedTransaction).

Tests cover:
  - Empty database returns zero-valued / empty responses
  - Correct paise → rupees conversion
  - User isolation (IDOR protection)
  - Date filtering
  - Export endpoints (CSV / Excel / PDF)
  - Data drift: ProcessedTransaction rows do NOT inflate dashboard numbers
"""

import uuid
import pytest
from datetime import date, datetime
from fastapi.testclient import TestClient

from main import app
from app.database.session import get_db
from app.models.transaction import Transaction, Direction
from app.models.processed_transaction import ProcessedTransaction
from app.models.user import User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(db, email=None):
    u = User(
        id=uuid.uuid4(),
        email=email or f"user_{uuid.uuid4().hex[:6]}@test.com",
        full_name="Test User",
        hashed_password="$2b$12$fakehashfakehashfakehashfake",
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _make_txn(db, user_id, *, txn_date=None, debit_paise=0, credit_paise=0,
               narration_raw="Test Txn", direction=None):
    if direction is None:
        direction = Direction.DEBIT if debit_paise > 0 else Direction.CREDIT
    t = Transaction(
        id=uuid.uuid4(),
        user_id=user_id,
        direction=direction,
        debit_paise=debit_paise or None,
        credit_paise=credit_paise or None,
        narration_raw=narration_raw,
        txn_date=txn_date or date(2025, 1, 15),
        source_type="statement",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(t)
    return t


def _make_pt(db, user_id, *, txn_date=None, debit=500.0, credit=0.0,
              description="PT Txn"):
    """Create a ProcessedTransaction that must NOT affect canonical endpoints."""
    pt = ProcessedTransaction(
        id=uuid.uuid4(),
        user_id=user_id,
        date=datetime.combine(txn_date or date(2025, 1, 15), datetime.min.time()),
        description=description,
        debit=debit,
        credit=credit,
        amount=max(debit, credit),
        balance=0.0,
        transaction_type="debit" if debit > 0 else "credit",
        final_category="Uncategorized",
        confidence=0.9,
        prediction_source="Rule Engine",
        model_version="v1.0.0",
        processing_timestamp=datetime.utcnow(),
    )
    db.add(pt)
    return pt


def _register_and_login(client: TestClient, email="user@example.com", password="pass1234"):
    """Register (ignore 400 if already registered) then login with JSON body."""
    client.post("/auth/register", json={
        "email": email,
        "full_name": "Test User",
        "password": password
    })
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _get_test_db():
    """Get a session from the test DB override."""
    return next(app.dependency_overrides[get_db]())


# ===========================================================================
# 1. DASHBOARD SUMMARY
# ===========================================================================

class TestDashboardSummary:
    def test_empty_db_returns_zeros(self, client):
        hdrs = _register_and_login(client, "dash_empty@test.com")
        resp = client.get("/dashboard/summary", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_transactions"] == 0
        assert data["total_debit"] == 0.0
        assert data["total_credit"] == 0.0
        assert data["net_cash_flow"] == 0.0

    def test_paise_to_rupees_conversion(self, client):
        """₹100.50 = 10050 paise → API must return 100.5"""
        hdrs = _register_and_login(client, "dash_conv@test.com")

        db = _get_test_db()
        try:
            user = db.query(User).filter(User.email == "dash_conv@test.com").first()
            _make_txn(db, user.id, debit_paise=10050)   # ₹100.50
            _make_txn(db, user.id, credit_paise=20000)  # ₹200.00
            db.commit()
        finally:
            db.close()

        resp = client.get("/dashboard/summary", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_transactions"] == 2
        assert data["total_debit"]   == pytest.approx(100.50)
        assert data["total_credit"]  == pytest.approx(200.00)
        assert data["net_cash_flow"] == pytest.approx(99.50)

    def test_processed_transactions_do_not_inflate_dashboard(self, client):
        """ProcessedTransaction rows must NOT be counted in the canonical dashboard."""
        hdrs = _register_and_login(client, "dash_isolation@test.com")

        db = _get_test_db()
        try:
            user = db.query(User).filter(User.email == "dash_isolation@test.com").first()
            # Only add a ProcessedTransaction — NO canonical Transaction
            _make_pt(db, user.id, debit=9999.0)
            db.commit()
        finally:
            db.close()

        resp = client.get("/dashboard/summary", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_transactions"] == 0
        assert data["total_debit"] == 0.0

    def test_superseded_txns_excluded(self, client):
        """Superseded (duplicate) transactions must be excluded."""
        hdrs = _register_and_login(client, "dash_superseded@test.com")

        db = _get_test_db()
        try:
            user = db.query(User).filter(User.email == "dash_superseded@test.com").first()
            t1 = _make_txn(db, user.id, debit_paise=5000)
            db.flush()
            t2 = _make_txn(db, user.id, debit_paise=5000)
            db.flush()
            t2.superseded_by_id = t1.id
            db.commit()
        finally:
            db.close()

        resp = client.get("/dashboard/summary", headers=hdrs)
        data = resp.json()
        assert data["total_transactions"] == 1
        assert data["total_debit"] == pytest.approx(50.0)


# ===========================================================================
# 2. USER ISOLATION
# ===========================================================================

class TestUserIsolation:
    def test_dashboard_user_isolation(self, client):
        """User A's transactions must not appear on User B's dashboard."""
        hdrs_a = _register_and_login(client, "iso_a@test.com")
        hdrs_b = _register_and_login(client, "iso_b@test.com")

        db = _get_test_db()
        try:
            user_a = db.query(User).filter(User.email == "iso_a@test.com").first()
            for _ in range(3):
                _make_txn(db, user_a.id, debit_paise=100000)
            db.commit()
        finally:
            db.close()

        resp_b = client.get("/dashboard/summary", headers=hdrs_b)
        data_b = resp_b.json()
        assert data_b["total_transactions"] == 0
        assert data_b["total_debit"] == 0.0

        resp_a = client.get("/dashboard/summary", headers=hdrs_a)
        data_a = resp_a.json()
        assert data_a["total_transactions"] == 3

    def test_transactions_list_user_isolation(self, client):
        hdrs_a = _register_and_login(client, "txiso_a@test.com")
        hdrs_b = _register_and_login(client, "txiso_b@test.com")

        db = _get_test_db()
        try:
            user_a = db.query(User).filter(User.email == "txiso_a@test.com").first()
            _make_txn(db, user_a.id, debit_paise=50000, narration_raw="ONLY USER A TXN")
            db.commit()
        finally:
            db.close()

        resp_b = client.get("/transactions", headers=hdrs_b)
        assert resp_b.status_code == 200
        assert len(resp_b.json()) == 0


# ===========================================================================
# 3. TRANSACTIONS LIST — FILTERS
# ===========================================================================

class TestTransactionsList:
    def test_date_filter(self, client):
        hdrs = _register_and_login(client, "tx_dates@test.com")

        db = _get_test_db()
        try:
            user = db.query(User).filter(User.email == "tx_dates@test.com").first()
            _make_txn(db, user.id, debit_paise=1000, txn_date=date(2025, 1, 10))
            _make_txn(db, user.id, debit_paise=2000, txn_date=date(2025, 2, 20))
            _make_txn(db, user.id, debit_paise=3000, txn_date=date(2025, 3, 15))
            db.commit()
        finally:
            db.close()

        resp = client.get("/transactions?start_date=2025-02-01&end_date=2025-02-28", headers=hdrs)
        assert resp.status_code == 200
        result = resp.json()
        assert len(result) == 1
        assert result[0]["debit"] == pytest.approx(20.0)

    def test_search_filter(self, client):
        hdrs = _register_and_login(client, "tx_search@test.com")

        db = _get_test_db()
        try:
            user = db.query(User).filter(User.email == "tx_search@test.com").first()
            _make_txn(db, user.id, debit_paise=1000, narration_raw="NEFT TO AMAZON")
            _make_txn(db, user.id, credit_paise=5000, narration_raw="SALARY CREDIT")
            db.commit()
        finally:
            db.close()

        resp = client.get("/transactions?search=amazon", headers=hdrs)
        result = resp.json()
        assert len(result) == 1
        assert "AMAZON" in result[0]["original_raw_text"].upper()


# ===========================================================================
# 4. ANALYTICS ENDPOINTS
# ===========================================================================

class TestAnalytics:
    def _seed_user(self, client, email, txns_spec):
        hdrs = _register_and_login(client, email)
        db = _get_test_db()
        try:
            user = db.query(User).filter(User.email == email).first()
            for spec in txns_spec:
                _make_txn(db, user.id, **spec)
            db.commit()
        finally:
            db.close()
        return hdrs

    def test_monthly_summary_paise_arithmetic(self, client):
        hdrs = self._seed_user(client, "monthly_a@test.com", [
            {"debit_paise": 100000, "txn_date": date(2025, 1, 5)},   # ₹1000
            {"credit_paise": 50000, "txn_date": date(2025, 1, 20)},  # ₹500
            {"debit_paise": 200000, "txn_date": date(2025, 2, 3)},   # ₹2000
        ])
        resp = client.get("/analytics/monthly-summary", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()

        by_month = {m["month"]: m for m in data}
        assert "2025-01" in by_month
        assert by_month["2025-01"]["total_debit"]   == pytest.approx(1000.0)
        assert by_month["2025-01"]["total_credit"]  == pytest.approx(500.0)
        assert by_month["2025-01"]["net_cash_flow"] == pytest.approx(-500.0)
        assert by_month["2025-01"]["transaction_count"] == 2

        assert "2025-02" in by_month
        assert by_month["2025-02"]["total_debit"] == pytest.approx(2000.0)

    def test_cash_flow_empty(self, client):
        hdrs = _register_and_login(client, "cf_empty@test.com")
        resp = client.get("/analytics/cash-flow", headers=hdrs)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_category_breakdown_uncategorized(self, client):
        hdrs = self._seed_user(client, "cat_break@test.com", [
            {"debit_paise": 30000},
            {"debit_paise": 70000},
        ])
        resp = client.get("/analytics/category-breakdown", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()
        assert any(item["category"] == "Uncategorized" for item in data)

    def test_filters_metadata(self, client):
        hdrs = _register_and_login(client, "filters_md@test.com")
        resp = client.get("/analytics/filters", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()
        assert "transaction_types" in data
        assert set(data["transaction_types"]) == {"debit", "credit"}


# ===========================================================================
# 5. REPORTS ENDPOINTS
# ===========================================================================

class TestReports:
    def _seed_and_login(self, client, email, month=1, year=2025):
        hdrs = _register_and_login(client, email)
        db = _get_test_db()
        try:
            user = db.query(User).filter(User.email == email).first()
            _make_txn(db, user.id, debit_paise=150000, txn_date=date(year, month, 10))
            _make_txn(db, user.id, credit_paise=80000,  txn_date=date(year, month, 20))
            db.commit()
        finally:
            db.close()
        return hdrs

    def test_monthly_report(self, client):
        hdrs = self._seed_and_login(client, "rep_monthly@test.com", month=3, year=2025)
        resp = client.get("/reports/monthly?year=2025&month=3", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()
        assert data["transaction_count"] == 2
        assert data["total_debit"]   == pytest.approx(1500.0)
        assert data["total_credit"]  == pytest.approx(800.0)
        assert data["net_cash_flow"] == pytest.approx(-700.0)

    def test_yearly_report(self, client):
        hdrs = self._seed_and_login(client, "rep_yearly@test.com", month=5, year=2025)
        resp = client.get("/reports/yearly?year=2025", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()
        assert data["year"] == 2025
        assert data["transaction_count"] == 2

    def test_expense_report(self, client):
        hdrs = self._seed_and_login(client, "rep_expense@test.com", month=6, year=2025)
        resp = client.get("/reports/expense", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["total_expense"] == pytest.approx(1500.0)

    def test_income_report(self, client):
        hdrs = self._seed_and_login(client, "rep_income@test.com", month=7, year=2025)
        resp = client.get("/reports/income", headers=hdrs)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["total_income"] == pytest.approx(800.0)


# ===========================================================================
# 6. EXPORT ENDPOINTS
# ===========================================================================

class TestExports:
    def _seed_and_login(self, client, email):
        hdrs = _register_and_login(client, email)
        db = _get_test_db()
        try:
            user = db.query(User).filter(User.email == email).first()
            _make_txn(db, user.id, debit_paise=12345, narration_raw="RENT PAYMENT")
            db.commit()
        finally:
            db.close()
        return hdrs

    def test_export_csv_contains_data(self, client):
        hdrs = self._seed_and_login(client, "exp_csv@test.com")
        resp = client.get("/reports/export/csv", headers=hdrs)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        content = resp.content.decode("utf-8")
        assert "RENT PAYMENT" in content
        assert "Date" in content
        assert "Description" in content

    def test_export_excel_returns_xlsx(self, client):
        hdrs = self._seed_and_login(client, "exp_xlsx@test.com")
        resp = client.get("/reports/export/excel", headers=hdrs)
        assert resp.status_code == 200
        assert "spreadsheetml" in resp.headers["content-type"]
        # xlsx magic bytes: PK (zip)
        assert resp.content[:2] == b"PK"

    def test_export_pdf_returns_text(self, client):
        hdrs = self._seed_and_login(client, "exp_pdf@test.com")
        resp = client.get("/reports/export/pdf", headers=hdrs)
        assert resp.status_code == 200
        content = resp.content.decode("utf-8")
        assert "FINANCIAL TRANSACTIONS REPORT" in content
        assert "RENT PAYMENT" in content

    def test_export_csv_empty_user(self, client):
        hdrs = _register_and_login(client, "exp_csv_empty@test.com")
        resp = client.get("/reports/export/csv", headers=hdrs)
        assert resp.status_code == 200
        content = resp.content.decode("utf-8")
        lines = [l for l in content.strip().split("\n") if l]
        # Only header row, no data rows
        assert len(lines) == 1


# ===========================================================================
# 7. RECENT TRANSACTIONS
# ===========================================================================

class TestRecentTransactions:
    def test_recent_transactions_order(self, client):
        """Most recent date must come first."""
        hdrs = _register_and_login(client, "recent_order@test.com")
        db = _get_test_db()
        try:
            user = db.query(User).filter(User.email == "recent_order@test.com").first()
            _make_txn(db, user.id, debit_paise=1000, txn_date=date(2025, 1, 1))  # ₹10 Jan
            _make_txn(db, user.id, debit_paise=2000, txn_date=date(2025, 3, 1))  # ₹20 Mar  ← most recent
            _make_txn(db, user.id, debit_paise=3000, txn_date=date(2025, 2, 1))  # ₹30 Feb
            db.commit()
        finally:
            db.close()

        resp = client.get("/analytics/recent-transactions?limit=3", headers=hdrs)
        assert resp.status_code == 200
        result = resp.json()
        assert len(result) == 3
        # Most recent first: March (₹20) > February (₹30) > January (₹10)
        assert result[0]["debit"] == pytest.approx(20.0)  # March
        assert result[1]["debit"] == pytest.approx(30.0)  # February
        assert result[2]["debit"] == pytest.approx(10.0)  # January
