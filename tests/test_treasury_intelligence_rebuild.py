"""Unit and integration tests for Treasury Intelligence rebuild, decision cards, and recurring forecast."""

import datetime
import uuid
import pytest
from tests.conftest import TestingSessionLocal
from app.models.account import Account
from app.models.entity import Entity
from app.models.transaction import Transaction
from app.models.user import User
from app.treasury.recurring_detector import (
    compute_30_60_90_forecast,
    detect_recurring_series,
)


@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def test_recurring_detector_cadence_and_forecast():
    """Test recurring series detection across monthly salary/rent pattern."""
    user_id = uuid.uuid4()
    account_id = uuid.uuid4()

    txns = []

    # Monthly salary outflow on ~1st of each month (4 occurrences)
    for i in range(4):
        txn_date = datetime.date(2026, 1 + i, 1)
        t = Transaction(
            id=uuid.uuid4(),
            user_id=user_id,
            account_id=account_id,
            txn_date=txn_date,
            debit_paise=5000000,  # 50k
            credit_paise=0,
            narration_clean="Salaries Payroll Disbursal",
            counterparty="Payroll",
            flow_type="outflow",
        )
        txns.append(t)

    # Monthly customer subscription inflow on ~15th of each month (4 occurrences)
    for i in range(4):
        txn_date = datetime.date(2026, 1 + i, 15)
        t = Transaction(
            id=uuid.uuid4(),
            user_id=user_id,
            account_id=account_id,
            txn_date=txn_date,
            debit_paise=0,
            credit_paise=12000000,  # 1.2L
            narration_clean="Customer SaaS Subscription",
            counterparty="Acme Corp",
            flow_type="inflow",
        )
        txns.append(t)

    ref_date = datetime.date(2026, 4, 20)
    series = detect_recurring_series(txns, ref_date=ref_date)
    assert len(series) == 2

    salary_series = next(s for s in series if s["direction"] == "outflow")
    assert salary_series["cadence"] == "monthly"
    assert salary_series["occurrences"] == 4
    assert salary_series["amount_paise"] == 5000000

    inflow_series = next(s for s in series if s["direction"] == "inflow")
    assert inflow_series["cadence"] == "monthly"
    assert inflow_series["occurrences"] == 4
    assert inflow_series["amount_paise"] == 12000000

    # Compute 30/60/90 forecast starting with 10 Lakhs closing balance
    closing_paise = 100000000  # 10 Lakhs
    forecast = compute_30_60_90_forecast(closing_paise, txns, history_days=110, ref_date=ref_date)
    assert forecast["available"] is True
    assert "horizons" in forecast
    assert forecast["horizons"]["day_30"]["expected_paise"] > closing_paise
    assert len(forecast["drivers"]) == 2


def test_treasury_overview_decision_cards_and_extended_matrix(db_session):
    """Test compute_treasury_overview includes decision cards, extended rows, and forecast."""
    from app.api.reports import compute_treasury_overview

    user = User(
        id=uuid.uuid4(),
        email=f"cfo_{uuid.uuid4().hex[:6]}@example.com",
        full_name="CFO User",
        hashed_password="pw",
    )
    db_session.add(user)

    entity = Entity(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Acme India Holdings",
        is_active=True,
    )
    db_session.add(entity)

    account = Account(
        id=uuid.uuid4(),
        user_id=user.id,
        entity_id=entity.id,
        bank_code="HDFC",
        account_number_masked="XX1234",
        account_label="Primary Operating Account",
        account_type="Current",
        currency="INR",
        min_balance_paise=10000000,  # 1 Lakh min balance threshold
    )
    db_session.add(account)
    db_session.commit()

    from app.models.transaction import Direction

    # Add transactions spanning 3 months
    # Opening on Jan 1: 5L (50,000,000 paise)
    t1 = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        entity_id=entity.id,
        txn_date=datetime.date(2026, 1, 1),
        credit_paise=50000000,
        debit_paise=0,
        balance_paise=50000000,
        flow_type="inflow",
        direction=Direction.CREDIT,
        category="Revenue",
    )
    # Jan 15: Outflow 4.5L -> balance drops to 50k (below 1L min threshold => breach!)
    t2 = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        entity_id=entity.id,
        txn_date=datetime.date(2026, 1, 15),
        credit_paise=0,
        debit_paise=45000000,
        balance_paise=5000000,  # 50,000 Rs < 100,000 Rs threshold
        flow_type="outflow",
        direction=Direction.DEBIT,
        category="Vendor Payments",
    )
    # Feb 1: Inflow 3L -> balance goes to 3.5L
    t3 = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        entity_id=entity.id,
        txn_date=datetime.date(2026, 2, 1),
        credit_paise=30000000,
        debit_paise=0,
        balance_paise=35000000,
        flow_type="inflow",
        direction=Direction.CREDIT,
        category="Uncategorized",
    )
    db_session.add_all([t1, t2, t3])
    db_session.commit()

    res = compute_treasury_overview(db_session, user.id, period="all_time")

    assert "decision_cards" in res
    dc = res["decision_cards"]
    assert "cash_runway" in dc
    assert "net_cash_flow" in dc
    assert "liquidity_alerts" in dc
    assert "overdue_receivables" in dc

    # Verify Liquidity alerts picked up the 1 min-balance breach + 1 uncategorized txn
    assert dc["liquidity_alerts"]["min_balance_breaches"] == 1
    assert dc["liquidity_alerts"]["uncategorized_count"] == 1
    assert dc["liquidity_alerts"]["total_alerts"] == 2
    assert dc["overdue_receivables"]["status"] == "Not enough data"

    # Verify extended entity matrix rows
    assert len(res["entities"]) == 1
    ent_res = res["entities"][0]
    assert ent_res["pct_of_group_cash"] == 100.0
    assert "cash_runway" in ent_res
    assert "days_of_cash" in ent_res
    assert len(ent_res["breach_details"]) == 1
    assert ent_res["breach_details"][0]["shortfall"] == 50000.0  # 100k - 50k

    # Verify Monthly Trend
    assert "monthly_trend" in res
    assert len(res["monthly_trend"]) == 12

    # Verify Forecast
    assert "forecast" in res

def test_treasury_csv_export(client, db_session):
    """Test export_treasury_csv outputs new metrics and account sub-columns."""
    # Register and login a user via API
    email = f"cfo_csv_{uuid.uuid4().hex[:6]}@example.com"
    password = "Password123!"
    reg_res = client.post("/auth/register", json={"email": email, "password": password})
    assert reg_res.status_code == 201
    user_id_str = reg_res.json()["id"]

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    u_uuid = uuid.UUID(user_id_str)
    entity = Entity(
        id=uuid.uuid4(),
        user_id=u_uuid,
        name="Acme Tech India",
        is_active=True,
    )
    db_session.add(entity)

    account = Account(
        id=uuid.uuid4(),
        user_id=u_uuid,
        entity_id=entity.id,
        bank_code="ICICI",
        account_number_masked="XX9988",
        account_label="ICICI Main Ops",
        account_type="Current",
        currency="INR",
        min_balance_paise=5000000,
    )
    db_session.add(account)
    db_session.commit()

    from app.models.transaction import Direction
    t1 = Transaction(
        id=uuid.uuid4(),
        user_id=u_uuid,
        account_id=account.id,
        entity_id=entity.id,
        txn_date=datetime.date(2026, 3, 1),
        credit_paise=20000000,
        debit_paise=0,
        balance_paise=20000000,
        flow_type="inflow",
        direction=Direction.CREDIT,
        category="Revenue",
    )
    db_session.add(t1)
    db_session.commit()

    res = client.get("/reports/export/treasury?period=all_time", headers=headers)
    assert res.status_code == 200
    content = res.text

    assert "TREASURY OVERVIEW - GROUP LEVEL" in content
    assert "% of Group Cash" in content
    assert "Cash Runway" in content
    assert "Days of Cash on Hand" in content
    assert "ICICI Main Ops" in content
    assert "Acme Tech India" in content
