import os
import sys
from datetime import date, datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.database.session import SessionLocal
from app.services.books_importer import parse_amount_to_paise, compute_row_hash
from app.models.reconciliation import ImportBatch, BookEntry
from app.models.statement import Statement
from app.models.user import User
from app.models.account import Account
from app.models.transaction import Transaction, Direction, SourceType
from app.services.reconciliation_engine import ReconciliationMatchingEngine
from app.parsers.pdf_parser import PDFParser
import uuid
import csv

FIX = os.path.join(os.path.dirname(__file__), '..', 'fixtures')
CSV_PATH = os.path.join(FIX, 'books_ledger_test3_nov2026.csv')
PDF_A = os.path.join(FIX, 'bank_stmt_test3a_v2_bank_high.pdf')
PDF_B = os.path.join(FIX, 'bank_stmt_test3b_v2_books_high.pdf')


def iso_date_from_str(s):
    # Try multiple known formats
    for fmt in ("%d-%m-%Y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    # fallback: try day-month-year with text month
    try:
        return datetime.strptime(s, "%d-%b-%Y").date()
    except Exception:
        return None


def paise(v):
    return int(v)


def create_user_account(db):
    # create test user
    u = User(email=f"test_fixtures_{uuid.uuid4().hex[:6]}@example.com", hashed_password="x")
    db.add(u)
    db.flush()
    a = Account(user_id=u.id, bank_code="HDFC", account_number_masked="****1234")
    db.add(a)
    db.flush()
    return u, a


def import_books_csv(db, user, account):
    # Read CSV and create ImportBatch + BookEntry rows
    with open(CSV_PATH, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    batch = ImportBatch(
        user_id=user.id,
        account_id=account.id,
        filename=os.path.basename(CSV_PATH),
        file_sha256=f"sha_{uuid.uuid4().hex}",
        column_mapping_json={},
        row_count=len(rows),
        book_opening_paise=50000 * 100  # ₹50,000
    )
    db.add(batch)
    db.flush()

    idx = 0
    for r in rows:
        idx += 1
        # Expect columns: date,description,debit,credit
        d = r.get('date') or r.get('Date') or ''
        if not d or d.strip() == ',':
            # closing line
            continue
        entry_date = iso_date_from_str(d.strip()) or date(2026,11,15)
        desc = r.get('description') or r.get('Particulars') or ''
        debit_str = r.get('debit') or r.get('Debit') or ''
        credit_str = r.get('credit') or r.get('Credit') or ''
        money_in = parse_amount_to_paise(credit_str)
        money_out = parse_amount_to_paise(debit_str)
        be = BookEntry(
            user_id=user.id,
            account_id=account.id,
            import_batch_id=batch.id,
            entry_date=entry_date,
            narration=desc,
            instrument_no="",
            money_in_paise=money_in,
            money_out_paise=money_out,
            row_index=idx,
            source_row_hash=compute_row_hash(r)
        )
        db.add(be)
    db.commit()
    # Update book_closing_paise based on in-period calculation
    batch = db.query(ImportBatch).get(batch.id)
    # Compute closing: opening + tot_in - tot_out
    book_in = db.query(BookEntry).filter(BookEntry.import_batch_id==batch.id).all()
    tot_in = sum(b.money_in_paise for b in book_in)
    tot_out = sum(b.money_out_paise for b in book_in)
    batch.book_closing_paise = batch.book_opening_paise + tot_in - tot_out
    db.add(batch)
    db.commit()
    return batch


def insert_statement_and_transactions(db, user, account, pdf_path):
    parser = PDFParser()
    txns = parser.parse(pdf_path)
    if not txns:
        print("No txns parsed from", pdf_path)
    # Create statement
    st = Statement(
        user_id=user.id,
        account_id=account.id,
        source_channel="upload",
        original_filename=os.path.basename(pdf_path),
        period_from=date(2026,11,1),
        period_to=date(2026,11,30),
    )
    db.add(st)
    db.flush()

    idx = 0
    last_bal = None
    for t in txns:
        idx += 1
        dstr = t.get('date') or t.get('txn_date') or ''
        txn_date = iso_date_from_str(dstr) or date(2026,11,10)
        debit = t.get('debit') or t.get('debit_str') or ''
        credit = t.get('credit') or t.get('credit_str') or ''
        # Use parse_amount_to_paise for consistency but values are strings like '9,900.00'
        from app.services.books_importer import parse_amount_to_paise as parse_paise
        debit_p = parse_paise(debit)
        credit_p = parse_paise(credit)
        direction = Direction.CREDIT if credit_p > 0 else Direction.DEBIT
        balance_p = parse_paise(t.get('balance') or t.get('balance_str') or 0)
        last_bal = balance_p
        tx = Transaction(
            user_id=user.id,
            account_id=account.id,
            txn_date=txn_date,
            direction=direction,
            debit_paise=debit_p if debit_p>0 else None,
            credit_paise=credit_p if credit_p>0 else None,
            narration_raw=t.get('description') or '',
            reference_no=t.get('ref') or t.get('reference_number') or '',
            source_type=SourceType.STATEMENT,
            balance_paise=balance_p,
            row_index=idx,
            statement_id=st.id
        )
        db.add(tx)
    # set statement closing balance
    if last_bal is not None:
        st.closing_balance_paise = last_bal
    db.commit()
    return st


def run_recon(db, user, account, batch):
    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026,11,1), date(2026,11,30))
    run = engine.execute_run(import_batch_id=batch.id, force=True)
    return run


def main():
    db = SessionLocal()
    try:
        user, account = create_user_account(db)
        print('Created user', user.id)
        batch = import_books_csv(db, user, account)
        print('Created import batch', batch.id, 'book_closing_paise=', batch.book_closing_paise)

        # Test 3A
        st_a = insert_statement_and_transactions(db, user, account, PDF_A)
        print('Inserted statement A', st_a.id, 'closing_balance_paise=', st_a.closing_balance_paise)
        run_a = run_recon(db, user, account, batch)
        print('\n=== RUN 3A ===')
        print('book_closing_paise:', run_a.book_closing_paise)
        print('computed_bank_closing_paise:', run_a.computed_bank_closing_paise)
        print('bank_closing_paise:', run_a.bank_closing_paise)
        print('residual_paise:', run_a.residual_paise)
        print('verdict:', run_a.verdict)
        direction_text = 'bank shows more than books' if run_a.residual_paise > 0 else 'books show more than bank'
        print('frontend direction text:', direction_text)

        # Clean up statement transactions before Test 3B
        # Delete transactions and statements for the period so we can insert Test3B variation
        db.query(Transaction).filter(Transaction.user_id==user.id, Transaction.account_id==account.id, Transaction.statement_id==st_a.id).delete()
        db.query(Statement).filter(Statement.id==st_a.id).delete()
        db.commit()

        # Insert Test 3B statement
        st_b = insert_statement_and_transactions(db, user, account, PDF_B)
        print('Inserted statement B', st_b.id, 'closing_balance_paise=', st_b.closing_balance_paise)
        run_b = run_recon(db, user, account, batch)
        print('\n=== RUN 3B ===')
        print('book_closing_paise:', run_b.book_closing_paise)
        print('computed_bank_closing_paise:', run_b.computed_bank_closing_paise)
        print('bank_closing_paise:', run_b.bank_closing_paise)
        print('residual_paise:', run_b.residual_paise)
        print('verdict:', run_b.verdict)
        direction_text = 'bank shows more than books' if run_b.residual_paise > 0 else 'books show more than bank'
        print('frontend direction text:', direction_text)

    finally:
        db.close()

if __name__ == '__main__':
    main()
