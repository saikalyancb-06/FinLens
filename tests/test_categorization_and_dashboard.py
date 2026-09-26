import pytest
import uuid
import datetime
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from main import app
from app.database.session import get_db
from app.models.transaction import Transaction, Direction, SourceType
from app.models.category import Category
from app.models.prediction import Prediction
from app.services.category_seeder import seed_categories, resolve_transaction_categories

def get_auth_headers(client: TestClient, email: str = "phase5_user@example.com") -> dict:
    password = "TestPassword123!"
    client.post("/auth/register", json={"email": email, "password": password})
    res = client.post("/auth/login", json={"email": email, "password": password})
    token = res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_1_category_seeding(client: TestClient):
    """Test that seed_categories seeds canonical taxonomy into categories table."""
    db_gen = client.app.dependency_overrides.get(get_db, get_db)()
    db: Session = next(db_gen)

    cat_map = seed_categories(db)
    assert len(cat_map) > 10
    assert "food & dining" in cat_map
    assert "salary payment" in cat_map
    assert "bank charges" in cat_map


def test_2_prediction_to_category_resolution(client: TestClient):
    """Test resolving Prediction.predicted_category into Transaction.category_id."""
    headers = get_auth_headers(client, "tx_user_pred_res@example.com")

    # Uploads require at least one registered Bank Master account.
    from conftest import register_bank_account
    register_bank_account(client, headers)
    db_gen = client.app.dependency_overrides.get(get_db, get_db)()
    db: Session = next(db_gen)

    me_res = client.get("/auth/me", headers=headers).json()
    user_id = uuid.UUID(me_res["id"])

    # Seed categories
    cat_map = seed_categories(db)
    food_cat_id = cat_map["food & dining"]

    # Create transaction with category_id = None
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user_id,
        direction=Direction.DEBIT,
        debit_paise=50000, # Rs 500
        credit_paise=0,
        txn_date=datetime.date.today(),
        narration_raw="SWIGGY FOOD ORDER",
        narration_clean="SWIGGY FOOD ORDER",
        category_id=None
    )
    db.add(tx)
    db.commit()

    # Create linked prediction
    pred = Prediction(
        id=uuid.uuid4(),
        transaction_id=tx.id,
        predicted_category="Food & Dining",
        confidence=0.98,
        rule_used="rule_9"
    )
    db.add(pred)
    db.commit()

    # Run category resolution
    resolved = resolve_transaction_categories(db)
    assert resolved >= 1

    db.refresh(tx)
    assert tx.category_id == food_cat_id


def test_3_uncategorized_count(client: TestClient):
    """Test uncategorized count calculation in GET /dashboard/summary."""
    headers = get_auth_headers(client, "tx_user_uncat_count@example.com")
    db_gen = client.app.dependency_overrides.get(get_db, get_db)()
    db: Session = next(db_gen)

    me_res = client.get("/auth/me", headers=headers).json()
    user_id = uuid.UUID(me_res["id"])

    # Add 1 categorized and 1 uncategorized transaction
    cat_map = seed_categories(db)
    
    tx_cat = Transaction(
        id=uuid.uuid4(), user_id=user_id, direction=Direction.CREDIT,
        debit_paise=0, credit_paise=100000, txn_date=datetime.date.today(),
        narration_raw="SALARY", category_id=cat_map["salary payment"]
    )
    tx_uncat = Transaction(
        id=uuid.uuid4(), user_id=user_id, direction=Direction.DEBIT,
        debit_paise=25000, credit_paise=0, txn_date=datetime.date.today(),
        narration_raw="UNKNOWN EXPENSE", category_id=None
    )
    db.add_all([tx_cat, tx_uncat])
    db.commit()

    summary_res = client.get("/dashboard/summary", headers=headers)
    assert summary_res.status_code == 200
    data = summary_res.json()
    assert data["total_transactions"] == 2
    assert data["uncategorized_count"] == 1


def test_4_mtd_cash_flow_mathematics(client: TestClient):
    """Test MTD inflow, outflow, and net cash flow calculations."""
    headers = get_auth_headers(client, "tx_user_mtd_math@example.com")
    db_gen = client.app.dependency_overrides.get(get_db, get_db)()
    db: Session = next(db_gen)

    me_res = client.get("/auth/me", headers=headers).json()
    user_id = uuid.UUID(me_res["id"])

    today = datetime.date.today()

    # Current MTD credits: 100000 (1000 INR), 250000 (2500 INR) -> Total 3500.00
    # Current MTD debits: 40000 (400 INR), 60000 (600 INR) -> Total 1000.00
    tx1 = Transaction(id=uuid.uuid4(), user_id=user_id, direction=Direction.CREDIT, debit_paise=0, credit_paise=100000, txn_date=today, narration_raw="CR 1")
    tx2 = Transaction(id=uuid.uuid4(), user_id=user_id, direction=Direction.CREDIT, debit_paise=0, credit_paise=250000, txn_date=today, narration_raw="CR 2")
    tx3 = Transaction(id=uuid.uuid4(), user_id=user_id, direction=Direction.DEBIT, debit_paise=40000, credit_paise=0, txn_date=today, narration_raw="DR 1")
    tx4 = Transaction(id=uuid.uuid4(), user_id=user_id, direction=Direction.DEBIT, debit_paise=60000, credit_paise=0, txn_date=today, narration_raw="DR 2")

    db.add_all([tx1, tx2, tx3, tx4])
    db.commit()

    res = client.get("/dashboard/summary", headers=headers)
    assert res.status_code == 200
    d = res.json()
    assert d["total_credit"] == 3500.0
    assert d["total_debit"] == 1000.0
    assert d["net_cash_flow"] == 2500.0


def test_5_previous_month_exclusion_from_mtd(client: TestClient):
    """Test that previous month transactions are excluded from MTD summary."""
    headers = get_auth_headers(client, "tx_user_prev_mth@example.com")
    db_gen = client.app.dependency_overrides.get(get_db, get_db)()
    db: Session = next(db_gen)

    me_res = client.get("/auth/me", headers=headers).json()
    user_id = uuid.UUID(me_res["id"])

    today = datetime.date.today()
    first_of_month = datetime.date(today.year, today.month, 1)
    prev_month_date = first_of_month - datetime.timedelta(days=5)

    tx_curr = Transaction(id=uuid.uuid4(), user_id=user_id, direction=Direction.CREDIT, debit_paise=0, credit_paise=50000, txn_date=today, narration_raw="CURRENT MTD")
    tx_prev = Transaction(id=uuid.uuid4(), user_id=user_id, direction=Direction.CREDIT, debit_paise=0, credit_paise=900000, txn_date=prev_month_date, narration_raw="PREV MONTH")

    db.add_all([tx_curr, tx_prev])
    db.commit()

    first_of_str = first_of_month.strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")
    res = client.get(f"/dashboard/summary?from_date={first_of_str}&to_date={today_str}", headers=headers)
    assert res.status_code == 200
    d = res.json()
    assert d["total_transactions"] == 1
    assert d["total_credit"] == 500.0  # Prev month's 900.0 excluded from MTD


def test_6_daily_vs_monthly_cash_flow_aggregation(client: TestClient):
    """Test GET /analytics/cash-flow?interval=daily vs interval=monthly."""
    headers = get_auth_headers(client, "tx_user_cf_agg@example.com")
    db_gen = client.app.dependency_overrides.get(get_db, get_db)()
    db: Session = next(db_gen)

    me_res = client.get("/auth/me", headers=headers).json()
    user_id = uuid.UUID(me_res["id"])

    d1 = datetime.date(2026, 8, 1)
    d2 = datetime.date(2026, 8, 2)

    tx1 = Transaction(id=uuid.uuid4(), user_id=user_id, direction=Direction.CREDIT, debit_paise=0, credit_paise=10000, txn_date=d1, narration_raw="D1 T1")
    tx2 = Transaction(id=uuid.uuid4(), user_id=user_id, direction=Direction.CREDIT, debit_paise=0, credit_paise=20000, txn_date=d1, narration_raw="D1 T2")
    tx3 = Transaction(id=uuid.uuid4(), user_id=user_id, direction=Direction.CREDIT, debit_paise=0, credit_paise=30000, txn_date=d2, narration_raw="D2 T1")

    db.add_all([tx1, tx2, tx3])
    db.commit()

    # Daily aggregation
    res_daily = client.get("/analytics/cash-flow?interval=daily", headers=headers)
    assert res_daily.status_code == 200
    cf_daily = res_daily.json()
    assert len(cf_daily) == 2
    assert cf_daily[0]["period"] == "2026-08-01"
    assert cf_daily[0]["inflow"] == 300.0
    assert cf_daily[1]["period"] == "2026-08-02"
    assert cf_daily[1]["inflow"] == 300.0

    # Monthly aggregation
    res_monthly = client.get("/analytics/cash-flow?interval=monthly", headers=headers)
    assert res_monthly.status_code == 200
    cf_monthly = res_monthly.json()
    assert len(cf_monthly) == 1
    assert cf_monthly[0]["period"] == "2026-08"
    assert cf_monthly[0]["inflow"] == 600.0


def test_7_category_breakdown_percentages(client: TestClient):
    """Test GET /analytics/category-breakdown percentage sum."""
    headers = get_auth_headers(client, "tx_user_cat_bk@example.com")
    db_gen = client.app.dependency_overrides.get(get_db, get_db)()
    db: Session = next(db_gen)

    me_res = client.get("/auth/me", headers=headers).json()
    user_id = uuid.UUID(me_res["id"])

    cat_map = seed_categories(db)
    tx1 = Transaction(id=uuid.uuid4(), user_id=user_id, direction=Direction.DEBIT, debit_paise=70000, credit_paise=0, txn_date=datetime.date.today(), narration_raw="FOOD", category_id=cat_map["food & dining"])
    tx2 = Transaction(id=uuid.uuid4(), user_id=user_id, direction=Direction.DEBIT, debit_paise=30000, credit_paise=0, txn_date=datetime.date.today(), narration_raw="UTIL", category_id=cat_map["utilities"])

    db.add_all([tx1, tx2])
    db.commit()

    res = client.get("/analytics/category-breakdown", headers=headers)
    assert res.status_code == 200
    cats = res.json()
    assert len(cats) == 2

    pct_sum = sum(c["percentage"] for c in cats)
    assert abs(pct_sum - 100.0) < 0.01


def test_8_empty_dataset_safeguard(client: TestClient):
    """Test dashboard metrics and charts on an empty dataset."""
    headers = get_auth_headers(client, "tx_user_empty@example.com")

    summary_res = client.get("/dashboard/summary", headers=headers)
    assert summary_res.status_code == 200
    s = summary_res.json()
    assert s["total_transactions"] == 0
    assert s["total_debit"] == 0.0
    assert s["total_credit"] == 0.0
    assert s["net_cash_flow"] == 0.0
    assert s["uncategorized_count"] == 0

    cf_res = client.get("/analytics/cash-flow?interval=daily", headers=headers)
    assert cf_res.status_code == 200
    assert cf_res.json() == []

    cat_res = client.get("/analytics/category-breakdown", headers=headers)
    assert cat_res.status_code == 200
    assert cat_res.json() == []


def test_9_report_dashboard_consistency(client: TestClient):
    """Test numerical consistency between dashboard MTD summary and monthly report."""
    headers = get_auth_headers(client, "tx_user_rpt_chk@example.com")
    db_gen = client.app.dependency_overrides.get(get_db, get_db)()
    db: Session = next(db_gen)

    me_res = client.get("/auth/me", headers=headers).json()
    user_id = uuid.UUID(me_res["id"])

    today = datetime.date.today()
    tx = Transaction(id=uuid.uuid4(), user_id=user_id, direction=Direction.CREDIT, debit_paise=0, credit_paise=123456, txn_date=today, narration_raw="CONSISTENCY TX")
    db.add(tx)
    db.commit()

    # Get Dashboard Summary for MTD
    dash_res = client.get("/dashboard/summary", headers=headers).json()

    # Get Monthly Report for current year/month
    rpt_res = client.get(f"/reports/monthly?year={today.year}&month={today.month}", headers=headers).json()

    assert dash_res["total_credit"] == rpt_res["total_credit"]
