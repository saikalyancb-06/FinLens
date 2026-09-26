import uuid
import pytest
from datetime import date
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.account import Account
from app.models.statement import Statement
from app.models.transaction import Transaction, Direction, SourceType
from app.models.reconciliation import (
    ImportBatch, BookEntry, ReconciliationRun, ReconciliationMatch,
    ReconciliationItem, MatchStatusEnum, ReconciliationStatusEnum
)
from app.services.reconciliation_engine import ReconciliationMatchingEngine, extract_reference_tokens, compare_references
from app.database.session import Base
from tests.pgtestdb import make_isolated_engine

@pytest.fixture
def db_session():
    # A dedicated PostgreSQL database, reset per test — replaces the old
    # sqlite:///:memory: engine so these cases run on the production backend.
    engine, Session = make_isolated_engine("controlled")
    Base.metadata.create_all(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()



def setup_user_and_account(db: Session, prefix: str):
    user = User(
        email=f"{prefix}_{uuid.uuid4().hex[:6]}@example.com",
        hashed_password="hashed",
        full_name="Controlled Test User",
        is_active=True
    )
    db.add(user)
    db.flush()

    account = Account(
        user_id=user.id,
        bank_code="HDFC",
        account_number_masked="****5241",
        account_type="CURRENT"
    )
    db.add(account)
    db.flush()

    return user, account


def create_import_batch(db: Session, user_id: uuid.UUID, account_id: uuid.UUID):
    batch = ImportBatch(
        user_id=user_id,
        account_id=account_id,
        filename="controlled_ledger.csv",
        file_sha256="test_hash",
        column_mapping_json={"entry_date": "Date", "instrument_no": "Reference"},
        row_count=10,
        period_from=date(2025, 3, 1),
        period_to=date(2025, 3, 30),
        book_opening_paise=0,
        book_closing_paise=1000000
    )
    db.add(batch)
    db.flush()
    return batch


def test_controlled_case_1_exact_match(db_session: Session):
    """1. EXACT_MATCH: Same direction + exact amount + matching reference."""
    user, account = setup_user_and_account(db_session, "exact_match")
    batch = create_import_batch(db_session, user.id, account.id)

    # Bank Credit
    t = Transaction(
        user_id=user.id,
        account_id=account.id,
        txn_date=date(2025, 3, 28),
        narration_raw="NEFT-YESCB50870093682-RESILIENT INNOVATIONS PVT LTD",
        reference_no="YESCB50870093682",
        credit_paise=2186100, # ₹21,861.00
        direction=Direction.CREDIT,
        source_type=SourceType.STATEMENT
    )
    db_session.add(t)

    # Ledger Credit (Money In)
    b = BookEntry(
        user_id=user.id,
        account_id=account.id,
        import_batch_id=batch.id,
        entry_date=date(2025, 3, 28),
        instrument_no="YESCB50870093682",
        narration="NEFT-YESCB50870093682",
        money_in_paise=2186100,
        money_out_paise=0,
        row_index=1,
        source_row_hash="hash_exact_1"
    )
    db_session.add(b)
    db_session.commit()

    engine = ReconciliationMatchingEngine(
        db=db_session, user_id=user.id, account_id=account.id,
        period_from=date(2025, 3, 1), period_to=date(2025, 3, 30)
    )
    run = engine.execute_run(import_batch_id=batch.id, force=True)

    assert run.matched_count == 1
    assert hasattr(run, "debug_log")
    exact_matches = [d for d in run.debug_log if d["match_status"] == "EXACT_MATCH"]
    assert len(exact_matches) == 1
    assert exact_matches[0]["bank_amount"] == 21861.0
    assert exact_matches[0]["ledger_amount"] == 21861.0


def test_controlled_case_2_bank_only(db_session: Session):
    """2. BANK_ONLY: Transaction present in bank statement only."""
    user, account = setup_user_and_account(db_session, "bank_only")
    batch = create_import_batch(db_session, user.id, account.id)

    # Bank Debit (Unexplained Charge)
    t = Transaction(
        user_id=user.id,
        account_id=account.id,
        txn_date=date(2025, 3, 15),
        narration_raw="CGTMSE FEE FY 2024-25 06/5241",
        debit_paise=655416,
        direction=Direction.DEBIT,
        source_type=SourceType.STATEMENT
    )
    db_session.add(t)
    db_session.commit()

    engine = ReconciliationMatchingEngine(
        db=db_session, user_id=user.id, account_id=account.id,
        period_from=date(2025, 3, 1), period_to=date(2025, 3, 30)
    )
    run = engine.execute_run(import_batch_id=batch.id, force=True)

    assert run.unmatched_bank_count == 1
    items = db_session.query(ReconciliationItem).filter(ReconciliationItem.run_id == run.id).all()
    assert len(items) == 1
    assert items[0].side.value == "bank"
    assert items[0].brs_category == "bank_charge"


def test_controlled_case_3_ledger_only(db_session: Session):
    """3. LEDGER_ONLY: Entry present in ledger only."""
    user, account = setup_user_and_account(db_session, "ledger_only")
    batch = create_import_batch(db_session, user.id, account.id)

    # Ledger Debit (Money Out)
    b = BookEntry(
        user_id=user.id,
        account_id=account.id,
        import_batch_id=batch.id,
        entry_date=date(2025, 3, 1),
        instrument_no="LEDGER-ONLY-001",
        narration="OFFICE RENT - TEST LEDGER ONLY",
        money_in_paise=0,
        money_out_paise=1200000, # ₹12,000.00
        row_index=1,
        source_row_hash="hash_ledger_only"
    )
    db_session.add(b)
    db_session.commit()

    engine = ReconciliationMatchingEngine(
        db=db_session, user_id=user.id, account_id=account.id,
        period_from=date(2025, 3, 1), period_to=date(2025, 3, 30)
    )
    run = engine.execute_run(import_batch_id=batch.id, force=True)

    assert run.unmatched_book_count == 1
    items = db_session.query(ReconciliationItem).filter(ReconciliationItem.run_id == run.id).all()
    assert len(items) == 1
    assert items[0].side.value == "book"
    assert items[0].brs_category == "unpresented_cheque"


def test_controlled_case_4_amount_mismatch(db_session: Session):
    """4. AMOUNT_MISMATCH: Same reference & direction, but different amounts."""
    user, account = setup_user_and_account(db_session, "amount_mismatch")
    batch = create_import_batch(db_session, user.id, account.id)

    # Bank Credit ₹12,460.00
    t = Transaction(
        user_id=user.id,
        account_id=account.id,
        txn_date=date(2025, 3, 30),
        narration_raw="UPI/508966282233/04:20:46/UPI/bharatpe payouts@yes",
        reference_no="508966282233",
        credit_paise=1246000,
        direction=Direction.CREDIT,
        source_type=SourceType.STATEMENT
    )
    db_session.add(t)

    # Ledger Credit ₹12,400.00
    b = BookEntry(
        user_id=user.id,
        account_id=account.id,
        import_batch_id=batch.id,
        entry_date=date(2025, 3, 30),
        instrument_no="508966282233",
        narration="UPI/508966282233",
        money_in_paise=1240000,
        money_out_paise=0,
        row_index=1,
        source_row_hash="hash_amt_mismatch"
    )
    db_session.add(b)
    db_session.commit()

    engine = ReconciliationMatchingEngine(
        db=db_session, user_id=user.id, account_id=account.id,
        period_from=date(2025, 3, 1), period_to=date(2025, 3, 30)
    )
    run = engine.execute_run(import_batch_id=batch.id, force=True)

    mismatch_entries = [d for d in run.debug_log if d["match_status"] == "AMOUNT_MISMATCH"]
    assert len(mismatch_entries) == 1
    assert mismatch_entries[0]["bank_amount"] == 12460.0
    assert mismatch_entries[0]["ledger_amount"] == 12400.0


def test_controlled_case_5_date_mismatch(db_session: Session):
    """5. DATE_MISMATCH: Bank date = 29-Mar, Ledger date = 28-Mar, reference & amount match."""
    user, account = setup_user_and_account(db_session, "date_mismatch")
    batch = create_import_batch(db_session, user.id, account.id)

    # Bank Credit 29-Mar
    t = Transaction(
        user_id=user.id,
        account_id=account.id,
        txn_date=date(2025, 3, 29),
        narration_raw="NEFT-YESCB50880037964-RESILIENT INNOVATIONS PVT LTD",
        reference_no="YESCB50880037964",
        credit_paise=2587400,
        direction=Direction.CREDIT,
        source_type=SourceType.STATEMENT
    )
    db_session.add(t)

    # Ledger Credit 28-Mar
    b = BookEntry(
        user_id=user.id,
        account_id=account.id,
        import_batch_id=batch.id,
        entry_date=date(2025, 3, 28),
        instrument_no="YESCB50880037964",
        narration="NEFT-YESCB50880037964",
        money_in_paise=2587400,
        money_out_paise=0,
        row_index=1,
        source_row_hash="hash_date_mismatch"
    )
    db_session.add(b)
    db_session.commit()

    engine = ReconciliationMatchingEngine(
        db=db_session, user_id=user.id, account_id=account.id,
        period_from=date(2025, 3, 1), period_to=date(2025, 3, 30)
    )
    run = engine.execute_run(import_batch_id=batch.id, force=True)

    date_mismatches = [d for d in run.debug_log if d["match_status"] == "DATE_MISMATCH"]
    assert len(date_mismatches) == 1
    assert date_mismatches[0]["bank_date"] == "2025-03-29"
    assert date_mismatches[0]["ledger_date"] == "2025-03-28"


def test_controlled_case_6_duplicate_ledger_row(db_session: Session):
    """6. DUPLICATE: One bank transaction, two candidate ledger rows with same reference & amount."""
    user, account = setup_user_and_account(db_session, "duplicate_case")
    batch = create_import_batch(db_session, user.id, account.id)

    # Single Bank Credit
    t = Transaction(
        user_id=user.id,
        account_id=account.id,
        txn_date=date(2025, 3, 27),
        narration_raw="NEFT-YESCB50860036730-RESILIENT INNOVATIONS PVT LTD",
        reference_no="YESCB50860036730",
        credit_paise=1351100,
        direction=Direction.CREDIT,
        source_type=SourceType.STATEMENT
    )
    db_session.add(t)

    # Ledger Row 1
    b1 = BookEntry(
        user_id=user.id,
        account_id=account.id,
        import_batch_id=batch.id,
        entry_date=date(2025, 3, 27),
        instrument_no="YESCB50860036730",
        narration="NEFT-YESCB50860036730",
        money_in_paise=1351100,
        money_out_paise=0,
        row_index=1,
        source_row_hash="hash_dup_1"
    )
    db_session.add(b1)

    # Ledger Row 2 (Duplicate)
    b2 = BookEntry(
        user_id=user.id,
        account_id=account.id,
        import_batch_id=batch.id,
        entry_date=date(2025, 3, 27),
        instrument_no="YESCB50860036730-DUP",
        narration="NEFT-YESCB50860036730 DUPLICATE TEST",
        money_in_paise=1351100,
        money_out_paise=0,
        row_index=2,
        source_row_hash="hash_dup_2"
    )
    db_session.add(b2)
    db_session.commit()

    engine = ReconciliationMatchingEngine(
        db=db_session, user_id=user.id, account_id=account.id,
        period_from=date(2025, 3, 1), period_to=date(2025, 3, 30)
    )
    run = engine.execute_run(import_batch_id=batch.id, force=True)

    dup_entries = [d for d in run.debug_log if d["match_status"] == "DUPLICATE"]
    assert len(dup_entries) == 1
    assert dup_entries[0]["ledger_transaction_id"] == str(b2.id)
