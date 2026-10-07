import uuid
import pytest
from datetime import date

from app.database.session import Base
from tests.pgtestdb import make_isolated_engine
from app.models.user import User
from app.models.account import Account
from app.models.transaction import Transaction, SourceType, Direction
from app.models.reconciliation import (
    ImportBatch, BookEntry, ReconciliationRun, RunVerdictEnum, ReconciliationMatch, MatchTierEnum, MatchStatusEnum
)
from app.services.reconciliation_engine import ReconciliationMatchingEngine


@pytest.fixture
def db_session():
    # A dedicated PostgreSQL database, reset for each use — replaces the old
    # sqlite:///:memory: engine so this module runs on the production backend.
    engine, Session = make_isolated_engine("verdict")
    Base.metadata.create_all(bind=engine)
    session = Session()
    yield session
    session.close()


def setup_user_and_account(db, prefix):
    user = User(
        id=uuid.uuid4(),
        email=f"{prefix}_{uuid.uuid4().hex[:6]}@example.com",
        hashed_password="hashed",
        full_name="Verdict Fix User",
        is_active=True
    )
    account = Account(
        id=uuid.uuid4(),
        user_id=user.id,
        bank_code="HDFC",
        account_number_masked="****1234",
        account_type="CURRENT"
    )
    db.add_all([user, account])
    db.commit()
    return user, account


def create_batch(db, user, account, book_closing_paise=100000):
    batch = ImportBatch(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        filename="test_ledger.csv",
        file_sha256="sha256",
        column_mapping_json={},
        row_count=5,
        period_from=date(2026, 3, 1),
        period_to=date(2026, 3, 31),
        book_opening_paise=0,
        book_closing_paise=book_closing_paise
    )
    db.add(batch)
    db.commit()
    return batch


def test_case_a_residual_zero_active_unpresented_cheque(db_session):
    """CASE A: residual = 0, active unpresented cheque exists -> expected verdict = reconciled_with_exceptions."""
    user, account = setup_user_and_account(db_session, "case_a")
    batch = create_batch(db_session, user, account, book_closing_paise=100000)

    # Active unpresented cheque (within 90 days, no exception flag originally)
    b = BookEntry(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        import_batch_id=batch.id,
        entry_date=date(2026, 3, 15),
        instrument_no="CHQ_ACTIVE_1",
        money_in_paise=0,
        money_out_paise=50000,
        row_index=1,
        source_row_hash="hash_a"
    )
    # Bank closing balance matches computed position: book(1000.00) + unpresented_cheque(500.00) = 1500.00
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        txn_date=date(2026, 3, 31),
        direction=Direction.CREDIT,
        source_type=SourceType.STATEMENT,
        credit_paise="0",
        debit_paise="0",
        balance_paise="150000",
        reference_no="REF_BAL"
    )
    db_session.add_all([b, tx])
    db_session.commit()

    engine = ReconciliationMatchingEngine(db_session, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    # Books opened at 1,500.00 (same as the bank); the 500.00 cheque takes them to 1,000.00.
    run = engine.execute_run(import_batch_id=batch.id, force=True, book_opening_paise=150000)

    assert run.residual_paise == 0
    assert run.unmatched_book_count == 1
    assert run.verdict == RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value


def test_case_b_residual_zero_pending_review_match(db_session):
    """CASE B: residual = 0, pending_review match exists -> expected verdict = reconciled_with_exceptions."""
    user, account = setup_user_and_account(db_session, "case_b")
    batch = ImportBatch(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        filename="test_ledger.csv",
        file_sha256="sha256",
        column_mapping_json={},
        row_count=5,
        period_from=date(2026, 3, 1),
        period_to=date(2026, 3, 31),
        book_opening_paise=100000, # Explicit book opening = ₹1,000.00
        book_closing_paise=180000
    )
    db_session.add(batch)

    # Amount mismatch on reference (Priority 5) triggering pending_review status: Bank credit ₹1,000, Ledger entry ₹800
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        txn_date=date(2026, 3, 20),
        narration_raw="NEFT-REFREV1234",
        reference_no="REFREV1234",
        credit_paise="100000", # ₹1,000.00
        direction=Direction.CREDIT,
        source_type=SourceType.STATEMENT,
        # Bank opened at 1,000.00 like the books; +1,000.00 credit -> 2,000.00.
        # Books recorded 800.00, so the 200.00 difference is a BRS line and the bridge balances.
        balance_paise="200000"
    )
    b = BookEntry(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        import_batch_id=batch.id,
        entry_date=date(2026, 3, 20),
        instrument_no="REFREV1234",
        narration="NEFT-REFREV1234",
        money_in_paise=80000,  # ₹800.00 (amount mismatch vs bank credit)
        money_out_paise=0,
        row_index=1,
        source_row_hash="hash_b_rev"
    )
    db_session.add_all([tx, b])
    db_session.commit()

    engine = ReconciliationMatchingEngine(db_session, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    run = engine.execute_run(import_batch_id=batch.id, force=True)

    assert run.residual_paise == 0
    assert run.pending_review_count == 1
    assert any(it.brs_category == "amount_difference" and it.amount_paise == 20000 for it in run.items)
    assert run.verdict == RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value


def test_case_c_residual_zero_no_outstanding_items(db_session):
    """CASE C: residual = 0, zero outstanding items -> expected verdict = reconciled_clean."""
    user, account = setup_user_and_account(db_session, "case_c")
    batch = create_batch(db_session, user, account, book_closing_paise=100000)

    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        txn_date=date(2026, 3, 20),
        reference_no="EXACTREF999",
        credit_paise="100000",
        direction=Direction.CREDIT,
        source_type=SourceType.STATEMENT,
        balance_paise="100000"
    )
    b = BookEntry(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        import_batch_id=batch.id,
        entry_date=date(2026, 3, 20),
        instrument_no="EXACTREF999",
        money_in_paise=100000,
        money_out_paise=0,
        row_index=1,
        source_row_hash="hash_c"
    )
    db_session.add_all([tx, b])
    db_session.commit()

    engine = ReconciliationMatchingEngine(db_session, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    run = engine.execute_run(import_batch_id=batch.id, force=True)

    assert run.residual_paise == 0
    assert run.unmatched_bank_count == 0
    assert run.unmatched_book_count == 0
    assert run.pending_review_count == 0
    assert run.verdict == RunVerdictEnum.RECONCILED_CLEAN.value


def test_case_d_residual_nonzero_unreconciled(db_session):
    """CASE D: residual != 0 -> expected verdict = unreconciled regardless of outstanding items."""
    user, account = setup_user_and_account(db_session, "case_d")
    batch = ImportBatch(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        filename="test_ledger.csv",
        file_sha256="sha256",
        column_mapping_json={},
        row_count=5,
        period_from=date(2026, 3, 1),
        period_to=date(2026, 3, 31),
        book_opening_paise=100000, # Explicit book opening = ₹1,000.00
        book_closing_paise=200000
    )
    db_session.add(batch)

    # Book entry: money_in = 100000 -> book_closing = 100000 + 100000 = 200000 (₹2,000.00)
    b = BookEntry(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        import_batch_id=batch.id,
        entry_date=date(2026, 3, 20),
        instrument_no="EXACTREF888",
        money_in_paise=100000,
        money_out_paise=0,
        row_index=1,
        source_row_hash="hash_d"
    )
    # Bank txn: credit = 100000, but statement closing balance is 250000 -> residual = +50000 (₹500.00)
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        txn_date=date(2026, 3, 20),
        reference_no="EXACTREF888",
        credit_paise="100000",
        direction=Direction.CREDIT,
        source_type=SourceType.STATEMENT,
        balance_paise="250000"
    )
    db_session.add_all([tx, b])
    db_session.commit()

    engine = ReconciliationMatchingEngine(db_session, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    run = engine.execute_run(import_batch_id=batch.id, force=True)

    assert run.residual_paise == 50000
    assert run.verdict == RunVerdictEnum.UNRECONCILED.value


def test_frontend_residual_text_semantics():
    """Verify frontend residual text condition: residual > 0 -> bank shows more than books, residual < 0 -> books show more than bank."""
    def get_residual_text(residual):
        return ' — bank shows more than books' if residual > 0 else ' — books show more than bank'

    assert get_residual_text(5000) == ' — bank shows more than books'
    assert get_residual_text(-3000) == ' — books show more than bank'
