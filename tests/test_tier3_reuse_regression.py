import pytest
import uuid
import datetime
from sqlalchemy.orm import Session
from app.database.session import get_db
from app.models.user import User
from app.models.account import Account

from app.models.reconciliation import BookEntry, ImportBatch, ReconciliationMatchLine
from app.models.transaction import Transaction, Direction, SourceType
from app.services.reconciliation_engine import ReconciliationMatchingEngine

def test_tier3_book_entry_reuse_prevention(client):
    """Verify that a book entry participating in a Tier 3 group match cannot be reused in another match."""
    db_gen = client.app.dependency_overrides.get(get_db, get_db)()
    db: Session = next(db_gen)

    user_id = uuid.uuid4()
    account_id = uuid.uuid4()

    db.add(User(id=user_id, email=f"reuse_{uuid.uuid4().hex[:6]}@test.com", full_name="User", hashed_password="fake"))
    # The Account row has to exist: PostgreSQL enforces the account_id foreign
    # key on import_batches, book_entries and transactions, where SQLite did not.
    db.add(Account(id=account_id, user_id=user_id, bank_code="HDFC",
                   account_number_masked="****3000"))
    db.flush()
    batch = ImportBatch(
        id=uuid.uuid4(), user_id=user_id, account_id=account_id,
        filename="f.csv", file_sha256=f"hash_{uuid.uuid4().hex[:6]}",
        column_mapping_json="{}", book_opening_paise=0, book_closing_paise=3000,
        imported_at=datetime.datetime.utcnow()
    )
    db.add(batch)

    # Book entries: B1 = 1000, B2 = 2000 (Sum = 3000)
    b1 = BookEntry(
        id=uuid.uuid4(), user_id=user_id, account_id=account_id, import_batch_id=batch.id,
        entry_date=datetime.date(2026, 8, 1), instrument_no="", voucher_no="V1",
        money_in_paise=1000, money_out_paise=0, reconciliation_status="UNMATCHED",
        row_index=0, source_row_hash="h1"
    )
    b2 = BookEntry(
        id=uuid.uuid4(), user_id=user_id, account_id=account_id, import_batch_id=batch.id,
        entry_date=datetime.date(2026, 8, 1), instrument_no="", voucher_no="V2",
        money_in_paise=2000, money_out_paise=0, reconciliation_status="UNMATCHED",
        row_index=1, source_row_hash="h2"
    )
    db.add_all([b1, b2])

    # Two separate bank transactions, both of amount 3000
    t1 = Transaction(
        id=uuid.uuid4(), user_id=user_id, account_id=account_id,
        txn_date=datetime.date(2026, 8, 1), narration_raw="DEP 1",
        direction=Direction.CREDIT, credit_paise=3000, debit_paise=0, balance_paise=3000,
        source_type=SourceType.STATEMENT
    )
    t2 = Transaction(
        id=uuid.uuid4(), user_id=user_id, account_id=account_id,
        txn_date=datetime.date(2026, 8, 1), narration_raw="DEP 2",
        direction=Direction.CREDIT, credit_paise=3000, debit_paise=0, balance_paise=6000,
        source_type=SourceType.STATEMENT
    )
    db.add_all([t1, t2])
    db.commit()

    recon = ReconciliationMatchingEngine(db, user_id, account_id, datetime.date(2026, 8, 1), datetime.date(2026, 8, 31))
    run = recon.execute_run(import_batch_id=batch.id)

    # Check match lines — B1 must only belong to 1 match line
    lines = db.query(ReconciliationMatchLine).filter(ReconciliationMatchLine.book_entry_id == b1.id).all()
    assert len(lines) == 1, "Book entry B1 must not be reused across multiple Tier 3 matches"
