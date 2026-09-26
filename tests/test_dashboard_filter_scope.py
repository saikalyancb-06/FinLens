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


def setup_user_master_data(user_id_str: str):
    """
    Creates 2 entities for the user:
      - Entity 1: KREDO_TECH with 2 accounts (HDFC_1, ICICI_1)
      - Entity 2: KREDO_RETAIL with 1 account (SBI_1)
    Total: 2 entities, 3 accounts.
    Transactions only seeded on Entity 1, Account 1 (HDFC_1).
    Entity 2 has 0 transactions.
    """
    db: Session = next(get_db())
    u_uuid = uuid.UUID(user_id_str)

    ent1 = Entity(id=uuid.uuid4(), user_id=u_uuid, name="KREDO TECH")
    ent2 = Entity(id=uuid.uuid4(), user_id=u_uuid, name="KREDO RETAIL")
    db.add_all([ent1, ent2])
    db.flush()

    acc1 = Account(
        id=uuid.uuid4(),
        user_id=u_uuid,
        entity_id=ent1.id,
        bank_code="HDFC",
        account_number_masked="XXXX1111",
        account_type="CURRENT",
        currency="INR"
    )
    acc2 = Account(
        id=uuid.uuid4(),
        user_id=u_uuid,
        entity_id=ent1.id,
        bank_code="ICICI",
        account_number_masked="XXXX2222",
        account_type="CURRENT",
        currency="INR"
    )
    acc3 = Account(
        id=uuid.uuid4(),
        user_id=u_uuid,
        entity_id=ent2.id,
        bank_code="SBI",
        account_number_masked="XXXX3333",
        account_type="CURRENT",
        currency="INR"
    )
    db.add_all([acc1, acc2, acc3])
    db.flush()

    # Seed transaction only on acc1
    tx1 = Transaction(
        user_id=u_uuid,
        entity_id=ent1.id,
        account_id=acc1.id,
        txn_date=datetime.date(2026, 8, 1),
        narration_raw="Client Payment",
        credit_paise=10000000, # 100,000 INR
        direction=Direction.CREDIT,
        source_channel="STATEMENT"
    )
    db.add(tx1)
    ent1_id = ent1.id
    ent2_id = ent2.id
    acc1_id = acc1.id
    acc2_id = acc2.id
    acc3_id = acc3.id

    db.commit()

    return {
        "ent1_id": ent1_id,
        "ent2_id": ent2_id,
        "acc1_id": acc1_id,
        "acc2_id": acc2_id,
        "acc3_id": acc3_id
    }


class TestDashboardFilterScope:
    """Test suite ensuring dashboard metrics distinguish between Filter Scope and Transaction Result Set."""

    def test_no_filters_returns_full_scope(self, client):
        headers, token, user_id = register_and_login_user(client, "scope_full")
        setup_user_master_data(user_id)

        res = client.get("/dashboard/summary", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["entities_count"] == 2
        assert data["accounts_count"] == 3
        assert data["consolidated_liquidity"] == 100000.0
        assert data["total_transactions"] == 1

    def test_entity_with_zero_transactions_retains_scope(self, client):
        """
        When Entity 2 (KREDO RETAIL with 0 transactions) is selected:
        - entities_count should be 1
        - accounts_count should be 1 (SBI_1)
        - consolidated_liquidity should be 0.00
        - total_transactions should be 0
        """
        headers, token, user_id = register_and_login_user(client, "scope_ent2")
        fixture = setup_user_master_data(user_id)

        ent2_id = str(fixture["ent2_id"])
        res = client.get(f"/dashboard/summary?entity_id={ent2_id}", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["entities_count"] == 1
        assert data["accounts_count"] == 1
        assert data["consolidated_liquidity"] == 0.0
        assert data["total_transactions"] == 0

    def test_entity_with_multiple_accounts_returns_correct_account_scope(self, client):
        """
        When Entity 1 (KREDO TECH with 2 accounts) is selected:
        - entities_count should be 1
        - accounts_count should be 2
        - consolidated_liquidity should reflect transactions
        """
        headers, token, user_id = register_and_login_user(client, "scope_ent1")
        fixture = setup_user_master_data(user_id)

        ent1_id = str(fixture["ent1_id"])
        res = client.get(f"/dashboard/summary?entity_id={ent1_id}", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["entities_count"] == 1
        assert data["accounts_count"] == 2
        assert data["consolidated_liquidity"] == 100000.0
        assert data["total_transactions"] == 1

    def test_bank_account_filter_with_zero_transactions(self, client):
        """
        When acc2 (ICICI_1 with 0 transactions) is selected:
        - entities_count should be 1
        - accounts_count should be 1
        - consolidated_liquidity should be 0.00
        """
        headers, token, user_id = register_and_login_user(client, "scope_acc2")
        fixture = setup_user_master_data(user_id)

        acc2_id = str(fixture["acc2_id"])
        res = client.get(f"/dashboard/summary?bank_id={acc2_id}", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["entities_count"] == 1
        assert data["accounts_count"] == 1
        assert data["consolidated_liquidity"] == 0.0
        assert data["total_transactions"] == 0

    def test_date_range_filter_with_zero_matching_transactions(self, client):
        """
        When a date range with no transactions is queried without entity filters:
        - entities_count should be 2 (full scope)
        - accounts_count should be 3 (full scope)
        - metrics should be 0
        """
        headers, token, user_id = register_and_login_user(client, "scope_dates")
        setup_user_master_data(user_id)

        res = client.get("/dashboard/summary?from_date=2025-01-01&to_date=2025-01-31", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["entities_count"] == 2
        assert data["accounts_count"] == 3
        assert data["total_transactions"] == 0
        assert data["total_credit"] == 0.0
        assert data["total_debit"] == 0.0
