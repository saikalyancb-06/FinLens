"""
Accounting Invariant Test for Reconciliation Engine & Books-to-Bank Bridge.

Verifies:
1. Books closing + every signed adjustment exactly once == computed bank balance
2. EXACT_MATCH = 0 variance
3. DATE_MISMATCH inside period = 0 variance
4. DUPLICATE signed bridge behavior
5. AMOUNT_MISMATCH pending review handling
"""

import pytest
from datetime import date
from decimal import Decimal
import uuid

from app.models.reconciliation import (
    BookEntry, ReconciliationRun, ReconciliationItem, ImportBatch,
    BRSSideEnum, DirectionEnum, RunVerdictEnum
)
from app.models.transaction import Transaction, Direction
from app.database.session import SessionLocal
from app.models.user import User
from app.models.account import Account
from app.services.reconciliation_engine import ReconciliationMatchingEngine, BookOpeningRequired

def test_accounting_bridge_invariants(setup_test_db):
    db_session = SessionLocal()
    try:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()

        user = User(id=user_id, email=f"invariant_{user_id.hex[:6]}@example.com", hashed_password="dummy")
        account = Account(id=account_id, user_id=user_id, account_number_masked="****2345", bank_code="HDFC")
        db_session.add_all([user, account])
        db_session.commit()

        period_from = date(2025, 3, 1)
        period_to = date(2025, 3, 30)

        import_batch = ImportBatch(
            id=uuid.uuid4(), user_id=user_id, account_id=account_id,
            filename="test_ledger.csv", file_sha256="abc123",
            column_mapping_json={"entry_date": "Date", "narration": "Description", "money_in": "Credit", "money_out": "Debit"},
            row_count=6, period_from=date(2025, 3, 1), period_to=date(2025, 3, 30),
            book_opening_paise=0, book_closing_paise=0
        )
        db_session.add(import_batch)
        db_session.commit()
        batch_id = import_batch.id

        # Setup ledger entries
        # 1. Exact match (Debit/Debit Out) - Rs.1000
        be_exact = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=batch_id, row_index=1, source_row_hash="hash1", entry_date=date(2025, 3, 10),
            money_in_paise=0, money_out_paise=100000, instrument_no="REF100", narration="EXACT MATCH"
        )
        # 2. Date mismatch (Credit/Credit In) - Rs.2500 (28-Mar)
        be_date = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=batch_id, row_index=2, source_row_hash="hash2", entry_date=date(2025, 3, 28),
            money_in_paise=250000, money_out_paise=0, instrument_no="REF200", narration="DATE MISMATCH"
        )
        # 3. Duplicate credit (Credit In) - Rs.13511
        be_dup1 = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=batch_id, row_index=3, source_row_hash="hash3", entry_date=date(2025, 3, 27),
            money_in_paise=1351100, money_out_paise=0, instrument_no="REF300", narration="CANONICAL CREDIT"
        )
        be_dup2 = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=batch_id, row_index=4, source_row_hash="hash4", entry_date=date(2025, 3, 27),
            money_in_paise=1351100, money_out_paise=0, instrument_no="REF300-DUP", narration="DUPLICATE CREDIT"
        )
        # 4. Amount mismatch (Credit In) - Rs.12400 ledger vs Rs.12460 bank
        be_amt = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=batch_id, row_index=5, source_row_hash="hash5", entry_date=date(2025, 3, 30),
            money_in_paise=1240000, money_out_paise=0, instrument_no="REF400", narration="AMOUNT MISMATCH"
        )
        # 5. Ledger only (Debit Out) - Rs.12000
        be_ledger_only = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=batch_id, row_index=6, source_row_hash="hash6", entry_date=date(2025, 3, 1),
            money_in_paise=0, money_out_paise=1200000, instrument_no="REF500", narration="OFFICE RENT"
        )

        db_session.add_all([be_exact, be_date, be_dup1, be_dup2, be_amt, be_ledger_only])
        db_session.commit()

        # Setup bank transactions
        bt_exact = Transaction(
            user_id=user_id, account_id=account_id, txn_date=date(2025, 3, 10),
            debit_paise=100000, credit_paise=0, direction=Direction.DEBIT,
            reference_no="REF100", narration_raw="EXACT MATCH BANK"
        )
        bt_date = Transaction(
            user_id=user_id, account_id=account_id, txn_date=date(2025, 3, 29),
            debit_paise=0, credit_paise=250000, direction=Direction.CREDIT,
            reference_no="REF200", narration_raw="DATE MISMATCH BANK"
        )
        bt_dup = Transaction(
            user_id=user_id, account_id=account_id, txn_date=date(2025, 3, 27),
            debit_paise=0, credit_paise=1351100, direction=Direction.CREDIT,
            reference_no="REF300", narration_raw="CANONICAL CREDIT BANK"
        )
        bt_amt = Transaction(
            user_id=user_id, account_id=account_id, txn_date=date(2025, 3, 30),
            debit_paise=0, credit_paise=1246000, direction=Direction.CREDIT,
            reference_no="REF400", narration_raw="AMOUNT MISMATCH BANK"
        )
        bt_bank_only = Transaction(
            user_id=user_id, account_id=account_id, txn_date=date(2025, 3, 1),
            debit_paise=0, credit_paise=2559700, direction=Direction.CREDIT,
            reference_no="REF600", narration_raw="BANK ONLY CREDIT"
        )

        db_session.add_all([bt_exact, bt_date, bt_dup, bt_amt, bt_bank_only])
        db_session.commit()

        engine = ReconciliationMatchingEngine(db_session, user_id, account_id, period_from, period_to)
        run = engine.execute_run()

        # INVARIANT 1: Bridge Formula Check
        book_closing = run.book_closing_paise
        computed_bank_closing = run.computed_bank_closing_paise

        items = db_session.query(ReconciliationItem).filter(ReconciliationItem.run_id == run.id).all()
        
        bridge_sum = sum(
            i.amount_paise if i.direction in (DirectionEnum.ADD.value, DirectionEnum.ADD) else -i.amount_paise
            for i in items
        )

        assert computed_bank_closing == book_closing + bridge_sum, "Invariant Failed: Bridge sum does not reconstruct computed bank closing balance"

        # INVARIANT 2 & 3: Reconciled items (exact, date mismatch inside period) carry 0 variance to bridge
        exact_items = [i for i in items if i.book_entry_id == be_exact.id]
        date_items = [i for i in items if i.book_entry_id == be_date.id]
        assert len(exact_items) == 0, "Exact match should be fully reconciled (0 bridge item)"
        assert len(date_items) == 0, "Date mismatch within period should be fully reconciled (0 bridge item)"

        # INVARIANT 4: Duplicate Credit Row behavior
        dup2_item = next((i for i in items if i.book_entry_id == be_dup2.id), None)
        assert dup2_item is not None, "Duplicate ledger credit must be represented in BRS adjustments"
        assert dup2_item.direction == DirectionEnum.SUBTRACT.value, "Duplicate ledger credit must SUBTRACT from book balance"
        assert dup2_item.amount_paise == 1351100, "Duplicate ledger credit amount must equal duplicate transaction amount"

        # INVARIANT 5: Amount mismatch pending review is not finalized
        amt_item = next((i for i in items if i.book_entry_id == be_amt.id or i.bank_txn_id == bt_amt.id), None)
        assert run.pending_review_count >= 1, "Amount mismatch must remain in pending_review"
    finally:
        db_session.close()


def test_out_of_period_ledger_raises_error(setup_test_db):
    """January period + March-only ledger => reconciliation must be rejected with ValueError."""
    db_session = SessionLocal()
    try:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()

        user = User(id=user_id, email=f"jan_period_{user_id.hex[:6]}@example.com", hashed_password="dummy")
        account = Account(id=account_id, user_id=user_id, account_number_masked="****9999", bank_code="HDFC")
        db_session.add_all([user, account])
        db_session.commit()

        # March ledger entry
        import_batch = ImportBatch(
            id=uuid.uuid4(), user_id=user_id, account_id=account_id,
            filename="march_ledger.csv", file_sha256="sha_march",
            column_mapping_json={"entry_date": "Date", "narration": "Description", "money_in": "Credit", "money_out": "Debit"},
            row_count=1, period_from=date(2025, 3, 1), period_to=date(2025, 3, 30),
            book_opening_paise=0, book_closing_paise=19850584
        )
        be_march = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=import_batch.id, row_index=1,
            source_row_hash="hash_m1", entry_date=date(2025, 3, 15),
            money_in_paise=19850584, money_out_paise=0, instrument_no="REF_M", narration="MARCH LEDGER ROW"
        )
        db_session.add_all([import_batch, be_march])
        db_session.commit()

        # January reconciliation period (no ledger rows in Jan)
        jan_engine = ReconciliationMatchingEngine(
            db_session, user_id, account_id,
            period_from=date(2025, 1, 1),
            period_to=date(2025, 1, 31)
        )

        with pytest.raises(ValueError) as excinfo:
            jan_engine.execute_run(import_batch_id=import_batch.id)

        assert "No ledger transactions found within the selected reconciliation period" in str(excinfo.value)
    finally:
        db_session.close()


def test_in_period_ledger_calculates_correctly(setup_test_db):
    """March period + March ledger => normal reconciliation should run with in-period calculated balance."""
    db_session = SessionLocal()
    try:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()

        user = User(id=user_id, email=f"march_period_{user_id.hex[:6]}@example.com", hashed_password="dummy")
        account = Account(id=account_id, user_id=user_id, account_number_masked="****8888", bank_code="HDFC")
        db_session.add_all([user, account])
        db_session.commit()

        # March ledger entry
        import_batch = ImportBatch(
            id=uuid.uuid4(), user_id=user_id, account_id=account_id,
            filename="march_ledger.csv", file_sha256="sha_march2",
            column_mapping_json={"entry_date": "Date", "narration": "Description", "money_in": "Credit", "money_out": "Debit"},
            row_count=1, period_from=date(2025, 3, 1), period_to=date(2025, 3, 30),
            book_opening_paise=0, book_closing_paise=99999999 # Full-file balance shouldn't be used
        )
        be_march = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=import_batch.id, row_index=1,
            source_row_hash="hash_m2", entry_date=date(2025, 3, 15),
            money_in_paise=500000, money_out_paise=0, instrument_no="REF_M2", narration="MARCH LEDGER ROW 2"
        )
        db_session.add_all([import_batch, be_march])
        db_session.commit()

        march_engine = ReconciliationMatchingEngine(
            db_session, user_id, account_id,
            period_from=date(2025, 3, 1),
            period_to=date(2025, 3, 30)
        )

        run = march_engine.execute_run()
        # Closing balance should be 0 + 5000 = 5000 (500,000 paise), NOT 99,999,999 paise
        assert run.book_closing_paise == 500000
    finally:
        db_session.close()


def test_period_to_bank_closing_boundary(setup_test_db):
    """
    Cases A & B test:
    A. 2025-03-01 -> 2025-03-30 must NOT include March 31's +Rs.8,065 movement, bank closing must correspond to Mar 30.
    B. 2025-03-01 -> 2025-03-31 MUST include March 31's +Rs.8,065 movement, bank closing must correspond to Mar 31.
    """
    db_session = SessionLocal()
    try:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()

        user = User(id=user_id, email=f"boundary_{user_id.hex[:6]}@example.com", hashed_password="dummy")
        account = Account(id=account_id, user_id=user_id, account_number_masked="****7777", bank_code="HDFC")
        db_session.add_all([user, account])
        db_session.commit()

        # March 30 transaction
        t_mar30 = Transaction(
            user_id=user_id, account_id=account_id, txn_date=date(2025, 3, 30),
            debit_paise=0, credit_paise=100000, balance_paise=23986998, # Rs.239,869.98 on Mar 30
            reference_no="MAR30_TXN", narration_raw="MARCH 30 BANK TXN", direction=Direction.CREDIT
        )
        # March 31 transaction (+Rs 8,065 net)
        t_mar31 = Transaction(
            user_id=user_id, account_id=account_id, txn_date=date(2025, 3, 31),
            debit_paise=0, credit_paise=806500, balance_paise=24793498, # Rs.247,934.98 on Mar 31
            reference_no="MAR31_TXN", narration_raw="MARCH 31 BANK TXN", direction=Direction.CREDIT
        )
        db_session.add_all([t_mar30, t_mar31])
        db_session.commit()

        # Case A: 2025-03-01 -> 2025-03-30
        engine_a = ReconciliationMatchingEngine(db_session, user_id, account_id, date(2025, 3, 1), date(2025, 3, 30))
        run_a = engine_a.execute_run(book_opening_paise=0)
        assert run_a.bank_closing_paise == 23986998, f"Case A Bank Closing should be 23986998 (Mar 30 balance), got {run_a.bank_closing_paise}"

        # Case B: 2025-03-01 -> 2025-03-31
        engine_b = ReconciliationMatchingEngine(db_session, user_id, account_id, date(2025, 3, 1), date(2025, 3, 31))
        run_b = engine_b.execute_run(book_opening_paise=0)
        assert run_b.bank_closing_paise == 24793498, f"Case B Bank Closing should be 24793498 (Mar 31 balance), got {run_b.bank_closing_paise}"
        assert run_b.bank_closing_paise - run_a.bank_closing_paise == 806500, "Difference between Mar 31 and Mar 30 must equal +Rs. 8,065"
    finally:
        db_session.close()


def test_no_explicit_book_opening_semantics(setup_test_db):
    """
    Case A: no book opening anywhere (not typed, no previous run, no opening row
    in the file). The engine must NOT borrow the bank balance (Rs 995.64 here):
    it stops and asks. Once the user types Rs 995.64, the arithmetic is
    opening 99564 + movement 5816100 = closing 5915664.
    """
    db_session = SessionLocal()
    try:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()

        user = User(id=user_id, email=f"sem_a_{user_id.hex[:6]}@example.com", hashed_password="dummy")
        account = Account(id=account_id, user_id=user_id, account_number_masked="****1111", bank_code="HDFC")
        db_session.add_all([user, account])
        db_session.commit()

        # Previous transaction establishing bank opening balance = Rs 995.64
        t_prev = Transaction(
            user_id=user_id, account_id=account_id, txn_date=date(2023, 2, 28),
            debit_paise=0, credit_paise=99564, balance_paise=99564,
            reference_no="PREV_TXN", narration_raw="FEBRUARY CLOSING", direction=Direction.CREDIT
        )
        db_session.add(t_prev)
        db_session.commit()

        # March ledger entry (no explicit book opening)
        import_batch = ImportBatch(
            id=uuid.uuid4(), user_id=user_id, account_id=account_id,
            filename="march2023_ledger.csv", file_sha256="sha_mar23",
            column_mapping_json={"entry_date": "Date", "narration": "Description", "money_in": "Credit", "money_out": "Debit"},
            row_count=1, period_from=date(2023, 3, 1), period_to=date(2023, 3, 28),
            book_opening_paise=None
        )
        be = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=import_batch.id, row_index=1,
            source_row_hash="hash_mar23", entry_date=date(2023, 3, 15),
            money_in_paise=5816100, money_out_paise=0, instrument_no="REF_MAR23", narration="MARCH MOVEMENT"
        )
        db_session.add_all([import_batch, be])
        db_session.commit()

        engine = ReconciliationMatchingEngine(db_session, user_id, account_id, date(2023, 3, 1), date(2023, 3, 28))
        with pytest.raises(BookOpeningRequired):
            engine.execute_run(import_batch_id=import_batch.id)
        db_session.rollback()

        run = engine.execute_run(import_batch_id=import_batch.id, book_opening_paise=99564)
        assert run.book_opening_source == "manual"
        assert run.opening_balance_paise == 99564
        assert run.net_movement_paise == 5816100
        assert run.book_closing_paise == 5915664
    finally:
        db_session.close()


def test_explicit_book_opening_semantics(setup_test_db):
    """
    Case B: Explicit book opening provided.
    book_opening_paise = 100000 (Rs 1,000.00)
    net_movement_paise = 500000 (Rs 5,000.00)
    book_closing_paise = 600000 (Rs 6,000.00)
    """
    db_session = SessionLocal()
    try:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()

        user = User(id=user_id, email=f"sem_b_{user_id.hex[:6]}@example.com", hashed_password="dummy")
        account = Account(id=account_id, user_id=user_id, account_number_masked="****2222", bank_code="HDFC")
        db_session.add_all([user, account])
        db_session.commit()

        import_batch = ImportBatch(
            id=uuid.uuid4(), user_id=user_id, account_id=account_id,
            filename="ledger_with_opening.csv", file_sha256="sha_explicit",
            column_mapping_json={"entry_date": "Date", "narration": "Description", "money_in": "Credit", "money_out": "Debit"},
            row_count=1, period_from=date(2025, 3, 1), period_to=date(2025, 3, 30),
            book_opening_paise=100000
        )
        be = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=import_batch.id, row_index=1,
            source_row_hash="hash_exp", entry_date=date(2025, 3, 10),
            money_in_paise=500000, money_out_paise=0, instrument_no="REF_EXP", narration="EXPLICIT OPENING MOVEMENT"
        )
        db_session.add_all([import_batch, be])
        db_session.commit()

        engine = ReconciliationMatchingEngine(db_session, user_id, account_id, date(2025, 3, 1), date(2025, 3, 30))
        run = engine.execute_run(import_batch_id=import_batch.id)

        assert run.opening_balance_paise == 100000
        assert run.net_movement_paise == 500000
        assert run.book_closing_paise == 600000
    finally:
        db_session.close()


def test_status_not_all_clear_when_bank_only_items_exist(setup_test_db):
    """
    TEST B: Residual is 0 because bank-only credit (+Rs 100) bridges the gap,
    but bank-only item remains unresolved.
    Verdict MUST NOT be RECONCILED_CLEAN; it MUST be RECONCILED_WITH_EXCEPTIONS.
    """
    db_session = SessionLocal()
    try:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()

        user = User(id=user_id, email=f"status_b_{user_id.hex[:6]}@example.com", hashed_password="dummy")
        account = Account(id=account_id, user_id=user_id, account_number_masked="****3333", bank_code="HDFC")
        db_session.add_all([user, account])
        db_session.commit()

        # Bank transaction (bank-only credit Rs 100) closing at Rs 100
        t_bank = Transaction(
            user_id=user_id, account_id=account_id, txn_date=date(2025, 3, 15),
            debit_paise=0, credit_paise=10000, balance_paise=10000,
            reference_no="BANK_CREDIT_ONLY", narration_raw="INTEREST CREDIT", direction=Direction.CREDIT
        )
        db_session.add(t_bank)
        db_session.commit()

        # Empty ledger (0 book entries, 0 opening)
        import_batch = ImportBatch(
            id=uuid.uuid4(), user_id=user_id, account_id=account_id,
            filename="empty_ledger.csv", file_sha256="sha_empty",
            column_mapping_json={"entry_date": "Date", "narration": "Description", "money_in": "Credit", "money_out": "Debit"},
            row_count=0, period_from=date(2025, 3, 1), period_to=date(2025, 3, 31),
            book_opening_paise=0
        )
        db_session.add(import_batch)
        db_session.commit()

        engine = ReconciliationMatchingEngine(db_session, user_id, account_id, date(2025, 3, 1), date(2025, 3, 31))
        run = engine.execute_run(import_batch_id=import_batch.id)

        assert run.residual_paise == 0
        assert run.unmatched_bank_count == 1
        assert run.verdict == RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value
        assert run.verdict != RunVerdictEnum.RECONCILED_CLEAN.value
    finally:
        db_session.close()


def test_status_all_clear_only_when_zero_residual_and_zero_outstanding(setup_test_db):
    """
    TEST C: Exact match, 0 timing differences, 0 bank-only items, 0 residual.
    Verdict MUST be RECONCILED_CLEAN.
    """
    db_session = SessionLocal()
    try:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()

        user = User(id=user_id, email=f"status_c_{user_id.hex[:6]}@example.com", hashed_password="dummy")
        account = Account(id=account_id, user_id=user_id, account_number_masked="****4444", bank_code="HDFC")
        db_session.add_all([user, account])
        db_session.commit()

        t_bank = Transaction(
            user_id=user_id, account_id=account_id, txn_date=date(2025, 3, 15),
            debit_paise=0, credit_paise=50000, balance_paise=50000,
            reference_no="MATCH123", narration_raw="CLEAN MATCH", direction=Direction.CREDIT
        )
        db_session.add(t_bank)
        db_session.commit()

        import_batch = ImportBatch(
            id=uuid.uuid4(), user_id=user_id, account_id=account_id,
            filename="clean_ledger.csv", file_sha256="sha_clean",
            column_mapping_json={"entry_date": "Date", "narration": "Description", "money_in": "Credit", "money_out": "Debit"},
            row_count=1, period_from=date(2025, 3, 1), period_to=date(2025, 3, 31),
            book_opening_paise=0
        )
        be = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=import_batch.id, row_index=1,
            source_row_hash="hash_clean", entry_date=date(2025, 3, 15),
            money_in_paise=50000, money_out_paise=0, instrument_no="MATCH123", narration="CLEAN MATCH"
        )
        db_session.add_all([import_batch, be])
        db_session.commit()

        engine = ReconciliationMatchingEngine(db_session, user_id, account_id, date(2025, 3, 1), date(2025, 3, 31))
        run = engine.execute_run(import_batch_id=import_batch.id)

        assert run.residual_paise == 0
        assert run.unmatched_bank_count == 0
        assert run.unmatched_book_count == 0
        assert run.verdict == RunVerdictEnum.RECONCILED_CLEAN.value
    finally:
        db_session.close()


def test_out_of_period_ledger_entries_excluded_from_net_movement(setup_test_db):
    """
    Defect 2 Regression Test:
    Reconciliation period: 2026-05-01 -> 2026-05-31.
    Ledger contains:
      - 2026-04-29 (out-of-period lookback tolerance entry): +Rs. 1,111 (111100 paise)
      - 2026-05-15 (in-period entry): +Rs. 40,850 (4085000 paise)
      - 2026-06-02 (out-of-period future entry): +Rs. 2,222 (222200 paise)

    In-period net_movement_paise MUST be exactly 4085000 (Rs. 40,850).
    The April 29 and June 2 entries must have 0 effect on the May accounting totals.
    """
    db_session = SessionLocal()
    try:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()

        user = User(id=user_id, email=f"may_period_{user_id.hex[:6]}@example.com", hashed_password="dummy")
        account = Account(id=account_id, user_id=user_id, account_number_masked="****5555", bank_code="HDFC")
        db_session.add_all([user, account])
        db_session.commit()

        # Bank opening balance before May = Rs. 10,000 (1000000 paise)
        t_prev = Transaction(
            user_id=user_id, account_id=account_id, txn_date=date(2026, 4, 30),
            debit_paise=0, credit_paise=1000000, balance_paise=1000000,
            reference_no="PREV_APRIL", narration_raw="APRIL CLOSING", direction=Direction.CREDIT
        )
        db_session.add(t_prev)
        db_session.commit()

        import_batch = ImportBatch(
            id=uuid.uuid4(), user_id=user_id, account_id=account_id,
            filename="may_ledger.csv", file_sha256="sha_may",
            column_mapping_json={"entry_date": "Date", "narration": "Description", "money_in": "Credit", "money_out": "Debit"},
            row_count=3, period_from=date(2026, 5, 1), period_to=date(2026, 5, 31),
            book_opening_paise=0
        )
        be_apr = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=import_batch.id, row_index=1,
            source_row_hash="hash_apr", entry_date=date(2026, 4, 29),
            money_in_paise=111100, money_out_paise=0, instrument_no="APR29", narration="OUT OF PERIOD APRIL"
        )
        be_may = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=import_batch.id, row_index=2,
            source_row_hash="hash_may", entry_date=date(2026, 5, 15),
            money_in_paise=4085000, money_out_paise=0, instrument_no="MAY15", narration="IN PERIOD MAY"
        )
        be_jun = BookEntry(
            user_id=user_id, account_id=account_id, import_batch_id=import_batch.id, row_index=3,
            source_row_hash="hash_jun", entry_date=date(2026, 6, 2),
            money_in_paise=222200, money_out_paise=0, instrument_no="JUN02", narration="OUT OF PERIOD JUNE"
        )
        db_session.add_all([import_batch, be_apr, be_may, be_jun])
        db_session.commit()

        engine = ReconciliationMatchingEngine(db_session, user_id, account_id, date(2026, 5, 1), date(2026, 5, 31))
        run = engine.execute_run(import_batch_id=import_batch.id, book_opening_paise=1000000)

        # Book opening typed as Rs 10,000
        assert run.opening_balance_paise == 1000000
        # In-period net movement = 4085000 (Rs 40,850)
        assert run.net_movement_paise == 4085000
        # Book closing position = 1000000 + 4085000 = 5085000 (Rs 50,850)
        assert run.book_closing_paise == 5085000
    finally:
        db_session.close()


def test_pdf_parser_explicit_columns_and_running_balance_fallback():
    """
    Format-Specific Parsing Test:
    1. PDFs with explicit direction columns (Debit / Credit) must extract exact debit/credit amounts.
    2. PDFs without explicit direction markers use running-balance inference fallback based on balance deltas.
    """
    from app.parsers.pdf_parser import PDFParser

    parser = PDFParser()

    # Format 1: Table with explicit Debit and Credit columns
    table_explicit = [
        ["Date", "Description", "Debit", "Credit", "Balance"],
        ["2026-05-01", "Opening", "", "", "10000.00"],
        ["2026-05-10", "Payment", "2500.00", "", "7500.00"],
        ["2026-05-15", "Deposit", "", "5000.00", "12500.00"],
    ]
    parsed_explicit = parser._process_table_matrix(table_explicit)
    assert len(parsed_explicit) == 3
    # Payment row -> Debit 2500, Credit 0
    assert parsed_explicit[1]["debit"] == 2500.0
    assert parsed_explicit[1]["credit"] == 0.0
    # Deposit row -> Debit 0, Credit 5000
    assert parsed_explicit[2]["debit"] == 0.0
    assert parsed_explicit[2]["credit"] == 5000.0

    # Format 2: Raw text lines without explicit DR/CR markers using running-balance inference
    text_lines = (
        "2026-05-01 OPENING BALANCE 10,000.00\n"
        "2026-05-10 SUPPLIER INVOICE 2,500.00 7,500.00\n"
        "2026-05-15 CLIENT RECEIPT 5,000.00 12,500.00\n"
    )
    parsed_inferred = parser._process_raw_text(text_lines)
    assert len(parsed_inferred) == 3
    # Row 0: Opening balance row (10000.00 balance)
    assert parsed_inferred[0]["balance"] == 10000.0
    # Row 1: Supplier Invoice (balance decreased from 10000 to 7500) -> inferred Debit 2500
    assert parsed_inferred[1]["debit"] == 2500.0
    assert parsed_inferred[1]["credit"] == 0.0
    # Row 2: Client Receipt (balance increased from 7500 to 12500) -> inferred Credit 5000
    assert parsed_inferred[2]["debit"] == 0.0
    assert parsed_inferred[2]["credit"] == 5000.0


def test_may_2026_opening_balance_and_out_of_period_exclusion(setup_test_db):
    """
    Regression test for May 2026 E2E Reconciliation:
    - Period: 2026-05-01 to 2026-05-31
    - Opening balance derived from May 1 transaction running balance = 10,000.00 (1000000 paise)
    - In-period net movement = 40,850.00 (4085000 paise)
    - April 29 (1,111.00) and June 2 (2,222.00) entries are outside May and MUST NOT alter net movement, book closing, or BRS items
    - Bank-only credit = +9,900.00 (990000 paise)
    - Computed bank = Actual bank = 60,750.00 (6075000 paise), Residual = 0
    """
    import uuid
    from datetime import date
    from app.database.session import SessionLocal
    from app.models.user import User
    from app.models.account import Account
    from app.models.transaction import Transaction, Direction
    from app.models.reconciliation import BookEntry, ImportBatch
    from app.services.reconciliation_engine import ReconciliationMatchingEngine

    db_session = SessionLocal()
    try:
        user_id = uuid.uuid4()
        user = User(id=user_id, email=f"may2026_{user_id.hex[:6]}@example.com", hashed_password="dummy")
        account = Account(id=uuid.uuid4(), user_id=user_id, account_number_masked="****2026", bank_code="HDFC")
        db_session.add_all([user, account])
        db_session.commit()

        # May Bank Transactions (May 1 opening balance 10,000.00 before 25,000 credit -> balance 35,000.00)
        tx1 = Transaction(
            id=uuid.uuid4(), user_id=user_id, account_id=account.id,
            txn_date=date(2026, 5, 1), direction=Direction.CREDIT,
            credit_paise=2500000, balance_paise=3500000,
            narration_raw="NEFT/TEST/001"
        )
        tx15 = Transaction(
            id=uuid.uuid4(), user_id=user_id, account_id=account.id,
            txn_date=date(2026, 5, 15), direction=Direction.CREDIT,
            credit_paise=1585000, balance_paise=5085000,
            narration_raw="IN PERIOD MOVEMENT"
        )
        # Bank-only credit on May 25 (+9,900.00 -> balance 60,750.00)
        tx_bank_only = Transaction(
            id=uuid.uuid4(), user_id=user_id, account_id=account.id,
            txn_date=date(2026, 5, 25), direction=Direction.CREDIT,
            credit_paise=990000, balance_paise=6075000,
            narration_raw="NEFT/TEST/BANKONLY"
        )
        db_session.add_all([tx1, tx15, tx_bank_only])
        db_session.commit()

        # ImportBatch (no explicit book opening)
        batch = ImportBatch(
            id=uuid.uuid4(), user_id=user_id, account_id=account.id,
            filename="ledger_may.csv", file_sha256="dummy_sha256", column_mapping_json={},
            period_from=date(2026, 5, 1), period_to=date(2026, 5, 31),
            book_opening_paise=0, book_closing_paise=0
        )
        db_session.add(batch)
        db_session.commit()

        # Ledger Entries:
        # 1. Out-of-period lookback (April 29)
        b_apr29 = BookEntry(
            id=uuid.uuid4(), user_id=user_id, account_id=account.id, import_batch_id=batch.id,
            entry_date=date(2026, 4, 29), money_in_paise=111100, money_out_paise=0,
            narration="April 29 out-of-period entry", row_index=1, source_row_hash="hash1"
        )
        # 2. In-period ledger entry (May 1) -> matched with tx1 (25,000.00 credit)
        b_may1 = BookEntry(
            id=uuid.uuid4(), user_id=user_id, account_id=account.id, import_batch_id=batch.id,
            entry_date=date(2026, 5, 1), money_in_paise=2500000, money_out_paise=0,
            narration="NEFT/TEST/001", row_index=2, source_row_hash="hash2"
        )
        # 3. In-period ledger entry (May 15) -> matched with tx15 (15,850.00 credit)
        b_may15 = BookEntry(
            id=uuid.uuid4(), user_id=user_id, account_id=account.id, import_batch_id=batch.id,
            entry_date=date(2026, 5, 15), money_in_paise=1585000, money_out_paise=0,
            narration="IN PERIOD MOVEMENT", row_index=3, source_row_hash="hash3"
        )
        # 4. Out-of-period look-ahead (June 2)
        b_june2 = BookEntry(
            id=uuid.uuid4(), user_id=user_id, account_id=account.id, import_batch_id=batch.id,
            entry_date=date(2026, 6, 2), money_in_paise=222200, money_out_paise=0,
            narration="June 2 out-of-period entry", row_index=4, source_row_hash="hash4"
        )
        db_session.add_all([b_apr29, b_may1, b_may15, b_june2])
        db_session.commit()

        engine = ReconciliationMatchingEngine(
            db=db_session, user_id=user_id, account_id=account.id,
            period_from=date(2026, 5, 1), period_to=date(2026, 5, 31)
        )
        run = engine.execute_run(import_batch_id=batch.id, force=True, book_opening_paise=1000000)

        # Assertions strictly enforcing prompt invariants
        assert run.opening_balance_paise == 1000000, f"Expected opening 1000000, got {run.opening_balance_paise}"
        assert run.net_movement_paise == 4085000, f"Expected net movement 4085000, got {run.net_movement_paise}"
        assert run.book_closing_paise == 5085000, f"Expected book closing 5085000, got {run.book_closing_paise}"
        assert run.bank_closing_paise == 6075000, f"Expected bank closing 6075000, got {run.bank_closing_paise}"
        assert run.computed_bank_closing_paise == 6075000, f"Expected computed 6075000, got {run.computed_bank_closing_paise}"
        assert run.residual_paise == 0, f"Expected residual 0, got {run.residual_paise}"

        # Assert out-of-period entries (b_apr29 and b_june2) are NOT emitted into items
        brs_book_entry_ids = [item.book_entry_id for item in run.items if item.book_entry_id is not None]
        assert b_apr29.id not in brs_book_entry_ids, "April 29 entry must NOT be emitted as a May BRS item"
        assert b_june2.id not in brs_book_entry_ids, "June 2 entry must NOT be emitted as a May BRS item"
    finally:
        db_session.close()






