import os
import tempfile
import pytest
import pandas as pd
from app.parsers.excel_csv_parser import ExcelCSVParser

def test_native_xlsx_parsing():
    parser = ExcelCSVParser()
    df = pd.DataFrame({
        "Txn Date": ["01/08/2026", "02/08/2026"],
        "Particulars": ["UPI Swiggy Payment", "Salary Credit ACME CORP"],
        "Withdrawal (Dr)": [499.0, 0.0],
        "Deposit (Cr)": [0.0, 50000.0],
        "Balance": [45000.0, 95000.0]
    })

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        df.to_excel(f.name, index=False)
        tmp_name = f.name

    try:
        results = parser.parse(tmp_name)
        assert len(results) == 2
        assert results[0]["date"] == "2026-08-01"
        assert results[0]["description"] == "UPI Swiggy Payment"
        assert float(results[0]["debit"]) == 499.0
        assert results[1]["description"] == "Salary Credit ACME CORP"
        assert float(results[1]["credit"]) == 50000.0
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def test_html_table_xlsx_parsing():
    parser = ExcelCSVParser()
    html_data = """
    <html>
    <body>
    <table>
    <tr><th>Value Date</th><th>Narration</th><th>Dr Amount</th><th>Cr Amount</th><th>Closing Balance</th></tr>
    <tr><td>15/07/2026</td><td>POS Amazon India</td><td>1250.00</td><td>0.00</td><td>43750.00</td></tr>
    <tr><td>20/07/2026</td><td>IMPS Electricity Bill</td><td>3200.00</td><td>0.00</td><td>40550.00</td></tr>
    </table>
    </body>
    </html>
    """

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False, mode="w") as f:
        f.write(html_data)
        tmp_name = f.name

    try:
        results = parser.parse(tmp_name)
        assert len(results) == 2
        assert results[0]["date"] == "2026-07-15"
        assert results[0]["description"] == "POS Amazon India"
        assert float(results[0]["debit"]) == 1250.0
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def test_multisheet_xlsx_parsing():
    parser = ExcelCSVParser()
    df_meta = pd.DataFrame({"Info": ["Account Statement Disclaimer", "Branch Code: 1234"]})
    df_txns = pd.DataFrame({
        "Date": ["10/06/2026"],
        "Transaction Details": ["NEFT Client Settlement"],
        "Debit": [0.0],
        "Credit": [75000.0],
        "Balance": [115500.0]
    })

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        with pd.ExcelWriter(f.name, engine="openpyxl") as writer:
            df_meta.to_excel(writer, sheet_name="CoverPage", index=False)
            df_txns.to_excel(writer, sheet_name="Statement", index=False)
        tmp_name = f.name

    try:
        results = parser.parse(tmp_name)
        assert len(results) == 1
        assert results[0]["description"] == "NEFT Client Settlement"
        assert float(results[0]["credit"]) == 75000.0
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
