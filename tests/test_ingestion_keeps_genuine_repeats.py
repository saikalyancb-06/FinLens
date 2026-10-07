"""Ingestion must keep every real row and read direction correctly (2026-10-07).

Found while auditing the filters: the totals every filter narrows were wrong
before any filter was applied.

* Two same-day UPI payments of the same amount to the same merchant were
  collapsed by the validator, and same-day repeats were superseded by the
  dedup engine — even though the statement printed a different running balance
  on each. A duplicate repeats the row INCLUDING its balance; a different
  balance means a different transaction (the rule agreed for Credit Lens).
* Bank PDFs now go through the balance-verified extractor first, the same one
  the statement API uses, and fall back to the legacy parser only when it
  cannot reconcile every row.
"""
import uuid
from datetime import date

import pytest

import app.database.session as dbm
from app.models.account import Account
from app.models.transaction import Direction, SourceType, Transaction
from app.models.user import User
from app.parsers.pipeline import TransactionParsingPipeline
from app.parsers.validator import TransactionValidator
from app.services.deduplication_engine import DeduplicationEngine


def _upi(balance, ref="UPI/AMAZON/shopping"):
    return {"date": "2025-10-12", "description": ref, "debit": 1999.0, "credit": 0.0, "amount": 1999.0,
            "balance": balance, "transaction_type": "UPI", "reference_number": "", "raw_text": ref}


def test_validator_keeps_same_day_upi_repeats_with_different_balances():
    res = TransactionValidator().validate([_upi(1010717.0), _upi(1008718.0)])
    assert len(res.transactions) == 2


def test_validator_still_drops_a_row_repeated_with_its_balance():
    res = TransactionValidator().validate([_upi(1010717.0), _upi(1010717.0)])
    assert len(res.transactions) == 1


@pytest.fixture
def account():
    db = dbm.SessionLocal()
    uid = uuid.uuid4()
    db.add(User(id=uid, email=f"dd_{uid.hex[:8]}@kredo.in", hashed_password="x"))
    acc = Account(id=uuid.uuid4(), user_id=uid, bank_code="TEST", account_number_masked="****9")
    db.add(acc)
    db.commit()
    yield db, uid, acc.id
    db.close()


def _row(uid, acc, bal, i):
    return Transaction(user_id=uid, account_id=acc, txn_date=date(2025, 10, 12), debit_paise=199900,
                       balance_paise=bal, narration_raw="UPI/AMAZON/shopping", row_index=i,
                       direction=Direction.DEBIT, source_type=SourceType.STATEMENT)


def test_dedup_keeps_rows_whose_balances_differ(account):
    db, uid, acc = account
    db.add_all([_row(uid, acc, 101071700, 1), _row(uid, acc, 100871800, 2)])
    db.commit()
    DeduplicationEngine(db, uid, acc).run_deduplication()
    assert db.query(Transaction).filter(Transaction.account_id == acc,
                                        Transaction.superseded_by_id.is_(None)).count() == 2


def test_dedup_still_merges_a_true_repeat(account):
    db, uid, acc = account
    db.add_all([_row(uid, acc, 101071700, 1), _row(uid, acc, 101071700, 2)])
    db.commit()
    DeduplicationEngine(db, uid, acc).run_deduplication()
    assert db.query(Transaction).filter(Transaction.account_id == acc,
                                        Transaction.superseded_by_id.is_(None)).count() == 1


ROWS = [("04-08-2025", "INB/IFT/ACME TRADERS/TPARTY TRANSFER", "50000.00", "CR", "381675.14"),
        ("05-08-2025", "ACH-DR-DEUTSCHE BANK-350041027230019", "178106.00", "DR", "203569.14"),
        ("07-08-2025", "INB/IFT/ACME TRADERS/TPARTY TRANSFER", "200000.00", "CR", "403569.14"),
        ("07-08-2025", "NEFT/EB/AXOEB21971423233/SUGUMARAN M", "14873.00", "DR", "388696.14"),
        ("12-08-2025", "RTGS/UTIBR52025081200012345/CLIENT ALPHA", "125000.00", "CR", "513696.14")]


def _axis_style_pdf(path, rows):
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=8)
    pdf.cell(0, 6, "Statement of Axis Account No :916020011912194 for the period "
                   "(From : 01-08-2025 To : 31-08-2025)", new_x="LMARGIN", new_y="NEXT")
    widths = (22, 22, 70, 24, 12, 30)
    table = [("Tran Date", "Value Date", "Transaction Particulars", "Amount(INR)", "DR/CR", "Balance(INR)"),
             ("", "", "OPENING BALANCE", "", "", "331675.14")] + [(d, d, n, a, t, b) for d, n, a, t, b in rows]
    for r in table:
        for w, v in zip(widths, r):
            pdf.cell(w, 6, v, border=1)
        pdf.ln()
    pdf.output(str(path))
    return str(path)


def test_pdf_ingestion_uses_the_verified_extractor(tmp_path):
    rows = TransactionParsingPipeline().parse_raw_file(_axis_style_pdf(tmp_path / "s.pdf", ROWS))
    assert {r["source_method"] for r in rows} == {"verified_extractor"}
    assert [(r["debit"], r["credit"]) for r in rows] == [
        (0.0, 50000.0), (178106.0, 0.0), (0.0, 200000.0), (14873.0, 0.0), (0.0, 125000.0)]


def test_pdf_that_does_not_reconcile_falls_back(tmp_path):
    broken = list(ROWS)
    broken[2] = ("07-08-2025", "INB/IFT/ACME TRADERS/TPARTY TRANSFER", "200000.00", "CR", "999999.99")
    rows = TransactionParsingPipeline().parse_raw_file(_axis_style_pdf(tmp_path / "b.pdf", broken))
    assert rows and "verified_extractor" not in {r.get("source_method") for r in rows}
