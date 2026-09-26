import uuid
from datetime import date

import pytest

from app.database.session import Base
from tests.pgtestdb import make_isolated_engine
from app.models.user import User
from app.models.account import Account
from app.models.reconciliation import ImportBatch, BookEntry, RunVerdictEnum
from app.models.statement import Statement
from app.services.reconciliation_engine import ReconciliationMatchingEngine
from app.parsers.pipeline import TransactionParsingPipeline
from app.services.transaction_storage import TransactionStorageService


@pytest.fixture
def db():
    """A dedicated PostgreSQL database, reset for each use — replaces the old
    sqlite:///:memory: engine so this module runs on the production backend.

    Teardown is not optional here. Every test in this module resets the SAME
    database (`make_isolated_engine` drops and rebuilds the schema per call), so
    a session left open by one test holds locks that block the next test's
    `DROP SCHEMA` indefinitely. This used to go unnoticed only because the first
    test failed early enough to have nothing worth locking.
    """
    engine, Session = make_isolated_engine("closing_balance")
    Base.metadata.create_all(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()      # returns the pooled connection, releasing its locks


def setup_user_account(db_session):
    user = User(id=uuid.uuid4(), email=f"test{uuid.uuid4().hex[:6]}@example.com", hashed_password="x")
    account = Account(id=uuid.uuid4(), user_id=user.id, bank_code="HDFC", account_number_masked="****1234")
    db_session.add_all([user, account])
    db_session.commit()
    return user, account


def test_statement_footer_preserved_and_reconciles(db):
    user, account = setup_user_account(db)

    # `bank_stmt_test3a_v2_bank_high.pdf` prints three rows and then
    #   "Closing Balance as on 30-Nov-2026   70,000.00".
    # The rows move the account by +6,100 −1,000 +9,900 = +15,000, so a closing
    # balance DERIVED from the movement is 65,000. The statement states 70,000.
    # That 5,000 gap is the whole point of the fixture ("residual +5000") and it
    # only survives if the stated footer is carried through as statement
    # metadata instead of being recomputed or taken from a row.
    pipeline = TransactionParsingPipeline()
    fixtures_dir = "fixtures"
    pdf_path = f"{fixtures_dir}/bank_stmt_test3a_v2_bank_high.pdf"
    out = pipeline.process_file_with_validation(pdf_path)

    # Ensure transactions parsed and statement-level closing captured
    txns = out["transactions"]
    assert len(txns) == 3, [t.get("balance") for t in txns]
    stmt_meta = out.get("statement_meta", {})
    assert stmt_meta and "closing_balance" in stmt_meta
    closing_val = stmt_meta["closing_balance"]
    assert int(round(closing_val)) == 70000

    # The footer must not be ingested as a fourth transaction — it is metadata.
    # Movement from the parsed rows is +15,000, i.e. a derived closing of 65,000.
    movement = sum((t.get("credit") or 0) - (t.get("debit") or 0) for t in txns)
    assert int(round(movement)) == 15000
    assert int(round(closing_val)) != 50000 + int(round(movement))

    # Check transaction running balances remain
    balances = [t.get("balance") for t in txns if t.get("balance")]
    assert any(int(round(b)) == 56100 for b in balances)
    assert any(int(round(b)) == 55100 for b in balances)

    # Create import batch and one book entry so book_closing = 65000
    batch = ImportBatch(id=uuid.uuid4(), user_id=user.id, account_id=account.id, filename="books.csv", file_sha256="h", column_mapping_json={}, book_opening_paise=50000 * 100)
    db.add(batch)
    db.flush()

    # Book movement +15000 -> book_closing = 65000, matching the bank's movement.
    # The books and the bank agree on every transaction; they disagree only on
    # the stated closing balance, which is what leaves a residual.
    b = BookEntry(id=uuid.uuid4(), user_id=user.id, account_id=account.id, import_batch_id=batch.id, entry_date=date(2026,11,15), money_in_paise=15000 * 100, money_out_paise=0, row_index=1, source_row_hash="h1")
    db.add(b)
    db.commit()

    # Persist statement with explicit closing balance
    st = Statement(id=uuid.uuid4(), user_id=user.id, account_id=account.id, period_from=date(2026,11,1), period_to=date(2026,11,30), closing_balance_paise=int(round(closing_val * 100)))
    db.add(st)
    db.commit()

    # Store parsed transactions into DB linked to statement
    storage = TransactionStorageService()
    stored = storage.store_transactions(db=db, processed_txns=txns, user_id=user.id, account_id=account.id, statement_id=st.id)
    # At least two transactions stored
    assert len(stored) >= 2

    # Run reconciliation
    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026,11,1), date(2026,11,30))
    run = engine.execute_run(import_batch_id=batch.id, force=True)

    assert run.book_closing_paise == 65000 * 100
    assert run.computed_bank_closing_paise == 65000 * 100
    # The STATED closing balance, carried from the footer — not the 65,000 the
    # movement implies. If the footer were ever dropped or recomputed this drops
    # to 65,000 and the residual silently becomes zero.
    assert run.bank_closing_paise == 70000 * 100
    assert run.residual_paise == 5000 * 100
    assert run.verdict == RunVerdictEnum.UNRECONCILED.value


def test_no_footer_behaviour_unchanged(db):
    user, account = setup_user_account(db)

    # Use a PDF where footer equals last txn balance (existing behaviour)
    pipeline = TransactionParsingPipeline()
    pdf_path = "fixtures/bank_stmt_test3b_v2_books_high.pdf"
    out = pipeline.process_file_with_validation(pdf_path)
    txns = out["transactions"]
    stmt_meta = out.get("statement_meta", {})

    # This fixture includes a footer in the table rows; either explicit footer captured or last txn matches.
    # Ensure parsing still yields transactions
    assert len(txns) >= 2

    # Create batch and book entry producing book_closing == 40000
    batch = ImportBatch(id=uuid.uuid4(), user_id=user.id, account_id=account.id, filename="books.csv", file_sha256="h2", column_mapping_json={}, book_opening_paise=50000 * 100)
    db.add(batch)
    db.flush()
    b = BookEntry(id=uuid.uuid4(), user_id=user.id, account_id=account.id, import_batch_id=batch.id, entry_date=date(2026,11,15), money_in_paise=0, money_out_paise=10000 * 100, row_index=1, source_row_hash="h2")
    db.add(b)
    db.commit()

    # If pipeline provided statement_meta, use it; otherwise create statement using last txn balance
    closing_val = stmt_meta.get("closing_balance") if stmt_meta else None
    if closing_val is None:
        # fallback to last txn balance
        closing_val = txns[-1].get("balance")

    st = Statement(id=uuid.uuid4(), user_id=user.id, account_id=account.id, period_from=date(2026,11,1), period_to=date(2026,11,30), closing_balance_paise=int(round(float(closing_val) * 100)))
    db.add(st)
    db.commit()

    storage = TransactionStorageService()
    stored = storage.store_transactions(db=db, processed_txns=txns, user_id=user.id, account_id=account.id, statement_id=st.id)
    assert len(stored) >= 2

    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026,11,1), date(2026,11,30))
    run = engine.execute_run(import_batch_id=batch.id, force=True)

    # Ensure verdict calculated (no assertion on numbers here beyond sanity)
    assert run.verdict in (RunVerdictEnum.UNRECONCILED.value, RunVerdictEnum.RECONCILED_CLEAN.value, RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value)


def _write_statement_pdf(path, rows, footer):
    """A minimal text-layout statement — no ruled cells, so it exercises the
    line-based extraction path rather than table extraction."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 9)
    y = 800
    c.drawString(40, y, "HDFC Bank Ltd - Statement of Account")
    y -= 14
    c.drawString(40, y, "Date Description Ref No Debit Credit Balance")
    for row in rows:
        y -= 14
        c.drawString(40, y, " ".join(row))
    if footer is not None:
        y -= 14
        c.drawString(40, y, f"Closing Balance as on 30-Nov-2026 {footer}")
    c.save()


# The rows below close at 58,900 while the footer states 63,900. The two must
# stay distinguishable, which is exactly what the checked-in fixtures cannot do:
# in `bank_stmt_test3a_v2_bank_high.pdf` the footer and the final running
# balance are both 70,000, so "read the footer" and "read the last row" produce
# the same answer there and the preference is untestable.
_ROWS = [
    ("10-Nov-2026", "Interest Credit", "INT202611", "", "9,900.00", "59,900.00"),
    ("20-Nov-2026", "Bank Charge", "CHG202611", "1,000.00", "", "58,900.00"),
]


def test_explicit_footer_beats_the_last_rows_running_balance(tmp_path):
    """The stated closing balance wins over the final row's running balance.

    Regression: statement metadata was only read on the OCR path, so a
    statement parsed by the offline engine or by line-based text extraction
    lost its footer entirely and reconciliation fell back to the last row.
    """
    pdf = tmp_path / "footer_differs.pdf"
    _write_statement_pdf(pdf, _ROWS, "63,900.00")

    out = TransactionParsingPipeline().process_file_with_validation(str(pdf))
    txns = out["transactions"]

    # The footer is metadata, not a row: two transactions, not three.
    assert len(txns) == 2, [t.get("balance") for t in txns]
    balances = [int(round(t["balance"])) for t in txns if t.get("balance")]
    assert balances == [59900, 58900]

    closing = out["statement_meta"]["closing_balance"]
    assert int(round(closing)) == 63900
    # The load-bearing part: it is the footer, not the last running balance.
    assert int(round(closing)) != balances[-1]


def test_a_statement_without_a_footer_reports_no_closing_balance(tmp_path):
    """No footer means no statement-level closing balance — not the previous
    statement's.

    Regression: `last_statement_closing` lived on the parser instance and was
    never cleared, and the pipeline holds one parser for its lifetime. Parsing a
    statement that HAS a footer and then one that does not reported the first
    statement's closing balance as the second's.
    """
    with_footer = tmp_path / "with_footer.pdf"
    without_footer = tmp_path / "without_footer.pdf"
    _write_statement_pdf(with_footer, _ROWS, "63,900.00")
    _write_statement_pdf(without_footer, _ROWS, None)

    pipeline = TransactionParsingPipeline()          # one parser, two files
    first = pipeline.process_file_with_validation(str(with_footer))
    assert int(round(first["statement_meta"]["closing_balance"])) == 63900

    second = pipeline.process_file_with_validation(str(without_footer))
    assert second["statement_meta"] == {}
