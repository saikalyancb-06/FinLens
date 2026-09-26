import sys, os, json, csv, urllib.request, io
BASE_URL = "http://localhost:7998"

# Ensure the project root is on PYTHONPATH
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app.database.session import SessionLocal
from app.api.auth import create_access_token
from app.models.user import User
from app.models.account import Account
from app.models.reconciliation import ReconciliationMatchLine, ReconciliationMatch, ReconciliationItem, ReconciliationRun
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.models.reconciliation import ImportBatch, ImportTemplate, BookEntry

def http_post_multipart(url, token, fields, files):
    boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
    body = io.BytesIO()
    for k, v in fields.items():
        part = f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
        body.write(part.encode('utf-8'))
    for k, (fname, content, ctype) in files.items():
        part = f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n'
        body.write(part.encode('utf-8'))
        body.write(content)
        body.write(b'\r\n')
    body.write(f'--{boundary}--\r\n'.encode('utf-8'))
    req = urllib.request.Request(url, data=body.getvalue(), headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': f'multipart/form-data; boundary={boundary}'
    })
    with urllib.request.urlopen(req) as resp:
        return resp.status, resp.read().decode('utf-8')

def http_post_json(url, token, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8')

def http_get(url, token):
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8')

EXPECTED_FOOTERS = {"clean": "48634.14", "exceptions": "76653.14", "broken": "72153.14"}
SEEN_RUN_IDS = set()

def clear_db():
    db = SessionLocal()
    try:
        from app.models.duplicate_match import DuplicateMatch
        db.query(DuplicateMatch).delete(synchronize_session=False)
        db.query(ReconciliationMatchLine).delete(synchronize_session=False)
        db.query(ReconciliationMatch).delete(synchronize_session=False)
        db.query(ReconciliationItem).delete(synchronize_session=False)
        db.query(ReconciliationRun).delete(synchronize_session=False)
        db.query(Transaction).delete(synchronize_session=False)
        db.query(Statement).delete(synchronize_session=False)
        db.query(BookEntry).delete(synchronize_session=False)
        db.query(ImportBatch).delete(synchronize_session=False)
        db.query(ImportTemplate).delete(synchronize_session=False)
        from app.models.processed_transaction import ProcessedTransaction
        from app.models.uploaded_file import UploadedFile
        db.query(ProcessedTransaction).delete(synchronize_session=False)
        db.query(UploadedFile).delete(synchronize_session=False)
        db.commit()
        print('Database cleared')
    finally:
        db.close()

def import_bank_statement(csv_path, user_id):
    abs_path = os.path.abspath(csv_path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"Bank statement CSV not found at {abs_path}")
    from app.services.parsing_queue import process_file_parsing_task
    from app.models.uploaded_file import UploadedFile
    db = SessionLocal()
    try:
        file_size = os.path.getsize(abs_path)
        db_file = UploadedFile(
            user_id=user_id,
            filename=os.path.basename(abs_path),
            file_path=abs_path,
            file_size=file_size,
            mime_type="text/csv",
            status="QUEUED"
        )
        db.add(db_file)
        db.commit()
        db.refresh(db_file)
        file_id = db_file.id
    finally:
        db.close()
    
    # Process synchronously for fixture runner
    res = process_file_parsing_task(file_id, abs_path, user_id=user_id)
    print(f"Bank statement CSV imported: {res.get('status')} ({res.get('total_stored')} transactions)")

def run_fixture(file_path, fname, force_flag):
    # 0. Assert expected footer
    alias = fname.replace('.csv', '').replace('books_ledger_', '')
    if alias in EXPECTED_FOOTERS:
        lines = open(file_path, 'r', encoding='utf-8', errors='ignore').readlines()
        last_line = lines[-1].strip() if lines else ""
        expected = EXPECTED_FOOTERS[alias]
        assert expected in last_line, f"Fixture footer corruption! {fname} last line is '{last_line}', expected footer '{expected}'"
        print(f"Verified {fname} footer contains {expected}")

    # 1. preview import
    file_bytes = open(file_path, 'rb').read()
    status_preview, text_preview = http_post_multipart(f'{BASE_URL}/v1/reconciliation/imports', token, {}, {'file': (fname, file_bytes, 'text/csv')})
    preview = json.loads(text_preview)
    # 2. confirm rows
    rows = list(csv.DictReader(open(file_path, 'r', encoding='utf-8', errors='ignore')))
    confirm_payload = {
        'account_id': str(account.id),
        'column_mapping': preview['detected_mapping'],
        'rows': rows
    }
    status_confirm, text_confirm = http_post_json(f'{BASE_URL}/v1/reconciliation/imports/confirm', token, confirm_payload)
    batch_id = json.loads(text_confirm).get('batch_id')
    # 3. run reconciliation
    run_payload = {
        'account_id': str(account.id),
        'period_from': '2025-01-01',
        'period_to': '2025-01-31',
        'import_batch_id': str(batch_id),
        'force': force_flag
    }
    status_run, text_run = http_post_json(f'{BASE_URL}/v1/reconciliation/runs', token, run_payload)
    print(f"Run API status code: {status_run}, response: {text_run}")
    try:
        parsed_run = json.loads(text_run)
    except Exception as e:
        print(f"FAILED TO PARSE RUN RESPONSE: {status_run} | {text_run}")
        raise e
    run_id = parsed_run.get('run_id') or parsed_run.get('id')
    assert run_id not in SEEN_RUN_IDS, f"stale run id returned: {run_id}"
    SEEN_RUN_IDS.add(run_id)
    
    # 4. fetch result
    status_get, text_get = http_get(f'{BASE_URL}/v1/reconciliation/runs/{run_id}', token)
    parsed_json = json.loads(text_get)
    created_at = parsed_json.get('created_at')
    print(f'=== RAW JSON for {fname} (force={force_flag}, run_id={run_id}, created_at={created_at}) ===')
    print(json.dumps(parsed_json, indent=2))

def import_bank_statement_csv(user_id):
    csv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'bank_alerts.py')) # placeholder check
    # We will import bank_statement_jan2025.csv directly into Statement & Transaction table
    # or parse via BooksImporter logic / direct insert for bank statement
    # Let's check where bank_statement_jan2025.csv exists or load rows
    # In fact, we can parse bank_statement_jan2025.csv using standard csv reader
    pass

if __name__ == '__main__':
    db = SessionLocal()
    user = db.query(User).filter(User.email == 'demo@kredo.in').first()
    if not user:
        from app.utils.security import hash_password
        user = User(email='demo@kredo.in', hashed_password=hash_password('password123'), full_name='Demo User')
        db.add(user)
        db.commit()
        db.refresh(user)
    account = db.query(Account).filter(Account.user_id == user.id).first()
    if not account:
        account = Account(user_id=user.id, bank_code="HDFC", account_number_masked="****1234", account_type="CURRENT")
        db.add(account)
        db.commit()
        db.refresh(account)
    token = create_access_token({'sub': str(user.id)})
    user_id = user.id
    db.close()

    fixtures = [
        (r'fixtures/books_ledger_clean.csv', 'clean'),
        (r'fixtures/books_ledger_exceptions.csv', 'exceptions'),
        (r'fixtures/books_ledger_broken.csv', 'broken'),
    ]
    for rel_path, name in fixtures:
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), rel_path))
        clear_db()
        # Find 45-row bank statement CSV in uploads
        bank_csv_path = os.path.join(os.path.dirname(__file__), 'uploads', '7844a803-18f2-482f-87b2-db43f0edbc8e_47a2d6308be949138b7c3d46e13e131b.csv')
        import_bank_statement(bank_csv_path, user_id)

        # Assertions
        from datetime import date
        db_chk = SessionLocal()
        n = db_chk.query(Transaction).filter(
            Transaction.txn_date.between(date(2025, 1, 1), date(2025, 1, 31))
        ).count()
        assert n == 45, f"expected 45 bank rows, got {n}"
        last = db_chk.query(Transaction).filter(
            Transaction.txn_date.between(date(2025, 1, 1), date(2025, 1, 31))
        ).order_by(Transaction.txn_date.desc(), Transaction.row_index.desc()).first()
        assert int(last.balance_paise) == 10063414, f"bank closing {last.balance_paise}"
        db_chk.close()

        run_fixture(path, f'{name}.csv', False)
    print('All unforced fixture runs completed')
