import pytest
import uuid
from datetime import date

from app.database.session import Base
from tests.pgtestdb import make_isolated_engine
from app.models.user import User
from app.models.account import Account
from app.models.transaction import Transaction, SourceType, Direction
from app.models.reconciliation import (
    ImportBatch, BookEntry, ReconciliationRun, RunVerdictEnum
)
from app.services.reconciliation_engine import ReconciliationMatchingEngine


@pytest.fixture
def db():
    # A dedicated PostgreSQL database, reset for each use — replaces the old
    # sqlite:///:memory: engine so this module runs on the production backend.
    engine, Session = make_isolated_engine("ui_reports")
    Base.metadata.create_all(bind=engine)
    session = Session()
    yield session
    session.close()


def make_user_account(db):
    user = User(
        id=uuid.uuid4(),
        email=f"test_{uuid.uuid4().hex[:6]}@example.com",
        hashed_password="hash",
        full_name="Report Test User",
        is_active=True
    )
    acc = Account(
        id=uuid.uuid4(),
        user_id=user.id,
        bank_code="HDFC",
        account_number_masked="****1234",
        account_type="SAVINGS"
    )
    db.add_all([user, acc])
    db.commit()
    return user, acc


def test_ui_report_clean_reconciliation(db):
    """
    Requirement: Clean case
    Timing differences = 0, Journal entries required = 0 -> Verdict = reconciled_clean
    """
    user, account = make_user_account(db)
    today = date(2026, 3, 15)

    batch = ImportBatch(id=uuid.uuid4(), user_id=user.id, account_id=account.id, filename="books.csv", file_sha256="hash1", column_mapping_json={})
    db.add(batch)
    db.add(BookEntry(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, import_batch_id=batch.id,
        entry_date=today, instrument_no="TX1", money_in_paise=100000, money_out_paise=0,
        row_index=1, source_row_hash="h1"
    ))
    db.add(Transaction(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, value_date=today, txn_date=today,
        direction=Direction.CREDIT, source_type=SourceType.STATEMENT, narration_clean="TX1 DEPOSIT",
        credit_paise="100000", debit_paise="0", balance_paise="100000"
    ))
    db.commit()

    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    run = engine.execute_run()

    book_items = [i for i in run.items if i.side == "book"]
    bank_items = [i for i in run.items if i.side == "bank"]

    assert len(book_items) == 0  # Timing differences = 0
    assert len(bank_items) == 0  # Journal entries required = 0
    assert run.residual_paise == 0
    assert run.verdict == RunVerdictEnum.RECONCILED_CLEAN.value


def test_ui_report_test_2_exact_counts_and_no_duplication(db):
    """
    Requirement: Test 2 Outstanding case
    Timing differences = 2
    Journal entries required = 1
    Verdict = reconciled_with_exceptions ("Reconciled with Outstanding Items")
    """
    user, account = make_user_account(db)
    today = date(2026, 3, 15)

    batch = ImportBatch(id=uuid.uuid4(), user_id=user.id, account_id=account.id, filename="books.csv", file_sha256="hash2", column_mapping_json={})
    db.add(batch)

    # Timing 1 (+₹3,000): Cheque issued but not cleared
    db.add(BookEntry(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, import_batch_id=batch.id,
        entry_date=today, instrument_no="CHQ_OUT", money_out_paise=300000, money_in_paise=0,
        row_index=1, source_row_hash="hb1"
    ))

    # Timing 2 (-₹10,000): Payment made/uncleared deposit in books
    db.add(BookEntry(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, import_batch_id=batch.id,
        entry_date=today, instrument_no="DEP_IN", money_in_paise=1000000, money_out_paise=0,
        row_index=2, source_row_hash="hb2"
    ))

    # Bank-only interest: +₹9,900 (990,000 paise credit)
    db.add(Transaction(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, value_date=today, txn_date=today,
        direction=Direction.CREDIT, source_type=SourceType.STATEMENT, narration_clean="INTEREST CREDIT FOR Q4",
        credit_paise="990000", debit_paise="0", balance_paise="5290000"
    ))
    db.commit()

    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    run = engine.execute_run()

    book_items = [i for i in run.items if i.side == "book"]
    bank_items = [i for i in run.items if i.side == "bank"]

    # Verify KPI counts
    assert len(book_items) == 2  # Timing differences = 2
    assert len(bank_items) == 1  # Journal entries required = 1 (Interest received)

    # Verify accounting identity & verdict
    assert run.residual_paise == 0
    assert run.verdict == RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value


def test_ui_report_non_zero_residual_case_a_bank_shows_more(db):
    """
    Deterministic Difference Test CASE A:
    Explicit Book Opening = ₹50,000 (5,000,000 paise)
    Book entry timing = +₹3,000 out (300,000 paise) -> Book closing = ₹47,000
    Bank interest = +₹9,900 in (990,000 paise) -> Computed bank closing = ₹59,900 (5,990,000 paise)
    Actual Bank Closing = ₹65,000 (6,500,000 paise)
    residual = 6,500,000 - 5,990,000 = +510,000 paise > 0
    verdict = unreconciled ("Difference Found — Needs Attention")
    UI direction text = "bank shows more than books"
    """
    user, account = make_user_account(db)
    today = date(2026, 3, 15)

    batch = ImportBatch(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id,
        filename="books.csv", file_sha256="hash3a", column_mapping_json={},
        book_opening_paise=5000000
    )
    db.add(batch)

    # Book entry (+₹3,000 unpresented cheque timing difference)
    db.add(BookEntry(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, import_batch_id=batch.id,
        entry_date=today, instrument_no="CHQ_OUT", money_out_paise=300000, money_in_paise=0,
        row_index=1, source_row_hash="hb3a"
    ))

    # Bank transaction with actual statement balance = ₹65,000 (6,500,000 paise)
    db.add(Transaction(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, value_date=today, txn_date=today,
        direction=Direction.CREDIT, source_type=SourceType.STATEMENT, narration_clean="INTEREST CREDIT FOR Q4",
        credit_paise="990000", debit_paise="0", balance_paise="6500000"
    ))
    db.commit()

    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    run = engine.execute_run()

    assert run.residual_paise == 6500000 - 5990000  # +510,000 paise > 0
    assert run.residual_paise > 0
    assert run.verdict == RunVerdictEnum.UNRECONCILED.value

    with open("app/static/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    assert "bank shows more than books" in html


def test_ui_report_non_zero_residual_case_b_books_show_more(db):
    """
    Deterministic Difference Test CASE B:
    Explicit Book Opening = ₹50,000 (5,000,000 paise)
    Book entry timing = +₹3,000 out (300,000 paise) -> Book closing = ₹47,000
    Bank interest = +₹9,900 in (990,000 paise) -> Computed bank closing = ₹59,900 (5,990,000 paise)
    Actual Bank Closing = ₹55,000 (5,500,000 paise)
    residual = 5,500,000 - 5,990,000 = -490,000 paise < 0
    verdict = unreconciled ("Difference Found — Needs Attention")
    UI direction text = "books show more than bank"
    """
    user, account = make_user_account(db)
    today = date(2026, 3, 15)

    batch = ImportBatch(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id,
        filename="books.csv", file_sha256="hash3b", column_mapping_json={},
        book_opening_paise=5000000
    )
    db.add(batch)

    db.add(BookEntry(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, import_batch_id=batch.id,
        entry_date=today, instrument_no="CHQ_OUT", money_out_paise=300000, money_in_paise=0,
        row_index=1, source_row_hash="hb3b"
    ))

    # Bank transaction with actual statement balance = ₹55,000 (5,500,000 paise)
    db.add(Transaction(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, value_date=today, txn_date=today,
        direction=Direction.CREDIT, source_type=SourceType.STATEMENT, narration_clean="INTEREST CREDIT FOR Q4",
        credit_paise="990000", debit_paise="0", balance_paise="5500000"
    ))
    db.commit()

    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    run = engine.execute_run()

    assert run.residual_paise == 5500000 - 5990000  # -490,000 paise < 0
    assert run.residual_paise < 0
    assert run.verdict == RunVerdictEnum.UNRECONCILED.value

    with open("app/static/index.html", "r", encoding="utf-8") as f:
        html = f.read()
    assert "books show more than bank" in html


def test_ui_report_grammar_and_duplication_checks():
    """
    Verify grammar fix (1 transaction requires action) and badge duplication prevention.
    """
    with open("app/static/index.html", "r", encoding="utf-8") as f:
        html = f.read()

    assert "${actionCount} transaction${actionCount === 1 ? ' requires' : 's require'} action." in html
    assert "showExcBadge" in html
    assert "JOURNAL ENTRIES REQUIRED" in html
