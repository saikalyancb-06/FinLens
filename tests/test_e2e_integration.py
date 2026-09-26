import io
import os
import uuid
import tempfile
import pytest
import fitz # PyMuPDF
from fastapi.testclient import TestClient
from main import app as fastapi_app
from tests.conftest import TestingSessionLocal
from app.models.uploaded_file import UploadedFile
from app.models.processed_transaction import ProcessedTransaction
from app.services.parsing_queue import process_file_parsing_task
from app.database.session import get_db

def create_sample_bank_statement_csv(file_path: str):
    lines = [
        "Date,Description,Debit,Credit,Balance",
        "2026-08-01,SALARY CREDIT,0.00,7500.00,7500.00",
        "2026-08-02,STARBUCKS COFFEE STORE,15.50,0.00,7484.50",
        "2026-08-03,ELECTRICITY BILL,120.00,0.00,7364.50",
        "2026-08-04,ATM CASH WITHDRAWAL,200.00,0.00,7164.50",
        "2026-08-05,SWIGGY FOOD ORDER,45.00,0.00,7119.50",
    ]
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def test_e2e_complete_integration_flow(client, tmp_path):
    db_session = None
    try:
        # Stage 0: User Registration & Authentication
        user_email = f"e2e_tester_{uuid.uuid4().hex[:8]}@example.com"
        user_password = "SecurePassword123!"

        reg_res = client.post("/auth/register", json={"email": user_email, "password": user_password})
        assert reg_res.status_code == 201, f"Registration failed: {reg_res.text}"

        login_res = client.post("/auth/login", json={"email": user_email, "password": user_password})
        assert login_res.status_code == 200, f"Login failed: {login_res.text}"
        token = login_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Uploads require at least one registered Bank Master account.
        from conftest import register_bank_account
        register_bank_account(client, headers)

        me_res = client.get("/auth/me", headers=headers)
        assert me_res.status_code == 200
        user_id_str = me_res.json()["id"]
        user_uuid = uuid.UUID(user_id_str)

        # Stage 1: File Upload
        csv_path = os.path.join(tmp_path, "sample_bank_statement.csv")
        create_sample_bank_statement_csv(csv_path)

        with open(csv_path, "rb") as csv_file:
            upload_res = client.post(
                "/files/upload",
                files={"file": ("sample_bank_statement.csv", csv_file, "text/csv")},
                headers=headers
            )


        assert upload_res.status_code == 202, f"Upload failed: {upload_res.text}"
        upload_data = upload_res.json()
        file_id_str = upload_data["file_id"]
        file_uuid = uuid.UUID(file_id_str)

        # Retrieve file metadata from DB
        db_gen = fastapi_app.dependency_overrides.get(get_db, get_db)()
        db_session = next(db_gen)
        db_file = db_session.query(UploadedFile).filter(UploadedFile.id == file_uuid).first()
        assert db_file is not None, "Uploaded file not persisted in database"
        assert db_file.status in ["QUEUED", "COMPLETED"], f"Unexpected initial file status: {db_file.status}"
        saved_file_path = db_file.file_path

        # -------------------------------------------------------------
        # Stage 2: Direct Execution of Background Parsing Worker Task
        # -------------------------------------------------------------
        summary = process_file_parsing_task(file_id=file_uuid, file_path=saved_file_path, user_id=user_id_str)
        
        assert summary["status"] == "COMPLETED"
        assert summary["total_extracted"] == 5
        assert summary["total_valid"] == 5
        assert summary["total_stored"] == 5
        assert summary["error_message"] is None

        # Verify status transition from QUEUED -> COMPLETED
        file_status_res = client.get(f"/files/{file_id_str}", headers=headers)
        assert file_status_res.status_code == 200
        assert file_status_res.json()["status"] == "COMPLETED"

        # -------------------------------------------------------------
        # Stage 3: Deduplication & Idempotency Check
        # -------------------------------------------------------------
        # Run parsing task again for the same file to ensure no duplicate transactions are inserted
        summary_repeat = process_file_parsing_task(file_id=file_uuid, file_path=saved_file_path, user_id=user_id_str)
        assert summary_repeat["status"] == "COMPLETED"
        assert summary_repeat["total_stored"] == 5

        txns_res = client.get("/transactions", headers=headers)
        assert txns_res.status_code == 200
        stored_txns = txns_res.json()
        assert len(stored_txns) == 5, f"Expected 5 transactions, found {len(stored_txns)} (Deduplication failed)"

        # -------------------------------------------------------------
        # Stage 4: Transaction Level & Business Logic Assertions
        # -------------------------------------------------------------
        for txn in stored_txns:
            assert txn["final_category"], "Transaction missing final_category"
            assert txn["confidence"] >= 0.0, "Transaction missing confidence score"
            assert txn["prediction_source"] in ["Rule Engine", "ML Model", "STATEMENT", "Account Aggregator", "MANUAL_UPLOAD"], f"Unknown prediction source: {txn['prediction_source']}"


        # Row 1: Salary Credit verification
        row1 = [t for t in stored_txns if "SALARY" in t["description"]][0]
        # "Salary / Income" is the canonical taxonomy name (app/categorization/taxonomy.py);
        # the older spellings are kept for rows predating the taxonomy.
        assert row1["final_category"] in ["Salary / Income", "Salary", "Salary Payment", "Income", "Direct Income", "Uncategorized"]

        # Row 2: Starbucks ML Model prediction verification
        row2 = [t for t in stored_txns if "STARBUCKS" in t["description"]][0]
        assert row2["final_category"] in ["Food & Dining", "Food", "Entertainment", "Others", "Other", "Uncategorized"]
        assert row2["amount"] == 15.50


        # -------------------------------------------------------------
        # Stage 5: Dashboard & Analytics Reconciliation
        # -------------------------------------------------------------
        dash_res = client.get("/dashboard/summary", headers=headers)
        assert dash_res.status_code == 200
        dash_data = dash_res.json()

        db_total_credit = round(sum(t["credit"] for t in stored_txns), 2)
        db_total_debit = round(sum(t["debit"] for t in stored_txns), 2)
        db_net_cash_flow = round(db_total_credit - db_total_debit, 2)

        assert dash_data["total_transactions"] == len(stored_txns)
        assert dash_data["total_credit"] == db_total_credit
        assert dash_data["total_debit"] == db_total_debit
        assert dash_data["net_cash_flow"] == db_net_cash_flow

        # Analytics checks
        recent_res = client.get("/analytics/recent-transactions?limit=10", headers=headers)
        assert recent_res.status_code == 200
        assert len(recent_res.json()) == 5

        monthly_res = client.get("/analytics/monthly-summary", headers=headers)
        assert monthly_res.status_code == 200
        august_summary = [m for m in monthly_res.json() if m["month"] == "2026-08"]
        assert len(august_summary) == 1
        assert august_summary[0]["transaction_count"] == 5

        cat_res = client.get("/analytics/category-breakdown", headers=headers)
        assert cat_res.status_code == 200
        assert len(cat_res.json()) > 0

        cashflow_res = client.get("/analytics/cash-flow", headers=headers)
        assert cashflow_res.status_code == 200
        assert len(cashflow_res.json()) > 0

        # Report endpoint check
        report_res = client.get("/reports/monthly?year=2026&month=8", headers=headers)
        assert report_res.status_code == 200
        report_data = report_res.json()
        assert report_data["period"] == "2026-08"
        assert report_data["transaction_count"] == dash_data["total_transactions"]

    finally:
        if db_session:
            db_session.close()
