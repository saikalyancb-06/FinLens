import pytest
import datetime
import uuid
import csv
import io
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.user import User
from app.models.entity import Entity
from app.models.account import Account
from app.models.transaction import Transaction, Direction
from app.models.reconciliation import ReconciliationRun


def register_and_login(client: TestClient, email: str, password: str = "StrongPass123!") -> dict:
    """Helper to register and log in a user."""
    client.post("/auth/register", json={
        "email": email,
        "password": password,
        "full_name": "Treasury Test User",
        "role": "treasurer"
    })
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"Login failed for {email}: {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestPhase11TreasuryOverview:
    """Phase 11 — Comprehensive Treasury Overview Report & End-to-End Audit Suite."""

    def test_treasury_endpoint_exists(self, client: TestClient):
        """GET /reports/treasury returns 200 with structured group-level overview."""
        headers = register_and_login(client, "treasury_exist@phase11.test")
        r = client.get("/reports/treasury", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["report_type"] == "Treasury Overview - Group Level"
        assert "group_totals" in data
        assert "entities" in data

    def test_treasury_deterministic_golden_dataset(self, client: TestClient):
        """Verify Treasury calculation equations hold exactly over a golden dataset:
        Closing = Opening + Net Flow (Inflows - Outflows).
        """
        headers = register_and_login(client, "golden_treasury@phase11.test")

        # Create two distinct Entities via bank-master API
        r_ent1 = client.post("/v1/bank-master/entities", headers=headers, json={"name": "Entity A"})
        ent1_id = r_ent1.json()["id"]

        r_ent2 = client.post("/v1/bank-master/entities", headers=headers, json={"name": "Entity B"})
        ent2_id = r_ent2.json()["id"]

        # Create Bank Accounts linked to Entities
        r_acc1 = client.post("/v1/bank-master/accounts", headers=headers, json={
            "entity_id": ent1_id,
            "bank_code": "HDFC",
            "account_number": "111122223333",
            "account_type": "CURRENT",
            "currency": "INR"
        })
        acc1_id = r_acc1.json()["id"]

        r_acc2 = client.post("/v1/bank-master/accounts", headers=headers, json={
            "entity_id": ent2_id,
            "bank_code": "ICICI",
            "account_number": "444455556666",
            "account_type": "CURRENT",
            "currency": "INR"
        })
        acc2_id = r_acc2.json()["id"]

        # Set min balance threshold on Acc1
        db: Session = next(get_db())
        acc1_db = db.query(Account).filter(Account.id == uuid.UUID(acc1_id)).first()
        acc1_db.min_balance_paise = 500000  # ₹5,000 threshold
        db.commit()

        # Seed transactions for Entity A (Acc1)
        # Prior transaction (opening balance: ₹10,000)
        t_prior = Transaction(
            id=uuid.uuid4(),
            user_id=acc1_db.user_id,
            account_id=acc1_db.id,
            entity_id=uuid.UUID(ent1_id),
            direction=Direction.CREDIT,
            credit_paise=1000000,
            txn_date=datetime.date(2026, 1, 1),
            narration_raw="Opening Capital"
        )
        # In-period inflow (₹5,000)
        t_in = Transaction(
            id=uuid.uuid4(),
            user_id=acc1_db.user_id,
            account_id=acc1_db.id,
            entity_id=uuid.UUID(ent1_id),
            direction=Direction.CREDIT,
            credit_paise=500000,
            txn_date=datetime.date(2026, 1, 15),
            narration_raw="Customer Collection"
        )
        # In-period outflow (₹8,000, balance drops to ₹7,000)
        t_out = Transaction(
            id=uuid.uuid4(),
            user_id=acc1_db.user_id,
            account_id=acc1_db.id,
            entity_id=uuid.UUID(ent1_id),
            direction=Direction.DEBIT,
            debit_paise=800000,
            balance_paise=400000,  # ₹4,000 < ₹5,000 threshold -> triggers 1 breach
            txn_date=datetime.date(2026, 1, 20),
            narration_raw="Vendor Payment"
        )
        db.add_all([t_prior, t_in, t_out])
        db.commit()

        # Query Treasury Overview API for period 2026-01-10 to 2026-01-31
        r_rep = client.get("/reports/treasury?start_date=2026-01-10&end_date=2026-01-31", headers=headers)
        assert r_rep.status_code == 200
        data = r_rep.json()

        entities = data["entities"]
        ent_a = next(e for e in entities if e["entity_name"] == "Entity A")

        # Verification of Entity A calculations:
        # Opening: ₹10,000.00
        # Inflows: ₹5,000.00
        # Outflows: ₹8,000.00
        # Net Flow: -₹3,000.00
        # Closing: ₹7,000.00
        assert ent_a["opening_balance"] == 10000.0
        assert ent_a["total_inflows"] == 5000.0
        assert ent_a["total_outflows"] == 8000.0
        assert ent_a["net_flow"] == -3000.0
        assert ent_a["closing_balance"] == 7000.0
        assert ent_a["min_balance_breaches"] == 1
        assert ent_a["uncategorized_count"] == 2

        # Verify group totals reconcile with entities sum
        gt = data["group_totals"]
        assert gt["opening_balance"] == sum(e["opening_balance"] for e in entities)
        assert gt["total_inflows"] == sum(e["total_inflows"] for e in entities)
        assert gt["total_outflows"] == sum(e["total_outflows"] for e in entities)
        assert round(gt["net_flow"], 2) == round(gt["total_inflows"] - gt["total_outflows"], 2)
        assert round(gt["closing_balance"], 2) == round(gt["opening_balance"] + gt["net_flow"], 2)

    def test_treasury_csv_export(self, client: TestClient):
        """GET /reports/export/treasury exports valid CSV matching API data."""
        headers = register_and_login(client, "treasury_csv@phase11.test")
        r = client.get("/reports/export/treasury?start_date=2026-01-01&end_date=2026-01-31", headers=headers)
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "attachment; filename=treasury_overview_report.csv" in r.headers["content-disposition"]

        csv_text = r.text
        assert "TREASURY OVERVIEW - GROUP LEVEL" in csv_text
        assert "Opening Balance" in csv_text
        assert "Total Inflows" in csv_text
        assert "Total Outflows" in csv_text
        assert "Net Flow" in csv_text
        assert "Closing Balance" in csv_text

    def test_treasury_user_isolation(self, client: TestClient):
        """User B cannot access or view User A's Treasury Overview data or export."""
        headers_a = register_and_login(client, "user_a_treasury@phase11.test")
        headers_b = register_and_login(client, "user_b_treasury@phase11.test")

        # Seed Entity for User A
        r_ent_a = client.post("/v1/bank-master/entities", headers=headers_a, json={"name": "Secret Entity A"})
        ent_a_id = r_ent_a.json()["id"]

        # User B queries Treasury Overview
        r_b = client.get("/reports/treasury", headers=headers_b)
        assert r_b.status_code == 200
        data_b = r_b.json()

        # User B should not see Secret Entity A
        entity_names_b = [e["entity_name"] for e in data_b["entities"]]
        assert "Secret Entity A" not in entity_names_b

        # User B filtering with User A's entity_id returns empty/isolated result
        r_b_filt = client.get(f"/reports/treasury?entity_id={ent_a_id}", headers=headers_b)
        assert r_b_filt.status_code == 200
        assert len(r_b_filt.json()["entities"]) == 1
        assert r_b_filt.json()["entities"][0]["entity_name"] == "Default Entity"

    def test_phase10_reconciliation_immutability_unaffected(self, client: TestClient):
        """Treasury Overview queries do NOT mutate or alter Phase 10 immutable reconciliation runs."""
        headers = register_and_login(client, "immut_check@phase11.test")

        # Create account
        client.post("/v1/bank-master/accounts", headers=headers, json={
            "bank_code": "HDFC",
            "account_number": "999900001111",
            "account_type": "CURRENT"
        })
        client.post("/v1/reconciliation/imports/confirm", headers=headers, json={
            "column_mapping": {"date": "Date", "narration": "Narration", "money_in": "Debit", "money_out": "Credit"},
            "rows": [{"Date": "2026-01-10", "Narration": "Test Row", "Debit": "1000.00", "Credit": "0.00"}]
        })

        # Run reconciliation
        r_run = client.post("/v1/reconciliation/runs", json={"book_opening": "0", "period_from": "2026-01-01", "period_to": "2026-01-31", "force": True}, headers=headers)
        run_id = r_run.json()["run_id"]

        # Query Treasury Overview
        client.get("/reports/treasury", headers=headers)
        client.get("/reports/export/treasury", headers=headers)

        # Verify historical run remains intact
        r_get = client.get(f"/v1/reconciliation/runs/{run_id}", headers=headers)
        assert r_get.status_code == 200
        assert r_get.json()["id"] == run_id
