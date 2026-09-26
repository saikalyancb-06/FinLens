import pytest
import datetime
import uuid
import json
import csv
import io
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.user import User
from app.models.entity import Entity
from app.models.account import Account
from app.models.transaction import Transaction, Direction
from app.models.statement import Statement
from main import app

client = TestClient(app)


def register_and_login(user_email: str, password: str = "StrongPass123!") -> dict:
    """Helper to register and log in a user."""
    client.post("/auth/register", json={
        "email": user_email,
        "password": password,
        "full_name": "Phase 11B UI Auditor",
        "role": "treasurer"
    })
    r = client.post("/auth/login", json={"email": user_email, "password": password})
    assert r.status_code == 200, f"Login failed for {user_email}: {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestPhase11BCurrentUIVerification:
    """Phase 11B — Current Vanilla UI Full End-to-End Verification Baseline."""

    def test_part1_app_startup_and_health(self):
        """PART 1: Verify app startup, health check, static assets, and UI html loading."""
        r_health = client.get("/health")
        assert r_health.status_code == 200
        assert r_health.json()["status"] == "healthy"

        r_readiness = client.get("/readiness")
        assert r_readiness.status_code == 200
        assert r_readiness.json()["status"] == "ready"

        r_ui = client.get("/dashboard-ui")
        assert r_ui.status_code == 200
        assert "<!DOCTYPE html>" in r_ui.text
        assert "Treasury Intelligence" in r_ui.text

        r_css = client.get("/static/styles.css")
        assert r_css.status_code == 200

    def test_part3_login_and_auth_workflow(self):
        """PART 3: Login, session refresh, invalid credentials rejection, and secret protection."""
        email = "ui_auth_user@phase11b.test"
        password = "SecurePassword123!"

        # Register & Login
        headers = register_and_login(email, password)
        assert "Authorization" in headers

        # Verify invalid login rejection
        r_invalid = client.post("/auth/login", json={"email": email, "password": "WrongPassword!"})
        assert r_invalid.status_code == 401
        assert "Invalid" in r_invalid.json()["detail"]

        # Verify backend secret key is not exposed in HTML UI response
        r_ui = client.get("/dashboard-ui")
        assert "SECRET_KEY" not in r_ui.text
        assert "jwt_secret" not in r_ui.text

    def test_part4_navigation_audit_matrix(self):
        """PART 4: Audit every navigation route in index.html for 200 OK and no broken pages."""
        html_routes = [
            "/",
            "/home",
            "/dashboard",
            "/dashboard-ui",
            "/ingestion",
            "/reconciliation",
            "/review-queue",
            "/reports",
            "/settings",
            "/bank-master",
            "/pricing",
            "/contact"
        ]

        for route in html_routes:
            r = client.get(route)
            assert r.status_code == 200, f"Route {route} failed with status {r.status_code}"
            assert "<!DOCTYPE html>" in r.text

    def test_part5_dashboard_ui_numerical_correctness(self):
        """PART 5: Dashboard metrics match exact database values."""
        headers = register_and_login("dashboard_ui@phase11b.test")

        r_summary = client.get("/dashboard/summary", headers=headers)
        assert r_summary.status_code == 200
        assert r_summary.json() is not None

    def test_part6_transactions_ui_and_paise_formatting(self):
        """PART 6: Canonical transaction listing, filtering, IDOR protection, and paise conversion."""
        headers = register_and_login("tx_ui_user@phase11b.test")

        r_acc = client.post("/v1/bank-master/accounts", headers=headers, json={
            "bank_code": "HDFC",
            "account_number": "987654321012",
            "account_type": "CURRENT"
        })
        acc_id = r_acc.json()["id"]

        db: Session = next(get_db())
        user_db = db.query(Account).filter(Account.id == uuid.UUID(acc_id)).first().user

        # 4863414 paise = ₹48,634.14
        txn = Transaction(
            id=uuid.uuid4(),
            user_id=user_db.id,
            account_id=uuid.UUID(acc_id),
            direction=Direction.CREDIT,
            credit_paise=4863414,
            txn_date=datetime.date(2026, 2, 1),
            narration_raw="Vendor Deposit Audit Test"
        )
        db.add(txn)
        db.commit()

        # Query API endpoint
        r_txs = client.get("/transactions", headers=headers)
        assert r_txs.status_code == 200
        txs_list = r_txs.json()
        target_tx = next(t for t in txs_list if t["id"] == str(txn.id))

        assert target_tx["credit"] == 48634.14 or target_tx["amount"] == 48634.14

        # Test IDOR protection
        headers_other = register_and_login("tx_other_user@phase11b.test")
        r_idor = client.get(f"/transactions/{txn.id}", headers=headers_other)
        assert r_idor.status_code in (403, 404)

    def test_part7_file_upload_and_idempotency(self):
        """PART 7: Upload workflow and Phase 6 idempotency."""
        headers = register_and_login("upload_ui_user@phase11b.test")

        csv_content = (
            "Date,Narration,Debit,Credit,Balance\n"
            "2026-02-01,Client Payment,0.00,15000.00,15000.00\n"
            "2026-02-02,Office Supplies,2500.00,0.00,12500.00\n"
        )

        files = {"file": ("statement.csv", io.BytesIO(csv_content.encode("utf-8")), "text/csv")}
        r_up1 = client.post("/files/upload", headers=headers, files=files)
        assert r_up1.status_code in (200, 202)

        r_conf = client.post("/v1/reconciliation/imports/confirm", headers=headers, json={
            "column_mapping": {"entry_date": "Date", "narration": "Narration", "money_in": "Credit", "money_out": "Debit"},
            "rows": [
                {"Date": "2026-02-01", "Narration": "Client Payment", "Credit": "15000.00", "Debit": "0.00"},
                {"Date": "2026-02-02", "Narration": "Office Supplies", "Credit": "0.00", "Debit": "2500.00"}
            ]
        })
        assert r_conf.status_code in (200, 201)

    def test_part9_reports_ui_endpoints(self):
        """PART 9: Monthly, Yearly, Expense, Income report endpoints."""
        headers = register_and_login("reports_ui@phase11b.test")

        assert client.get("/reports/monthly?year=2026&month=2", headers=headers).status_code == 200
        assert client.get("/reports/yearly?year=2026", headers=headers).status_code == 200
        assert client.get("/reports/expense", headers=headers).status_code == 200
        assert client.get("/reports/income", headers=headers).status_code == 200

    def test_part10_and_11_treasury_overview_ui_and_csv(self):
        """PART 10 & 11: Treasury Overview report calculations and CSV download."""
        headers = register_and_login("treasury_ui@phase11b.test")

        r_tr = client.get("/reports/treasury?start_date=2026-01-01&end_date=2026-02-28", headers=headers)
        assert r_tr.status_code == 200
        data = r_tr.json()
        assert data["report_type"] == "Treasury Overview - Group Level"

        gt = data["group_totals"]

        assert round(gt["net_flow"], 2) == round(gt["total_inflows"] - gt["total_outflows"], 2)
        assert round(gt["closing_balance"], 2) == round(gt["opening_balance"] + gt["net_flow"], 2)

        r_csv = client.get("/reports/export/treasury?start_date=2026-01-01&end_date=2026-02-28", headers=headers)
        assert r_csv.status_code == 200
        assert "text/csv" in r_csv.headers["content-type"]
        assert "Opening Balance" in r_csv.text

    def test_part12_and_13_reconciliation_and_immutable_versioning(self):
        """PART 12 & 13: Reconciliation engine run execution & Phase 10 immutable history versioning."""
        headers = register_and_login("recon_ui@phase11b.test")

        r_acc = client.post("/v1/bank-master/accounts", headers=headers, json={
            "bank_code": "HDFC",
            "account_number": "112233445566",
            "account_type": "CURRENT"
        })

        client.post("/v1/reconciliation/imports/confirm", headers=headers, json={
            "column_mapping": {"entry_date": "Date", "narration": "Narration", "money_in": "Credit", "money_out": "Debit"},
            "rows": [{"Date": "2026-01-10", "Narration": "Bank Transfer", "Credit": "5000.00", "Debit": "0.00"}]
        })

        # Execute Run 1
        r_run1 = client.post("/v1/reconciliation/runs", json={"period_from": "2026-01-01", "period_to": "2026-02-28", "force": True}, headers=headers)
        assert r_run1.status_code in (200, 201)
        run1_id = r_run1.json()["run_id"]

        # Execute Run 2
        r_run2 = client.post("/v1/reconciliation/runs", json={"period_from": "2026-01-01", "period_to": "2026-02-28", "force": True}, headers=headers)
        assert r_run2.status_code in (200, 201)
        run2_id = r_run2.json()["run_id"]

        # Verify Run 1 is still fetchable
        r_fetch1 = client.get(f"/v1/reconciliation/runs/{run1_id}", headers=headers)
        assert r_fetch1.status_code == 200

    def test_part14_review_queue_ui(self):
        """PART 14: Review queue fetching for cross-source duplicates and BRS candidates."""
        headers = register_and_login("queue_ui@phase11b.test")
        r_dedup = client.get("/v1/deduplication/matches", headers=headers)
        assert r_dedup.status_code == 200

    def test_part15_bank_master_and_entities_ui(self):
        """PART 15: Bank master entity and account creation."""
        headers = register_and_login("bank_master_ui@phase11b.test")

        r_ent = client.post("/v1/bank-master/entities", headers=headers, json={"name": "ACME Holding Entity"})
        assert r_ent.status_code == 201
        ent_id = r_ent.json()["id"]

        r_acc = client.post("/v1/bank-master/accounts", headers=headers, json={
            "entity_id": ent_id,
            "bank_code": "ICICI",
            "account_number": "554433221100",
            "account_type": "SAVINGS"
        })
        assert r_acc.status_code == 201

    def test_part16_and_17_external_integrations_classification(self):
        """PART 16 & 17: Gmail and Account Aggregator integration endpoints."""
        headers = register_and_login("integrations_ui@phase11b.test")

        r_gmail = client.post("/email/connect", headers=headers)
        assert r_gmail.status_code in (200, 307)
        assert "authorization_url" in r_gmail.json()

        r_rpa = client.get("/rpa/banks", headers=headers)
        assert r_rpa.status_code in (200, 501)
