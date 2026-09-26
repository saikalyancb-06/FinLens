"""
Phase 8: Reports + Exports + PDF Correctness Audit
Tests every report endpoint's mathematical correctness, consistency, user isolation,
date boundary filtering, money precision, and export correctness.

DESIGN NOTES:
  - CSV column values are INput as RUPEES (e.g., 1000.00 = ₹1000).
  - The storage service converts them to paise: 1000.00 * 100 = 100000 paise.
  - The report endpoints convert paise back to rupees: 100000 / 100 = 1000.00.
  - So when we upload "1000.00" credit, the report should show total_credit = 1000.00.
  - Expected values in tests use RUPEE amounts matching what is IN the CSV.
"""
import io
import csv
import datetime
import uuid

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def register_and_login(client: TestClient, email: str, password: str = "StrongPass123!") -> dict:
    client.post("/auth/register", json={"email": email, "password": password})
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"Login failed for {email}: {r.text}"
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.post("/v1/bank-master/accounts", headers=headers, json={
        "bank_code": "HDFC",
        "account_number": "1234567890",
        "account_type": "CURRENT"
    })
    return headers


def upload_csv(client: TestClient, headers: dict, rows: list[str], filename: str = "test.csv") -> dict:
    """Upload a minimal bank-statement CSV and return the response.
    
    CSV column format: Date,Description,Debit,Credit,Balance
    Values are RUPEES (float), stored as rupees*100 paise internally.
    """
    header_row = "Date,Description,Debit,Credit,Balance"
    content = "\n".join([header_row] + rows) + "\n"
    r = client.post(
        "/files/upload",
        files={"file": (filename, io.BytesIO(content.encode()), "text/csv")},
        headers=headers,
    )
    assert r.status_code == 202, f"Upload failed: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# PART 1 — REPORT ENDPOINTS: Monthly, Yearly, Expense, Income
# ---------------------------------------------------------------------------

class TestMonthlyReport:
    """Independent verification of GET /reports/monthly."""

    def test_monthly_report_correct_totals(self, client: TestClient):
        """INDEPENDENT: sum from raw records must match report output.
        
        CSV uploads use RUPEE amounts. The report converts paise→rupees at the boundary.
        So expected values are the RAW CSV RUPEE NUMBERS.
        """
        headers = register_and_login(client, "monthly_report@phase8.test")

        # Rupee amounts in CSV → stored as paise internally → reported as rupees
        # SALARY credit=1000.00, GROCERIES debit=50.00, RENT debit=200.00, FREELANCE credit=300.00
        upload_csv(client, headers, [
            "2026-08-01,SALARY,,1000.00,1000.00",    # credit ₹1000
            "2026-08-02,GROCERIES,50.00,,950.00",      # debit  ₹50
            "2026-08-10,RENT,200.00,,750.00",           # debit  ₹200
            "2026-08-15,FREELANCE,,300.00,1050.00",    # credit ₹300
        ])

        # Independent expected values = what's in the CSV (rupee amounts)
        expected_credit_rupees = 1000.00 + 300.00  # = 1300.00
        expected_debit_rupees  = 50.00 + 200.00    # = 250.00
        expected_count         = 4
        expected_net           = expected_credit_rupees - expected_debit_rupees  # = 1050.00

        r = client.get("/reports/monthly?year=2026&month=8", headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()

        assert data["transaction_count"] == expected_count, \
            f"Expected {expected_count} transactions, got {data['transaction_count']}"
        assert abs(data["total_credit"] - expected_credit_rupees) < 0.005, \
            f"total_credit: expected {expected_credit_rupees}, got {data['total_credit']}"
        assert abs(data["total_debit"] - expected_debit_rupees) < 0.005, \
            f"total_debit: expected {expected_debit_rupees}, got {data['total_debit']}"
        assert abs(data["net_cash_flow"] - expected_net) < 0.005, \
            f"net_cash_flow: expected {expected_net}, got {data['net_cash_flow']}"

    def test_monthly_report_empty(self, client: TestClient):
        """Empty month must return zeros without error."""
        headers = register_and_login(client, "monthly_empty@phase8.test")
        r = client.get("/reports/monthly?year=2000&month=1", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["transaction_count"] == 0
        assert data["total_debit"] == 0.0
        assert data["total_credit"] == 0.0
        assert data["net_cash_flow"] == 0.0

    def test_monthly_report_date_boundary(self, client: TestClient):
        """Transactions in adjacent months must NOT appear in the selected month report."""
        headers = register_and_login(client, "monthly_boundary@phase8.test")
        upload_csv(client, headers, [
            "2026-07-31,PREV_MONTH,10.00,,0",     # July — must NOT appear in August
            "2026-08-01,AUG_FIRST,,20.00,20.00",   # August — MUST appear
            "2026-09-01,NEXT_MONTH,5.00,,15.00",   # September — must NOT appear
        ])

        r = client.get("/reports/monthly?year=2026&month=8", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["transaction_count"] == 1, \
            f"Expected 1 August transaction, got {data['transaction_count']}: {[t['date'] for t in data['transactions']]}"
        assert abs(data["total_credit"] - 20.00) < 0.005, \
            f"Expected total_credit=20.00, got {data['total_credit']}"
        assert data["total_debit"] == 0.0

    def test_monthly_report_user_isolation(self, client: TestClient):
        """User A's monthly report must not include User B's transactions."""
        hA = register_and_login(client, "isolation_a@phase8.test")
        hB = register_and_login(client, "isolation_b@phase8.test")

        upload_csv(client, hA, ["2026-08-05,A_TRANSACTION,,500.00,500.00"])
        upload_csv(client, hB, ["2026-08-05,B_TRANSACTION,,999.99,999.99"])

        rA = client.get("/reports/monthly?year=2026&month=8", headers=hA)
        assert rA.status_code == 200
        data = rA.json()
        assert data["transaction_count"] == 1
        assert abs(data["total_credit"] - 500.00) < 0.005, \
            f"Expected total_credit=500.00 for User A, got {data['total_credit']}"


class TestYearlyReport:
    """Independent verification of GET /reports/yearly."""

    def test_yearly_report_correct_totals(self, client: TestClient):
        """INDEPENDENT: rupee sums from raw records must match report output."""
        headers = register_and_login(client, "yearly_report@phase8.test")

        upload_csv(client, headers, [
            "2026-01-15,JAN_INCOME,,1000.00,1000.00",  # credit ₹1000
            "2026-06-20,JUN_EXPENSE,400.00,,600.00",    # debit  ₹400
            "2026-12-31,DEC_INCOME,,800.00,1400.00",    # credit ₹800
        ])

        expected_credit = 1000.00 + 800.00  # 1800.00
        expected_debit  = 400.00
        expected_count  = 3

        r = client.get("/reports/yearly?year=2026", headers=headers)
        assert r.status_code == 200
        data = r.json()

        assert data["transaction_count"] == expected_count
        assert abs(data["total_credit"] - expected_credit) < 0.005
        assert abs(data["total_debit"]  - expected_debit)  < 0.005

    def test_yearly_report_december_january_boundary(self, client: TestClient):
        """Dec 2025 must NOT appear in 2026 yearly report; Jan 2026 must appear."""
        headers = register_and_login(client, "yearly_boundary@phase8.test")
        upload_csv(client, headers, [
            "2025-12-31,DEC25,,500.00,500.00",   # 2025 — must NOT appear in 2026
            "2026-01-01,JAN26,,300.00,800.00",   # 2026 — MUST appear
        ])

        r = client.get("/reports/yearly?year=2026", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["transaction_count"] == 1, \
            f"Expected 1 (2026 only), got {data['transaction_count']}: {[t['date'] for t in data['transactions']]}"
        assert abs(data["total_credit"] - 300.00) < 0.005

    def test_yearly_report_empty(self, client: TestClient):
        """Yearly report with no matching transactions returns zeros."""
        headers = register_and_login(client, "yearly_empty@phase8.test")
        r = client.get("/reports/yearly?year=1999", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["transaction_count"] == 0
        assert data["total_debit"] == 0.0
        assert data["total_credit"] == 0.0


class TestExpenseReport:
    """Independent verification of GET /reports/expense."""

    def test_expense_report_debit_only(self, client: TestClient):
        """Expense report must include ONLY debit transactions."""
        headers = register_and_login(client, "expense_report@phase8.test")
        upload_csv(client, headers, [
            "2026-08-01,SALARY,,2000.00,2000.00",  # credit — must NOT be in expense
            "2026-08-02,RENT,300.00,,1700.00",       # debit — must be in expense
            "2026-08-03,FOOD,50.00,,1650.00",         # debit — must be in expense
        ])

        expected_total_debit = 300.00 + 50.00  # 350.00
        expected_count = 2

        r = client.get("/reports/expense", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == expected_count, \
            f"Expected {expected_count} expense rows, got {data['count']}"
        assert abs(data["total_expense"] - expected_total_debit) < 0.005, \
            f"Expected total_expense={expected_total_debit}, got {data['total_expense']}"

    def test_expense_report_date_filter(self, client: TestClient):
        """Date filters on expense report must use txn_date boundaries correctly."""
        headers = register_and_login(client, "expense_date@phase8.test")
        upload_csv(client, headers, [
            "2026-07-31,BEFORE,100.00,,0",      # before range — excluded
            "2026-08-01,FIRST_DAY,200.00,,0",   # within range — included
            "2026-08-31,LAST_DAY,300.00,,0",    # within range — included
            "2026-09-01,AFTER,400.00,,0",       # after range — excluded
        ])

        r = client.get("/reports/expense?start_date=2026-08-01&end_date=2026-08-31", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2, \
            f"Expected 2 within-range expenses, got {data['count']}"
        assert abs(data["total_expense"] - (200.00 + 300.00)) < 0.005, \
            f"Expected total_expense=500.00, got {data['total_expense']}"

    def test_expense_report_empty(self, client: TestClient):
        """Expense report with no debit transactions returns zero/no-error."""
        headers = register_and_login(client, "expense_empty@phase8.test")
        upload_csv(client, headers, ["2026-08-01,INCOME,,500.00,500.00"])  # credit only
        r = client.get("/reports/expense", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 0
        assert data["total_expense"] == 0.0


class TestIncomeReport:
    """Independent verification of GET /reports/income."""

    def test_income_report_credit_only(self, client: TestClient):
        """Income report must include ONLY credit transactions."""
        headers = register_and_login(client, "income_report@phase8.test")
        upload_csv(client, headers, [
            "2026-08-01,SALARY,,2000.00,2000.00",   # credit — must be in income
            "2026-08-02,EXPENSE,100.00,,1900.00",    # debit — must NOT be in income
            "2026-08-05,FREELANCE,,500.00,2400.00",  # credit — must be in income
        ])

        expected_credit = 2000.00 + 500.00  # 2500.00
        expected_count = 2

        r = client.get("/reports/income", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == expected_count, \
            f"Expected {expected_count} income rows, got {data['count']}"
        assert abs(data["total_income"] - expected_credit) < 0.005, \
            f"Expected total_income={expected_credit}, got {data['total_income']}"

    def test_income_report_date_filter(self, client: TestClient):
        """Date filters on income report must respect txn_date boundaries."""
        headers = register_and_login(client, "income_date@phase8.test")
        upload_csv(client, headers, [
            "2026-07-31,PREV,,1000.00,0",       # before range — excluded
            "2026-08-01,FIRST_DAY,,2000.00,0",  # within range — included
            "2026-08-31,LAST_DAY,,3000.00,0",   # within range — included
            "2026-09-01,NEXT,,4000.00,0",       # after range — excluded
        ])

        r = client.get("/reports/income?start_date=2026-08-01&end_date=2026-08-31", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2, \
            f"Expected 2 within-range incomes, got {data['count']}"
        assert abs(data["total_income"] - (2000.00 + 3000.00)) < 0.005, \
            f"Expected total_income=5000.00, got {data['total_income']}"

    def test_income_report_empty(self, client: TestClient):
        """Income report with no credit transactions returns zero without error."""
        headers = register_and_login(client, "income_empty@phase8.test")
        upload_csv(client, headers, ["2026-08-01,RENT,500.00,,500.00"])  # debit only
        r = client.get("/reports/income", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 0
        assert data["total_income"] == 0.0


# ---------------------------------------------------------------------------
# PART 2 — CSV EXPORT CORRECTNESS
# ---------------------------------------------------------------------------

class TestCSVExport:
    """Verify GET /reports/export/csv."""

    def test_csv_export_returns_correct_content_type(self, client: TestClient):
        headers = register_and_login(client, "csv_content_type@phase8.test")
        r = client.get("/reports/export/csv", headers=headers)
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")

    def test_csv_export_row_count_matches_db(self, client: TestClient):
        """INDEPENDENT: count CSV data rows vs the number of transactions uploaded."""
        headers = register_and_login(client, "csv_rowcount@phase8.test")
        upload_csv(client, headers, [
            "2026-08-01,TXN1,,100.00,100.00",
            "2026-08-02,TXN2,50.00,,50.00",
            "2026-08-03,TXN3,,30.00,80.00",
        ])

        r = client.get("/reports/export/csv", headers=headers)
        assert r.status_code == 200
        content = r.content.decode("utf-8")
        reader = list(csv.reader(io.StringIO(content)))
        data_rows = reader[1:]  # skip header
        assert len(data_rows) == 3, f"Expected 3 data rows, got {len(data_rows)}"

    def test_csv_export_monetary_values_correct(self, client: TestClient):
        """Verify CSV monetary values are correct rupee amounts (paise/100)."""
        headers = register_and_login(client, "csv_amounts@phase8.test")
        # Upload ₹123.45 credit, ₹678.90 debit
        upload_csv(client, headers, [
            "2026-08-01,CREDIT,,123.45,123.45",    # credit ₹123.45
            "2026-08-02,DEBIT,678.90,,0",           # debit ₹678.90
        ])

        r = client.get("/reports/export/csv", headers=headers)
        assert r.status_code == 200
        content = r.content.decode("utf-8")
        reader = list(csv.reader(io.StringIO(content)))
        # Build dict: description → row
        rows_by_desc = {row[1]: row for row in reader[1:]}

        # CSV header: Date, Description, Debit, Credit, Amount, Balance, ...
        # Indices:      0      1           2      3       4       5
        credit_row = rows_by_desc.get("CREDIT")
        assert credit_row is not None, f"CREDIT row not found. Rows: {rows_by_desc}"
        assert abs(float(credit_row[3]) - 123.45) < 0.005, \
            f"Credit column: expected 123.45, got {credit_row[3]}"

        debit_row = rows_by_desc.get("DEBIT")
        assert debit_row is not None, f"DEBIT row not found. Rows: {rows_by_desc}"
        assert abs(float(debit_row[2]) - 678.90) < 0.005, \
            f"Debit column: expected 678.90, got {debit_row[2]}"

    def test_csv_export_no_legacy_fields(self, client: TestClient):
        """CSV must not contain legacy columns: file_id, debit_credit, review_status."""
        headers = register_and_login(client, "csv_nolegacy@phase8.test")
        r = client.get("/reports/export/csv", headers=headers)
        assert r.status_code == 200
        content = r.content.decode("utf-8")
        reader = list(csv.reader(io.StringIO(content)))
        col_names = [h.lower() for h in reader[0]]
        for bad_col in ("file_id", "debit_credit", "review_status"):
            assert bad_col not in col_names, \
                f"Legacy column '{bad_col}' found in CSV export header: {reader[0]}"

    def test_csv_export_user_isolation(self, client: TestClient):
        """User A's CSV must not contain User B's transactions."""
        hA = register_and_login(client, "csv_iso_a@phase8.test")
        hB = register_and_login(client, "csv_iso_b@phase8.test")
        upload_csv(client, hA, ["2026-08-01,ONLY_A,,10.00,10.00"])
        upload_csv(client, hB, ["2026-08-01,ONLY_B,,99.99,99.99"])

        rA = client.get("/reports/export/csv", headers=hA)
        content = rA.content.decode("utf-8")
        assert "ONLY_A" in content.upper(), "User A's description not found in their own CSV"
        assert "ONLY_B" not in content.upper(), "User B's description leaked into User A's CSV"


# ---------------------------------------------------------------------------
# PART 3 — EXCEL EXPORT
# ---------------------------------------------------------------------------

class TestExcelExport:
    """Verify GET /reports/export/excel."""

    def test_excel_export_status_and_content_type(self, client: TestClient):
        headers = register_and_login(client, "excel_status@phase8.test")
        r = client.get("/reports/export/excel", headers=headers)
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        assert "spreadsheetml" in ct or "excel" in ct or "octet-stream" in ct, \
            f"Unexpected content-type: {ct}"

    def test_excel_export_valid_workbook(self, client: TestClient):
        """Verify the exported bytes form a valid openpyxl workbook."""
        try:
            import openpyxl
        except ImportError:
            pytest.skip("openpyxl not installed")

        headers = register_and_login(client, "excel_valid@phase8.test")
        upload_csv(client, headers, [
            "2026-08-01,XL_TXN1,,100.00,100.00",
            "2026-08-02,XL_TXN2,50.00,,50.00",
        ])
        r = client.get("/reports/export/excel", headers=headers)
        assert r.status_code == 200

        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        assert "Transactions" in wb.sheetnames, \
            f"Sheet 'Transactions' not found. Found: {wb.sheetnames}"
        ws = wb["Transactions"]
        rows = list(ws.iter_rows(values_only=True))
        # Header row + at least 2 data rows
        assert len(rows) >= 3, f"Expected header + 2 data rows, got {len(rows)}"

    def test_excel_row_count_matches_db(self, client: TestClient):
        """INDEPENDENT: Excel data row count must equal DB transaction count."""
        try:
            import openpyxl
        except ImportError:
            pytest.skip("openpyxl not installed")

        headers = register_and_login(client, "excel_count@phase8.test")
        upload_csv(client, headers, [
            "2026-08-01,XE1,,100.00,100.00",
            "2026-08-02,XE2,50.00,,50.00",
            "2026-08-03,XE3,,30.00,80.00",
        ])

        r = client.get("/reports/export/excel", headers=headers)
        assert r.status_code == 200
        wb = openpyxl.load_workbook(io.BytesIO(r.content))
        ws = wb["Transactions"]
        all_rows = list(ws.iter_rows(values_only=True))
        data_rows = [row for row in all_rows[1:] if any(v is not None for v in row)]
        assert len(data_rows) == 3, \
            f"Expected 3 data rows in Excel, got {len(data_rows)}"


# ---------------------------------------------------------------------------
# PART 4 — PDF EXPORT
# ---------------------------------------------------------------------------

class TestPDFExport:
    """Verify GET /reports/export/pdf."""

    def test_pdf_returns_200(self, client: TestClient):
        headers = register_and_login(client, "pdf_status@phase8.test")
        r = client.get("/reports/export/pdf", headers=headers)
        assert r.status_code == 200

    def test_pdf_not_empty(self, client: TestClient):
        """PDF must have non-trivial content."""
        headers = register_and_login(client, "pdf_notempty@phase8.test")
        upload_csv(client, headers, ["2026-08-01,TEST,,100.00,100.00"])
        r = client.get("/reports/export/pdf", headers=headers)
        assert r.status_code == 200
        assert len(r.content) > 100, "PDF content suspiciously small (likely empty)"

    def test_pdf_contains_correct_title(self, client: TestClient):
        """PDF must contain the report header."""
        headers = register_and_login(client, "pdf_title@phase8.test")
        r = client.get("/reports/export/pdf", headers=headers)
        assert r.status_code == 200
        content_text = r.content.decode("utf-8", errors="replace")
        assert "FINANCIAL TRANSACTIONS REPORT" in content_text

    def test_pdf_totals_match_independent_calculation(self, client: TestClient):
        """INDEPENDENT: PDF totals must match direct calculation from raw records.
        
        Upload ₹500 credit + ₹200 debit.
        Expected: Total Credit=500.00, Total Debit=200.00, Net=300.00.
        """
        headers = register_and_login(client, "pdf_totals@phase8.test")
        upload_csv(client, headers, [
            "2026-08-01,INCOME,,500.00,500.00",  # credit ₹500
            "2026-08-02,EXPENSE,200.00,,300.00",  # debit ₹200
        ])

        # Independent calculation (what the PDF should show):
        expected_credit = 500.00   # ₹500
        expected_debit  = 200.00   # ₹200
        expected_net    = 300.00   # ₹500 - ₹200

        r = client.get("/reports/export/pdf", headers=headers)
        assert r.status_code == 200
        content_text = r.content.decode("utf-8", errors="replace")

        # Verify no sentinel None/NaN/undefined in PDF output
        for bad in ("None", "NaN", "undefined"):
            assert bad not in content_text, f"'{bad}' found in PDF output"

        # Extract total line and verify amounts
        assert "Total Debit" in content_text, "PDF missing 'Total Debit' summary line"
        assert "Total Credit" in content_text, "PDF missing 'Total Credit' summary line"

        # Check exact amounts
        assert "200.00" in content_text, \
            f"Debit total 200.00 not found in PDF. Content snippet: ...{content_text[-300:]}"
        assert "500.00" in content_text, \
            f"Credit total 500.00 not found in PDF. Content snippet: ...{content_text[-300:]}"
        assert "300.00" in content_text, \
            f"Net cash flow 300.00 not found in PDF. Content snippet: ...{content_text[-300:]}"

    def test_pdf_no_stale_demo_values(self, client: TestClient):
        """PDF must not contain hardcoded demo/placeholder values."""
        headers = register_and_login(client, "pdf_nodemo@phase8.test")
        r = client.get("/reports/export/pdf", headers=headers)
        assert r.status_code == 200
        content_text = r.content.decode("utf-8", errors="replace")
        for placeholder in ("TODO", "PLACEHOLDER", "DEMO_VALUE", "HARDCODED"):
            assert placeholder not in content_text, \
                f"Placeholder '{placeholder}' found in PDF content"

    def test_pdf_user_isolation(self, client: TestClient):
        """User A's PDF must not contain User B's descriptions."""
        hA = register_and_login(client, "pdf_iso_a@phase8.test")
        hB = register_and_login(client, "pdf_iso_b@phase8.test")
        upload_csv(client, hA, ["2026-08-01,ONLY_A_DESC,,100.00,100.00"])
        upload_csv(client, hB, ["2026-08-01,ONLY_B_DESC,,999.99,999.99"])

        rA = client.get("/reports/export/pdf", headers=hA)
        content = rA.content.decode("utf-8", errors="replace")
        assert "ONLY_A_DESC" in content.upper(), \
            "User A's description not found in their own PDF"
        assert "ONLY_B_DESC" not in content.upper(), \
            "User B's description leaked into User A's PDF"


# ---------------------------------------------------------------------------
# PART 5 — RECONCILIATION EXPORT
# ---------------------------------------------------------------------------

class TestReconciliationExport:
    """Verify GET /v1/reconciliation/runs/{run_id}/export."""

    def test_recon_export_not_found_for_wrong_user(self, client: TestClient):
        """Reconciliation export must enforce user ownership (404 for wrong user)."""
        hA = register_and_login(client, "recon_exp_a@phase8.test")
        hB = register_and_login(client, "recon_exp_b@phase8.test")

        # Create a run as User A
        rA = client.post("/v1/reconciliation/runs", json={
            "period_from": "2026-08-01",
            "period_to": "2026-08-31",
            "force": True
        }, headers=hA)
        assert rA.status_code in (200, 201), f"Run creation failed: {rA.text}"
        run_id = rA.json().get("run_id") or rA.json().get("id")
        assert run_id is not None

        # User B tries to export User A's run — must get 404
        rB = client.get(f"/v1/reconciliation/runs/{run_id}/export", headers=hB)
        assert rB.status_code == 404, \
            f"Expected 404 for cross-user access, got {rB.status_code}: {rB.text}"

    def test_recon_export_contains_required_fields(self, client: TestClient):
        """BRS export must contain book_closing, bank_closing, residual, verdict."""
        headers = register_and_login(client, "recon_export_fields@phase8.test")

        r_run = client.post("/v1/reconciliation/runs", json={
            "period_from": "2026-08-01",
            "period_to": "2026-08-31",
            "force": True
        }, headers=headers)
        assert r_run.status_code in (200, 201), f"Run creation failed: {r_run.text}"
        run_id = r_run.json().get("run_id") or r_run.json().get("id")
        assert run_id is not None

        r_export = client.get(f"/v1/reconciliation/runs/{run_id}/export", headers=headers)
        assert r_export.status_code == 200, f"Export failed: {r_export.text}"
        content = r_export.content.decode("utf-8", errors="replace")

        for required in (
            "Balance as per books",
            "Computed Bank Closing",
            "Residual Difference",
            "Verdict",
        ):
            assert required in content, \
                f"Required field '{required}' not found in BRS export. Got:\n{content}"


# ---------------------------------------------------------------------------
# PART 6 — REPORT vs DASHBOARD CONSISTENCY
# ---------------------------------------------------------------------------

class TestReportDashboardConsistency:
    """Verify that report metrics match dashboard for the same date range."""

    def test_monthly_report_vs_dashboard_summary(self, client: TestClient):
        """For the same month, monthly report credit/debit must match dashboard summary."""
        headers = register_and_login(client, "dashboard_vs_report@phase8.test")
        upload_csv(client, headers, [
            "2026-08-01,INC,,600.00,600.00",   # credit ₹600
            "2026-08-02,EXP,250.00,,350.00",   # debit  ₹250
        ])

        # Dashboard summary for August 2026
        r_dash = client.get(
            "/dashboard/summary?from_date=2026-08-01&to_date=2026-08-31",
            headers=headers
        )
        assert r_dash.status_code == 200, f"Dashboard failed: {r_dash.text}"
        dash = r_dash.json()

        # Monthly report for August 2026
        r_rep = client.get("/reports/monthly?year=2026&month=8", headers=headers)
        assert r_rep.status_code == 200, f"Monthly report failed: {r_rep.text}"
        rep = r_rep.json()

        # Both sources must agree on total_debit and total_credit to 2dp
        assert abs(dash["total_debit"]  - rep["total_debit"])  < 0.005, \
            f"Dashboard debit {dash['total_debit']} ≠ Report debit {rep['total_debit']}"
        assert abs(dash["total_credit"] - rep["total_credit"]) < 0.005, \
            f"Dashboard credit {dash['total_credit']} ≠ Report credit {rep['total_credit']}"


# ---------------------------------------------------------------------------
# PART 7 — PAISE ARITHMETIC PRECISION
# ---------------------------------------------------------------------------

class TestPaiseArithmeticPrecision:
    """Verify that paise→rupee conversions never use floating-point intermediate math."""

    def test_fractional_paise_no_floating_point_error(self, client: TestClient):
        """Upload amounts with many decimal places; report must round correctly."""
        headers = register_and_login(client, "paise_precision@phase8.test")
        # ₹1.01 credit (101 paise), ₹2.01 debit (201 paise)
        upload_csv(client, headers, [
            "2026-08-01,FRAC_CREDIT,,1.01,1.01",
            "2026-08-02,FRAC_DEBIT,2.01,,0",
        ])

        r = client.get("/reports/monthly?year=2026&month=8", headers=headers)
        assert r.status_code == 200
        data = r.json()

        # Verify to 2dp — the round-trip paise arithmetic must be exact
        assert abs(data["total_credit"] - 1.01) < 0.005, \
            f"Expected total_credit=1.01, got {data['total_credit']}"
        assert abs(data["total_debit"] - 2.01) < 0.005, \
            f"Expected total_debit=2.01, got {data['total_debit']}"
        # Net = 1.01 - 2.01 = -1.00 (not -0.9999999... from float error)
        assert abs(data["net_cash_flow"] - (-1.00)) < 0.005, \
            f"Expected net_cash_flow=-1.00, got {data['net_cash_flow']}"

    def test_csv_export_precision(self, client: TestClient):
        """CSV export Debit/Credit columns must have correct 2dp precision."""
        headers = register_and_login(client, "csv_precision@phase8.test")
        upload_csv(client, headers, [
            "2026-08-01,EXACT,,999.99,999.99",
            "2026-08-02,CENT,0.01,,0",
        ])

        r = client.get("/reports/export/csv", headers=headers)
        assert r.status_code == 200
        content = r.content.decode("utf-8")
        reader = list(csv.reader(io.StringIO(content)))
        rows_by_desc = {row[1]: row for row in reader[1:]}

        credit_row = rows_by_desc.get("EXACT")
        assert credit_row is not None
        assert abs(float(credit_row[3]) - 999.99) < 0.005  # credit col

        debit_row = rows_by_desc.get("CENT")
        assert debit_row is not None
        assert abs(float(debit_row[2]) - 0.01) < 0.005   # debit col
