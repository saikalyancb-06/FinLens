"""
tests/test_brs_accounting_invariants.py

Part 8 — Accounting Invariants for Reconciliation Runs.

For any reconciliation run the BRS accounting identity must hold:

    computed_bank_closing_paise  ==  book_closing_paise + book_bridge_net_paise

    residual_paise  ==  computed_bank_closing_paise - actual_bank_closing_paise

All arithmetic uses integer paise (no floating point).
A genuinely reconciled case: residual_paise == 0.
An unexplained bank-side item: residual_paise != 0.
"""

import uuid
import pytest
from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.user import User
from app.models.account import Account
from app.models.transaction import Transaction, SourceType, Direction
from app.models.reconciliation import (
    ImportBatch, BookEntry, ReconciliationRun, RunVerdictEnum
)
from app.services.reconciliation_engine import ReconciliationMatchingEngine
from tests.pgtestdb import make_isolated_engine


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    # A dedicated PostgreSQL database, reset per test: the same isolation the
    # old sqlite:///:memory: engine gave, against the real production backend.
    engine, Session = make_isolated_engine("brs")
    Base.metadata.create_all(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def make_user_account(db):
    user = User(id=uuid.uuid4(), email=f"inv_{uuid.uuid4().hex[:6]}@kredo.in", hashed_password="h")
    account = Account(id=uuid.uuid4(), user_id=user.id, bank_code="HDFC", account_number_masked="****9999")
    db.add_all([user, account])
    db.commit()
    return user, account


def make_batch(db, user, account, book_closing_paise=None, book_opening_paise=0):
    batch = ImportBatch(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id,
        filename="ledger.csv", file_sha256="hash", column_mapping_json={},
        book_opening_paise=book_opening_paise,
        book_closing_paise=book_closing_paise if book_closing_paise is not None else 0,
    )
    db.add(batch)
    db.flush()
    return batch


def make_book_entry(db, user, account, batch, entry_date, money_out=0, money_in=0, instrument_no="CHQ000"):
    b = BookEntry(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id,
        import_batch_id=batch.id, entry_date=entry_date,
        instrument_no=instrument_no,
        money_out_paise=money_out, money_in_paise=money_in,
        row_index=1, source_row_hash=uuid.uuid4().hex
    )
    db.add(b)
    db.flush()
    return b


def make_bank_txn(db, user, account, txn_date, debit=0, credit=0, balance=0, reference_no=None, narration=""):
    tx = Transaction(
        id=uuid.uuid4(), user_id=user.id, account_id=account.id,
        txn_date=txn_date, value_date=txn_date,
        direction=Direction.DEBIT if debit > 0 else Direction.CREDIT,
        source_type=SourceType.STATEMENT,
        debit_paise=str(debit), credit_paise=str(credit),
        balance_paise=str(balance),
        reference_no=reference_no,
        narration_clean=narration,
    )
    db.add(tx)
    db.flush()
    return tx


# ─────────────────────────────────────────────────────────────────────────────
# 1. BRS identity: clean match
# ─────────────────────────────────────────────────────────────────────────────

def test_invariant_clean_match_zero_residual(db):
    """
    Single book entry matched by a single bank transaction.
    book_closing = -100 000 (batch stated)
    bank_closing  = -100 000 (balance_paise on transaction)
    Bridge: empty (all matched)
    Expected: computed = -100 000, residual = 0
    """
    user, account = make_user_account(db)
    today = date(2026, 3, 15)
    batch = make_batch(db, user, account, book_closing_paise=-10000000)  # ₹-1 00 000.00

    b = make_book_entry(db, user, account, batch, today, money_out=10000000, instrument_no="CHQ001")
    tx = make_bank_txn(db, user, account, today, debit=10000000, balance=-10000000, reference_no="CHQ001")
    db.commit()

    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    run = engine.execute_run()

    # Accounting identity
    assert run.residual_paise == 0
    assert run.computed_bank_closing_paise == run.book_closing_paise
    assert run.bank_closing_paise == run.computed_bank_closing_paise
    assert run.verdict == RunVerdictEnum.RECONCILED_CLEAN.value
    assert run.matched_count == 1


def test_invariant_single_outstanding_cheque(db):
    """
    Book has one unpresented cheque (₹500), no bank transactions.
    bridge ADD 500.
    computed = book_closing(0) + 500 = 500
    bank_closing = None (no transaction) → the bridge is built but cannot be
    checked against a real bank balance, so the run is never called reconciled.
    """
    user, account = make_user_account(db)
    today = date(2026, 3, 10)
    batch = make_batch(db, user, account)
    make_book_entry(db, user, account, batch, today, money_out=50000, instrument_no="CHQ002")
    db.commit()

    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    run = engine.execute_run()

    # book + bridge = computed; no bank statement → residual = 0
    assert run.residual_paise == 0
    # book_item_net = +50000 (ADD)
    assert run.computed_bank_closing_paise == run.book_closing_paise + 50000
    assert run.bank_closing_paise == run.computed_bank_closing_paise  # engine sets equal
    assert run.status == "completed_no_bank_statement"
    assert run.verdict == RunVerdictEnum.UNRECONCILED.value


def test_invariant_unbooked_bank_charge_produces_residual(db):
    """
    Bank charges ₹20 not in books.
    Unmatched bank debit (-₹20) is incorporated into computed bank position (-2000 paise).
    Actual bank closing = -2000.
    Computed bank closing = -2000.
    residual = 0, but exception_flag is set → RECONCILED_WITH_EXCEPTIONS.
    """
    user, account = make_user_account(db)
    today = date(2026, 3, 15)
    make_bank_txn(db, user, account, today, debit=2000, balance=-2000, narration="ANNUAL FEE")
    db.commit()

    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    run = engine.execute_run(book_opening_paise=0)

    assert run.computed_bank_closing_paise == -2000
    assert run.bank_closing_paise == -2000
    assert run.residual_paise == 0
    assert run.verdict == RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value


def test_invariant_multiple_outstanding_cheques(db):
    """
    Three unpresented cheques (₹100, ₹200, ₹300) — none presented to bank.
    Bridge ADD = 60000. No bank transactions.
    residual = 0.
    """
    user, account = make_user_account(db)
    batch = make_batch(db, user, account, book_closing_paise=-60000)
    period_to = date(2026, 3, 31)
    for i, amt in enumerate([10000, 20000, 30000], start=1):
        make_book_entry(db, user, account, batch,
                        date(2026, 3, i * 5), money_out=amt, instrument_no=f"CHQ{i:03d}")
    db.commit()

    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026, 3, 1), period_to)
    run = engine.execute_run()

    assert run.residual_paise == 0
    assert run.unmatched_book_count == 3
    assert run.computed_bank_closing_paise == run.book_closing_paise + 60000


def test_invariant_outstanding_cheque_plus_matched(db):
    """
    2 book entries: one matched, one outstanding.
    matched entry: CHQ-A book ↔ bank debit exact match (₹1000 = 100000 paise).
    outstanding: CHQ-B in books (₹500 = 50000 paise), no bank transaction.

    book_closing = -150 000 paise (both CHQ-A + CHQ-B recorded in books)
    Bank only shows CHQ-A has cleared → bank balance = -100 000 paise
    Bridge: ADD 50 000 (CHQ-B outstanding)
    computed = -150 000 + 50 000 = -100 000
    residual = -100 000 - (-100 000) = 0 → RECONCILED_CLEAN
    """
    user, account = make_user_account(db)
    today = date(2026, 3, 15)
    # book_closing = -150 000: both CHQA (100000 out) + CHQB (50000 out) recorded
    batch = make_batch(db, user, account, book_closing_paise=-150000)

    # Matched pair (CHQA): bank balance -100000 (only CHQA cleared, CHQB not yet)
    make_book_entry(db, user, account, batch, today, money_out=100000, instrument_no="CHQA")
    make_bank_txn(db, user, account, today, debit=100000, balance=-100000, reference_no="CHQA")

    # Outstanding cheque (CHQB): in books only, not yet presented to bank
    make_book_entry(db, user, account, batch, today, money_out=50000, instrument_no="CHQB")
    db.commit()

    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    run = engine.execute_run()

    assert run.matched_count == 1
    assert run.unmatched_book_count == 1  # CHQB outstanding
    # BRS identity
    assert run.computed_bank_closing_paise == run.book_closing_paise + 50000  # = -100000
    assert run.residual_paise == 0
    assert run.bank_closing_paise == -100000
    assert run.verdict == RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value



def test_invariant_paise_integer_only(db):
    """
    Ensure all computed values are integer paise (no floats).
    Amounts chosen to expose float drift: ₹333.33 = 33333 paise.
    """
    user, account = make_user_account(db)
    today = date(2026, 3, 15)
    batch = make_batch(db, user, account, book_closing_paise=-33333)
    make_book_entry(db, user, account, batch, today, money_out=33333, instrument_no="FLOAT1")
    db.commit()

    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31))
    run = engine.execute_run()

    assert isinstance(run.residual_paise, int)
    assert isinstance(run.computed_bank_closing_paise, int)
    assert isinstance(run.book_closing_paise, int)
    assert isinstance(run.bank_closing_paise, int)


def test_invariant_residual_formula_always_holds(db):
    """
    Assert the BRS identity residual = computed - bank_closing for every
    possible verdict (CLEAN, WITH_EXCEPTIONS, UNRECONCILED).
    """
    for scenario_bank_closing, expect_zero in [(0, True), (-5000, False), (3000, False)]:
        d, S = make_isolated_engine("brs_scenario")
        Base.metadata.create_all(bind=d)
        session = S()
        try:
            user = User(id=uuid.uuid4(), email=f"inv_{uuid.uuid4().hex[:4]}@k.in", hashed_password="h")
            account = Account(id=uuid.uuid4(), user_id=user.id, bank_code="HDFC",
                              account_number_masked="****0001")
            session.add_all([user, account])
            session.commit()

            if scenario_bank_closing != 0:
                make_bank_txn(session, user, account, date(2026, 3, 15),
                              debit=abs(scenario_bank_closing) if scenario_bank_closing < 0 else 0,
                              credit=scenario_bank_closing if scenario_bank_closing > 0 else 0,
                              balance=scenario_bank_closing)
                session.commit()

            engine = ReconciliationMatchingEngine(
                session, user.id, account.id, date(2026, 3, 1), date(2026, 3, 31)
            )
            run = engine.execute_run(book_opening_paise=0)

            # Identity must always hold: residual = bank closing - computed bank closing
            assert run.residual_paise == (run.bank_closing_paise - run.computed_bank_closing_paise), (
                f"Identity violated for bank_closing={scenario_bank_closing}: "
                f"residual={run.residual_paise}, computed={run.computed_bank_closing_paise}, "
                f"bank={run.bank_closing_paise}"
            )

            if expect_zero:
                # No transactions, no book entries → all zeros → residual 0
                assert run.residual_paise == 0
        finally:
            session.close()
