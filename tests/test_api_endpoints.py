import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from main import app
from app.models import User, UploadedFile, ProcessedTransaction
from app.utils.security import create_access_token
from tests.conftest import TestingSessionLocal, Base, test_engine

@pytest.fixture
def auth_headers(client):
    Base.metadata.create_all(bind=test_engine)
    email = f"user_{uuid.uuid4().hex[:6]}@example.com"
    password = "Password123!"
    reg_res = client.post("/auth/register", json={"email": email, "password": password})
    user_id_str = reg_res.json()["id"]

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    token = login_res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, user_id_str

def test_upload_returns_job_id(client, auth_headers, tmp_path):
    headers, user_id = auth_headers
    
    # Create an account for the user
    acc_res = client.post("/v1/bank-master/accounts", headers=headers, json={
        "bank_code": "HDFC",
        "account_number": "123456789012",
        "account_type": "SAVINGS",
        "currency": "INR"
    })
    acc_id = acc_res.json()["id"]

    file_content = (
        "Date,Particulars,Debit,Credit,Balance\n"
        "01/08/2026,SWIGGY BANGALORE ORDER,300.00,0.00,700.00\n"
        "02/08/2026,BHARATPE PAYOUTS 101,0.00,1500.00,2200.00\n"
    )

    response = client.post(
        "/files/upload",
        headers=headers,
        data={"bank_account_id": acc_id},
        files={"file": ("test_statement.csv", file_content.encode("utf-8"), "text/csv")}
    )

    assert response.status_code == 202
    data = response.json()
    assert "file_id" in data
    assert data["status"] == "QUEUED"

    # Polling job status
    job_id = data["file_id"]
    poll_res = client.get(f"/files/{job_id}", headers=headers)
    assert poll_res.status_code == 200
    assert poll_res.json()["status"] in ["QUEUED", "PROCESSING", "COMPLETED"]

def test_dashboard_and_analytics_endpoints(client, auth_headers):
    headers, user_id = auth_headers

    # Seed processed transaction
    db = TestingSessionLocal()
    p_txn = ProcessedTransaction(
        user_id=uuid.UUID(user_id),
        description="TEST DASHBOARD TXN",
        debit=150.0,
        credit=0.0,
        amount=150.0,
        balance=1000.0,
        final_category="Food & Dining",
        confidence=0.97,
        prediction_source="Rule Engine"
    )
    db.add(p_txn)
    db.commit()
    db.close()

    # 1. Summary
    res = client.get("/dashboard/summary", headers=headers)
    assert res.status_code == 200
    assert "total_transactions" in res.json()

    # 2. Transactions List & Filters
    res = client.get("/transactions", headers=headers)
    assert res.status_code == 200

    res = client.get("/analytics/filters", headers=headers)
    assert res.status_code == 200
    assert "categories" in res.json()

    # 3. Monthly Summary, Category Breakdown, Cash Flow, Recent Transactions
    assert client.get("/analytics/monthly-summary", headers=headers).status_code == 200
    assert client.get("/analytics/category-breakdown", headers=headers).status_code == 200
    assert client.get("/analytics/cash-flow", headers=headers).status_code == 200
    assert client.get("/analytics/recent-transactions", headers=headers).status_code == 200

def test_reports_and_export_endpoints(client, auth_headers):
    headers, _ = auth_headers

    # Reports
    assert client.get("/reports/monthly?year=2026&month=8", headers=headers).status_code == 200
    assert client.get("/reports/yearly?year=2026", headers=headers).status_code == 200
    assert client.get("/reports/expense", headers=headers).status_code == 200
    assert client.get("/reports/income", headers=headers).status_code == 200

    # Exports
    res_csv = client.get("/reports/export/csv", headers=headers)
    assert res_csv.status_code == 200
    assert res_csv.headers["content-type"] == "text/csv; charset=utf-8"

    res_excel = client.get("/reports/export/excel", headers=headers)
    assert res_excel.status_code == 200

    res_pdf = client.get("/reports/export/pdf", headers=headers)
    assert res_pdf.status_code == 200
    assert res_pdf.headers["content-type"] == "application/pdf"
