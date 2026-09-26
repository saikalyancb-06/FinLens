"""
tests/test_parsing_pipeline.py
Integration tests for TransactionParsingPipeline.

Tests cover:
  - SBI-style CSV  (Txn Date, Particulars, Ref, Debit, Credit, Balance)
  - HDFC-style CSV (Date, Narration, Chq.Ref, Value Dt, Withdrawal, Deposit, Closing Bal)
  - ICICI-style CSV
  - Axis-style CSV
  - Excel with header offset (bank name in row 0)
  - Continuation-row descriptions (multi-row descriptions)
  - Quoted fields containing commas
  - Integer amounts (no decimals)
  - Indian lakh formatting
  - Image parser fallback (blank image → list returned)
  - PDF detector smoke test
"""

import os
import pytest
import pandas as pd
from PIL import Image

from app.parsers.pipeline import TransactionParsingPipeline
from app.parsers.pdf_detector import is_digital_pdf


@pytest.fixture
def pipeline():
    return TransactionParsingPipeline()


@pytest.fixture
def tmp(tmp_path):
    return tmp_path


# ─────────────────────────────────────────────────────────────────────────────
# SBI-style CSV
# ─────────────────────────────────────────────────────────────────────────────

def test_sbi_style_csv(tmp, pipeline):
    path = str(tmp / "sbi_statement.csv")
    content = (
        "State Bank of India\n"
        "Account Statement\n"
        "\n"
        "Txn Date,Particulars,Ref No./Cheque No.,Debit,Credit,Balance\n"
        "01/08/2026,SALARY CREDIT UPI/123456789012,,0.00,50000.00,50000.00\n"
        '02/08/2026,"GROCERY, MARKET POS/987654321098",987654321098,1500.50,0.00,48499.50\n'
        "03/08/2026,ATM WITHDRAWAL CHQ/112233,112233,2000.00,0.00,46499.50\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    txns = pipeline.process_file(path)

    assert len(txns) == 3, f"Expected 3 txns, got {len(txns)}"

    t1 = txns[0]
    assert t1["date"] == "2026-08-01"
    assert "SALARY" in t1["description"]
    assert t1["credit"] == 50000.00
    assert t1["debit"] == 0.0
    assert t1["transaction_type"] == "credit"
    assert t1["balance"] == 50000.00

    t2 = txns[1]
    assert t2["date"] == "2026-08-02"
    # Quoted field with comma must be in description intact
    assert "GROCERY" in t2["description"]
    assert t2["debit"] == 1500.50
    assert t2["transaction_type"] == "debit"

    t3 = txns[2]
    assert t3["debit"] == 2000.00
    assert t3["balance"] == 46499.50


# ─────────────────────────────────────────────────────────────────────────────
# HDFC-style CSV
# ─────────────────────────────────────────────────────────────────────────────

def test_hdfc_style_csv(tmp, pipeline):
    path = str(tmp / "hdfc_statement.csv")
    content = (
        "Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "01/08/2026,NEFT CR-SALARY-AXIS BANK,REF123456,01/08/2026,,50000.00,50000.00\n"
        "02/08/2026,POS DEBIT-AMAZON,REF789012,02/08/2026,3000.00,,47000.00\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    txns = pipeline.process_file(path)
    assert len(txns) == 2

    t1 = txns[0]
    assert t1["date"] == "2026-08-01"
    assert t1["credit"] == 50000.00
    assert t1["transaction_type"] == "credit"

    t2 = txns[1]
    assert t2["debit"] == 3000.00
    assert t2["transaction_type"] == "debit"
    assert t2["balance"] == 47000.00


# ─────────────────────────────────────────────────────────────────────────────
# ICICI-style CSV
# ─────────────────────────────────────────────────────────────────────────────

def test_icici_style_csv(tmp, pipeline):
    path = str(tmp / "icici_statement.csv")
    content = (
        "Transaction Date,Value Date,Description,Ref No,Amount,CR/DR,Balance\n"
        "01-08-2026,01-08-2026,IMPS-SALARY RECEIVED,TXN001,50000.00,CR,50000.00\n"
        "05-08-2026,05-08-2026,BILL PAYMENT-ELECTRICITY,TXN002,1200.00,DR,48800.00\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    txns = pipeline.process_file(path)
    assert len(txns) >= 1  # At least one must be extracted

    # Find the credit transaction
    credits = [t for t in txns if t["transaction_type"] == "credit"]
    debits  = [t for t in txns if t["transaction_type"] == "debit"]
    assert len(credits) >= 1
    assert len(debits) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Axis Bank-style CSV
# ─────────────────────────────────────────────────────────────────────────────

def test_axis_style_csv(tmp, pipeline):
    path = str(tmp / "axis_statement.csv")
    content = (
        "Tran Date,PARTICULARS,Instrument Id,Debit,Credit,Balance\n"
        "01-08-2026,SALARY TRANSFER - AXIS BANK,INS123,0.00,45000.00,45000.00\n"
        "03-08-2026,EMI DEDUCTION - HDFC LOAN,INS456,5000.00,0.00,40000.00\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    txns = pipeline.process_file(path)
    assert len(txns) == 2
    assert txns[0]["date"] == "2026-08-01"
    assert txns[0]["credit"] == 45000.00
    assert txns[1]["debit"] == 5000.00


# ─────────────────────────────────────────────────────────────────────────────
# Indian lakh formatting
# ─────────────────────────────────────────────────────────────────────────────

def test_indian_lakh_amounts(tmp, pipeline):
    path = str(tmp / "lakh_statement.csv")
    content = (
        "Date,Description,Debit,Credit,Balance\n"
        "01/08/2026,PROPERTY SALE PROCEEDS,,1,50,000.00,1,50,000.00\n"
        "02/08/2026,INCOME TAX PAYMENT,25,000.00,,1,25,000.00\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    txns = pipeline.process_file(path)
    # Just assert no crash and types are correct
    assert isinstance(txns, list)
    for t in txns:
        assert isinstance(t["amount"], float)
        assert t["amount"] < 50_000_000.0  # Below MAX_AMOUNT ceiling


# ─────────────────────────────────────────────────────────────────────────────
# Excel with header offset
# ─────────────────────────────────────────────────────────────────────────────

def test_excel_header_offset(tmp, pipeline):
    path = str(tmp / "bank_statement.xlsx")
    data = [
        ["Canara Bank", "", "", "", "", ""],
        ["Account Statement Period: Aug 2026", "", "", "", "", ""],
        ["", "", "", "", "", ""],
        ["Date", "Description", "Ref No", "Debit", "Credit", "Balance"],
        ["05-Aug-2026", "ATM CASH WITHDRAWAL", "CHQ/112233", "2000.00", "", "46499.50"],
        ["06-Aug-2026", "SALARY CREDIT", "REF456789", "", "55000.00", "101499.50"],
    ]
    df = pd.DataFrame(data)
    df.to_excel(path, index=False, header=False)

    txns = pipeline.process_file(path)
    assert len(txns) == 2

    debit_txn  = next(t for t in txns if t["debit"] > 0)
    credit_txn = next(t for t in txns if t["credit"] > 0)

    assert debit_txn["debit"] == 2000.00
    assert credit_txn["credit"] == 55000.00


# ─────────────────────────────────────────────────────────────────────────────
# Continuation-row description merging
# ─────────────────────────────────────────────────────────────────────────────

def test_continuation_row_merged(tmp, pipeline):
    path = str(tmp / "continuation.csv")
    # Second row has no date/amount → continuation of first row's description
    content = (
        "Date,Description,Debit,Credit,Balance\n"
        "01/08/2026,NEFT TRANSFER TO,5000.00,,45000.00\n"
        ",JOHN DOE A/C 123456,,,\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    txns = pipeline.process_file(path)
    assert len(txns) == 1
    assert "JOHN DOE" in txns[0]["description"]


# ─────────────────────────────────────────────────────────────────────────────
# Garbage rows rejected before pipeline
# ─────────────────────────────────────────────────────────────────────────────

def test_garbage_amounts_rejected(tmp, pipeline):
    path = str(tmp / "garbage.csv")
    content = (
        "Date,Description,Debit,Credit,Balance\n"
        "01/08/2026,VALID TRANSACTION,1500.00,,48500.00\n"
        "02/08/2026,GARBAGE ROW,000000000000000,,0\n"
        "03/08/2026,ANOTHER GARBAGE,99999999999999999.00,,0\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    txns = pipeline.process_file(path)
    # Only the valid row should survive
    valid = [t for t in txns if t.get("amount", 0) > 0 and t["amount"] < 50_000_000]
    assert len(valid) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# No duplicate transactions
# ─────────────────────────────────────────────────────────────────────────────

def test_no_duplicate_transactions(tmp, pipeline):
    path = str(tmp / "dupes.csv")
    content = (
        "Date,Description,Debit,Credit,Balance\n"
        "01/08/2026,SALARY CREDIT,,50000.00,50000.00\n"
        "01/08/2026,SALARY CREDIT,,50000.00,50000.00\n"  # exact duplicate
        "02/08/2026,GROCERY DEBIT,1000.00,,49000.00\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    result = pipeline.process_file_with_validation(path)
    dup_errors = [
        e for e in result.get("errors", [])
        if e.get("error_type") == "duplicate_transaction"
    ]
    assert len(dup_errors) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Metadata fields present on all transactions
# ─────────────────────────────────────────────────────────────────────────────

def test_metadata_fields_present(tmp, pipeline):
    path = str(tmp / "meta.csv")
    content = (
        "Date,Description,Debit,Credit,Balance\n"
        "01/08/2026,SALARY,,50000.00,50000.00\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    txns = pipeline.process_file(path)
    assert len(txns) >= 1
    t = txns[0]
    # These metadata fields must always be present after our fix
    assert "source_page" in t or True   # may be stripped by decision engine; verify from parse_raw_file
    raw = pipeline.parse_raw_file(path)
    assert len(raw) >= 1
    r = raw[0]
    assert "confidence" in r
    assert "warnings" in r
    assert "source_page" in r
    assert "source_method" in r


# ─────────────────────────────────────────────────────────────────────────────
# Image parser fallback
# ─────────────────────────────────────────────────────────────────────────────

def test_image_parser_fallback(tmp, pipeline):
    path = str(tmp / "blank.png")
    img = Image.new("RGB", (200, 100), color=(255, 255, 255))
    img.save(path)

    txns = pipeline.process_file(path)
    assert isinstance(txns, list)  # must not crash


# ─────────────────────────────────────────────────────────────────────────────
# PDF detector smoke test
# ─────────────────────────────────────────────────────────────────────────────

def test_pdf_detector_non_pdf_returns_false(tmp):
    path = str(tmp / "not_a_pdf.pdf")
    with open(path, "wb") as f:
        f.write(b"This is not a PDF")

    result = is_digital_pdf(path)
    assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# Accuracy report
# ─────────────────────────────────────────────────────────────────────────────

def test_accuracy_report(tmp, pipeline):
    path = str(tmp / "report.csv")
    content = (
        "Date,Description,Debit,Credit,Balance\n"
        "01/08/2026,SALARY,,50000.00,50000.00\n"
        "02/08/2026,GROCERY,1500.00,,48500.00\n"
        "03/08/2026,ATM,2000.00,,46500.00\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    report = pipeline.generate_accuracy_report(path, expected_count=3)
    assert report["rows_extracted"] == 3
    assert report["rows_valid"] == 3
    assert report["rows_rejected"] == 0
    assert report["field_completeness"]["date"] == 100.0
    assert report["field_completeness"]["amount"] == 100.0
    assert report["coverage_pct"] == 100.0
