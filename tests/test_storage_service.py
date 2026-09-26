import pytest
import uuid

from app.database.session import Base
from tests.pgtestdb import make_isolated_engine
from app.models import User, UploadedFile, ProcessedTransaction
from app.services.transaction_storage import TransactionStorageService
from app.parsers.pipeline import TransactionParsingPipeline

def _seed_user(session, email_prefix="storage"):
    """PostgreSQL enforces the user_id foreign keys these tests used to invent."""
    user = User(id=uuid.uuid4(), email=f"{email_prefix}_{uuid.uuid4().hex[:6]}@test.com",
                hashed_password="x")
    session.add(user)
    session.commit()
    return user.id


def _seed_uploaded_file(session, user_id, filename="storage_test.csv"):
    """processed_transactions.file_id references uploaded_files.id."""
    uploaded = UploadedFile(id=uuid.uuid4(), user_id=user_id, filename=filename,
                            file_path=f"/tmp/{filename}", mime_type="text/csv",
                            status="PENDING")
    session.add(uploaded)
    session.commit()
    return uploaded.id


@pytest.fixture
def db_session():
    # A dedicated PostgreSQL database, reset for each use — replaces the old
    # sqlite:///:memory: engine so this module runs on the production backend.
    engine, Session = make_isolated_engine("storage")
    Base.metadata.create_all(bind=engine)
    session = Session()
    yield session
    session.close()

def test_store_processed_transactions_and_deduplication(db_session, tmp_path):
    storage_service = TransactionStorageService()

    csv_file = tmp_path / "storage_test.csv"
    csv_content = (
        "Date,Particulars,Debit,Credit,Balance\n"
        "01/08/2026,SWIGGY BANGALORE ORDER #99,350.00,0.00,1000.00\n"
        "02/08/2026,BHARATPE PAYOUTS 123,0.00,2000.00,3000.00\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    pipeline = TransactionParsingPipeline()
    processed_txns = pipeline.process_file(str(csv_file))

    user_id = _seed_user(db_session)
    file_id = _seed_uploaded_file(db_session, user_id)

    # 1. Initial Storage Run
    records = storage_service.store_processed_transactions(
        db=db_session,
        processed_transactions=processed_txns,
        file_id=str(file_id),
        user_id=str(user_id),
        model_version="v1.0.0"
    )

    assert len(records) == 2
    assert db_session.query(ProcessedTransaction).filter_by(file_id=file_id).count() == 2

    # Check row 1 stored fields
    r1 = records[0]
    assert r1.description == "SWIGGY BANGALORE ORDER #99"
    assert float(r1.debit) == 350.0
    assert float(r1.amount) == 350.0
    assert float(r1.balance) == 1000.0
    assert r1.final_category == "Food & Dining"
    assert r1.confidence == 0.97
    assert r1.model_version == "v1.0.0"
    assert r1.original_raw_text != ""
    assert r1.processing_timestamp is not None

    # 2. Re-upload / Reprocess Same Statement (Deduplication Check)
    records_second_run = storage_service.store_processed_transactions(
        db=db_session,
        processed_transactions=processed_txns,
        file_id=str(file_id),
        user_id=str(user_id),
        model_version="v1.0.0"
    )

    # Must still total 2 (not 4) records for this file_id
    total_count = db_session.query(ProcessedTransaction).filter_by(file_id=file_id).count()
    assert total_count == 2


def test_bank_sign_convention_debit_credit_mapping(db_session):
    from app.models.transaction import Transaction, Direction

    from app.models.account import Account
    from app.models.statement import Statement

    storage_service = TransactionStorageService()
    # Real parent rows: transactions references users, accounts and statements,
    # and PostgreSQL enforces all three.
    test_user_id = _seed_user(db_session, "signconv")
    test_account_id = uuid.uuid4()
    test_stmt_id = uuid.uuid4()
    db_session.add(Account(id=test_account_id, user_id=test_user_id, bank_code="HDFC",
                           account_number_masked="****1234"))
    db_session.add(Statement(id=test_stmt_id, user_id=test_user_id, account_id=test_account_id,
                             original_filename="signconv.csv", status="parsed"))
    db_session.commit()

    txns_sample = [
        {
            "date": "2025-01-10",
            "description": "LOAN EMI PAYMENT",
            "debit": "25000.00",
            "credit": "",
            "balance": "450272.14",
            "row_index": 0
        },
        {
            "date": "2025-01-31",
            "description": "INTEREST CREDIT",
            "debit": "0.00",
            "credit": "412.00",
            "balance": "100634.14",
            "row_index": 1
        }
    ]

    storage_service.store_transactions(
        db=db_session,
        processed_txns=txns_sample,
        user_id=test_user_id,
        account_id=test_account_id,
        statement_id=test_stmt_id
    )

    rows = db_session.query(Transaction).filter_by(statement_id=test_stmt_id).order_by(Transaction.row_index).all()
    assert len(rows) == 2

    # Known debit row
    debit_row = rows[0]
    assert debit_row.direction == Direction.DEBIT
    assert debit_row.debit_paise == 2500000
    assert debit_row.credit_paise is None

    # Known credit row
    credit_row = rows[1]
    assert credit_row.direction == Direction.CREDIT
    assert credit_row.credit_paise == 41200
    assert not credit_row.debit_paise
