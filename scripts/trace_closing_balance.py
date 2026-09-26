import os
import sys
import uuid
from datetime import date

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.parsers.pdf_parser import PDFParser
from app.parsers.pipeline import TransactionParsingPipeline
from app.services.transaction_storage import TransactionStorageService
from app.services.parsing_queue import process_file_parsing_task
from app.database.session import SessionLocal
from app.models.user import User
from app.models.account import Account
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.services.reconciliation_engine import ReconciliationMatchingEngine

FIX = os.path.join(os.path.dirname(__file__), '..', 'fixtures')
PDF = os.path.join(FIX, 'bank_stmt_test3a_v2_bank_high.pdf')


def paise_from_float(f):
    return int(round(float(f) * 100)) if f is not None else None


def main():
    out_lines = []
    def P(s):
        print(s)
        out_lines.append(str(s))

    P('STEP 0: Setup DB user/account')
    db = SessionLocal()
    # create user/account
    user = User(email=f"trace_{uuid.uuid4().hex[:6]}@example.com", hashed_password='x')
    db.add(user)
    db.flush()
    account = Account(user_id=user.id, bank_code='HDFC', account_number_masked='****1234')
    db.add(account)
    db.commit()
    P('User: ' + str(user.id))
    P('Account: ' + str(account.id))

    P('\nSTEP 1: PDF extraction using PDFParser.parse()')
    parser = PDFParser()
    txns = parser.parse(PDF)
    P('Parsed transactions count: ' + str(len(txns)))
    for i,t in enumerate(txns, start=1):
        P(f" Txn {i}: date={t.get('date')}, amount={t.get('amount')}, balance={t.get('balance')}, raw={str(t.get('raw_text'))[:80]}")
    P('Parser.last_statement_closing: ' + str(getattr(parser, 'last_statement_closing', None)))

    P('\nSTEP 2: pipeline.process_file_with_validation()')
    pipeline = TransactionParsingPipeline()
    out = pipeline.process_file_with_validation(PDF)
    P('pipeline total_extracted, total_valid: ' + str((out.get('total_extracted'), out.get('total_valid'))))
    P('pipeline statement_meta: ' + str(out.get('statement_meta')))
    txs = out.get('transactions', [])
    for i,t in enumerate(txs, start=1):
        P(f" Pipe Txn {i}: date={t.get('date')}, amount={t.get('amount')}, balance={t.get('balance')}, raw={str(t.get('raw_text'))[:80]}")

    P('\nSTEP 3: Call parsing queue task to persist Statement and Transactions')
    fid = uuid.uuid4()
    summary = process_file_parsing_task(str(fid), PDF, user_id=str(user.id), account_id=str(account.id))
    P('Parsing queue summary: ' + str(summary))

    # Fetch statement
    stmt = db.query(Statement).filter(Statement.user_id==user.id, Statement.account_id==account.id).order_by(Statement.uploaded_at.desc()).first()
    print('\nSTEP 4: Statement record in DB')
    if stmt:
        P(' Statement.id: ' + str(stmt.id))
        P(' Statement.closing_balance_paise: ' + str(stmt.closing_balance_paise))
        P(' Statement.opening_balance_paise: ' + str(stmt.opening_balance_paise))
        P(' Statement.period_from/to: ' + str(stmt.period_from) + ' / ' + str(stmt.period_to))
    else:
        P(' No Statement found')

    print('\nSTEP 5: Transactions stored in DB (Transaction.balance_paise)')
    txns_db = db.query(Transaction).filter(Transaction.user_id==user.id, Transaction.account_id==account.id).order_by(Transaction.row_index.asc()).all()
    for i,t in enumerate(txns_db, start=1):
        P(f" DB Txn {i}: id={t.id}, txn_date={t.txn_date}, debit={t.debit_paise}, credit={t.credit_paise}, balance_paise={t.balance_paise}, statement_id={t.statement_id}")

    print('\nSTEP 6: Run ReconciliationMatchingEngine.execute_run()')
    engine = ReconciliationMatchingEngine(db, user.id, account.id, date(2026,11,1), date(2026,11,30))
    run = engine.execute_run(force=True)
    P('ReconciliationRun: id= ' + str(run.id))
    P(' run.book_closing_paise: ' + str(run.book_closing_paise))
    P(' run.computed_bank_closing_paise: ' + str(run.computed_bank_closing_paise))
    P(' run.bank_closing_paise: ' + str(run.bank_closing_paise))
    P(' run.residual_paise: ' + str(run.residual_paise))
    P(' run.verdict: ' + str(run.verdict))

    # write trace to file for inspection
    try:
        with open(os.path.join(os.path.dirname(__file__), 'trace_output.txt'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(out_lines))
        P('\nWrote detailed trace to scripts/trace_output.txt')
    except Exception as e:
        P('Failed to write trace file: ' + str(e))

    db.close()

if __name__ == '__main__':
    main()
