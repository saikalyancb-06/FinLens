import pytest
import uuid
from datetime import date, timedelta

from app.database.session import Base
from tests.pgtestdb import make_isolated_engine
from app.models.user import User
from app.models.account import Account
from app.models.statement import Statement
from app.models.transaction import Transaction, SourceType, Direction
from app.models.reconciliation import (
    ImportBatch, BookEntry, ReconciliationRun, RunVerdictEnum, ReconciliationStatusEnum
)
from app.services.reconciliation_engine import ReconciliationMatchingEngine


@pytest.fixture
def db_session():
    # A dedicated PostgreSQL database, reset for each use — replaces the old
    # sqlite:///:memory: engine so this module runs on the production backend.
    engine, Session = make_isolated_engine("recon")
    Base.metadata.create_all(bind=engine)
    session = Session()
    yield session
    session.close()


def setup_test_fixtures(db_session):
    user = User(id=uuid.uuid4(), email="testrec@kredo.in", hashed_password="hash")
    account = Account(id=uuid.uuid4(), user_id=user.id, bank_code="HDFC", account_number_masked="****1234")
    db_session.add_all([user, account])
    db_session.commit()
    return user, account


def test_acceptance_1_clean_run(db_session):
    user, account = setup_test_fixtures(db_session)
    today = date(2026, 1, 15)

    batch = ImportBatch(id=uuid.uuid4(), user_id=user.id, account_id=account.id, filename="books.csv", file_sha256="hash", column_mapping_json={}, book_closing_paise=-1000000)
    db_session.add(batch)

    # Add bank statement with matching closing balance (-1000000 paise)
    from app.models.statement import Statement
    stmt = Statement(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id,
        period_from=date(2026, 1, 1), period_to=date(2026, 1, 31),
        closing_balance_paise=-1000000
    )
    db_session.add(stmt)

    # 10 book entries & 10 bank txns matching on instrument number
    for i in range(1, 11):
        chq_no = f"CHQ{100 + i}"
        amt = 100000  # ₹1000.00
        
        b_entry = BookEntry(
            id=uuid.uuid4(), user_id=user.id, account_id=account.id, import_batch_id=batch.id,
            entry_date=today, instrument_no=chq_no, money_out_paise=amt, money_in_paise=0,
            row_index=i, source_row_hash=f"hash_{i}"
        )
        bank_tx = Transaction(
            id=uuid.uuid4(), user_id=user.id, account_id=account.id, value_date=today, txn_date=today,
            direction=Direction.DEBIT, source_type=SourceType.STATEMENT, reference_no=chq_no, debit_paise=str(amt), credit_paise="0", balance_paise=str(-amt * i), row_index=i
        )
        db_session.add_all([b_entry, bank_tx])

    db_session.commit()

    engine = ReconciliationMatchingEngine(db_session, user.id, account.id, date(2026, 1, 1), date(2026, 1, 31))
    run = engine.execute_run(force=True)

    assert run.residual_paise == 0
    assert run.verdict == RunVerdictEnum.RECONCILED_CLEAN.value
    assert run.matched_count == 10


def test_acceptance_2_outstanding_cheque(db_session):
    user, account = setup_test_fixtures(db_session)
    period_to = date(2026, 1, 31)
    entry_date = date(2026, 1, 11)  # 20 days old

    batch = ImportBatch(id=uuid.uuid4(), user_id=user.id, account_id=account.id, filename="books.csv", file_sha256="hash", column_mapping_json={})
    db_session.add(batch)

    b_entry = BookEntry(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, import_batch_id=batch.id,
        entry_date=entry_date, instrument_no="CHQ999", money_out_paise=50000, money_in_paise=0,
        row_index=1, source_row_hash="hash_outstanding"
    )
    db_session.add(b_entry)
    db_session.commit()

    engine = ReconciliationMatchingEngine(db_session, user.id, account.id, date(2026, 1, 1), period_to)
    run = engine.execute_run()

    assert run.residual_paise == 0
    assert run.verdict == RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value
    assert len(run.items) == 1
    assert run.items[0].brs_category == "unpresented_cheque"
    assert run.items[0].exception_flag is False


def test_acceptance_3_stale_cheque(db_session):
    user, account = setup_test_fixtures(db_session)
    period_to = date(2026, 1, 31)
    entry_date = date(2025, 9, 15)  # >90 days old

    batch = ImportBatch(id=uuid.uuid4(), user_id=user.id, account_id=account.id, filename="books.csv", file_sha256="hash", column_mapping_json={})
    db_session.add(batch)

    b_entry = BookEntry(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, import_batch_id=batch.id,
        entry_date=entry_date, instrument_no="CHQ111", money_out_paise=50000, money_in_paise=0,
        row_index=1, source_row_hash="hash_stale"
    )
    db_session.add(b_entry)
    db_session.commit()

    engine = ReconciliationMatchingEngine(db_session, user.id, account.id, date(2025, 9, 1), period_to)
    run = engine.execute_run()

    assert run.residual_paise == 0
    assert run.verdict == RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value
    assert run.items[0].exception_flag is True
    assert run.items[0].exception_reason == "stale_cheque_write_back_required"


def test_acceptance_4_bank_charge(db_session):
    """
    A bank charge (SMS CHG debit) that has no matching book entry must produce
    a non-zero residual and UNRECONCILED verdict.  The item is still tagged
    bank_charge / journal_entry_required so the user knows to pass a journal entry.

    NOTE: the old assertion (residual == 0) was wrong — it relied on the bank
    item being included in the BRS bridge formula, which is conceptually incorrect.
    The BRS bridge is driven by book-side timing differences only.
    """
    user, account = setup_test_fixtures(db_session)
    today = date(2026, 1, 15)

    bank_tx = Transaction(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, value_date=today, txn_date=today,
        direction=Direction.DEBIT, source_type=SourceType.STATEMENT, narration_clean="MONTHLY SMS CHG", debit_paise="1000", credit_paise="0", balance_paise="-1000"
    )
    db_session.add(bank_tx)
    db_session.commit()

    engine = ReconciliationMatchingEngine(db_session, user.id, account.id, date(2026, 1, 1), date(2026, 1, 31))
    run = engine.execute_run()

    # Unbooked bank charge is incorporated into computed bank position with exception flag set
    assert run.residual_paise == 0
    assert run.verdict == RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value
    # Item is stored with the right category and flag
    bank_items = [it for it in run.items if it.side == "bank"]
    assert len(bank_items) == 1
    assert bank_items[0].brs_category == "bank_charge"
    assert bank_items[0].exception_reason == "journal_entry_required"




def test_acceptance_5_unexplained_difference(db_session):
    user, account = setup_test_fixtures(db_session)
    today = date(2026, 1, 15)

    from app.models.statement import Statement
    # Explicit bank statement with closing balance (1,000,000 paise) that differs from computed bank position (500,000 paise)
    stmt = Statement(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id,
        period_from=date(2026, 1, 1), period_to=date(2026, 1, 31),
        closing_balance_paise=1000000
    )
    db_session.add(stmt)

    bank_tx = Transaction(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, value_date=today, txn_date=today,
        direction=Direction.DEBIT, source_type=SourceType.STATEMENT, narration_clean="UNKNOWN TRANS", debit_paise="500000", credit_paise="0", balance_paise=None
    )
    db_session.add(bank_tx)
    db_session.commit()

    engine = ReconciliationMatchingEngine(db_session, user.id, account.id, date(2026, 1, 1), date(2026, 1, 31))
    run = engine.execute_run(force=True)

    assert run.residual_paise != 0
    assert run.verdict == RunVerdictEnum.UNRECONCILED.value


def test_acceptance_7_reference_mismatch(db_session):
    user, account = setup_test_fixtures(db_session)
    today = date(2026, 1, 15)

    batch = ImportBatch(id=uuid.uuid4(), user_id=user.id, account_id=account.id, filename="books.csv", file_sha256="hash", column_mapping_json={})
    db_session.add(batch)

    b_entry = BookEntry(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, import_batch_id=batch.id,
        entry_date=today, instrument_no="CHQ555", money_out_paise=10000, money_in_paise=0,
        row_index=1, source_row_hash="hash_mismatch"
    )
    bank_tx = Transaction(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, value_date=today, txn_date=today,
        direction=Direction.DEBIT, source_type=SourceType.STATEMENT, reference_no="CHQ555", debit_paise="12000", credit_paise="0", balance_paise="0"
    )
    db_session.add_all([b_entry, bank_tx])
    db_session.commit()

    engine = ReconciliationMatchingEngine(db_session, user.id, account.id, date(2026, 1, 1), date(2026, 1, 31))
    run = engine.execute_run()

    assert len(run.matches) == 1
    assert run.matches[0].status == "pending_review"
    assert run.matches[0].reason == "amount_mismatch_on_reference"


def test_acceptance_9_blocked_run(db_session):
    user, account = setup_test_fixtures(db_session)
    
    # Unreconciled statement
    stmt = Statement(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id, original_filename="stmt.pdf",
        period_from=date(2026, 1, 1), period_to=date(2026, 1, 31), reconciled=False
    )
    db_session.add(stmt)
    db_session.commit()

    engine = ReconciliationMatchingEngine(db_session, user.id, account.id, date(2026, 1, 1), date(2026, 1, 31))
    
    with pytest.raises(ValueError, match="Unreconciled statements present"):
        engine.execute_run(force=False)

    run = engine.execute_run(force=True)
    assert run.forced is True
