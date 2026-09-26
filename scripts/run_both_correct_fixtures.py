import sys, json, csv, urllib.request, io
sys.path.insert(0, '.')
from app.database.session import SessionLocal
from app.api.auth import create_access_token
from app.models.user import User
from app.models.account import Account

db = SessionLocal()
user = db.query(User).filter(User.email == 'demo@kredo.in').first()
account = db.query(Account).filter(Account.user_id == user.id).first()
token = create_access_token({'sub': str(user.id)})

def http_post_multipart(url, token, fields, files):
    boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
    body = io.BytesIO()
    for k, v in fields.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode('utf-8'))
    for k, (fname, content, ctype) in files.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n'.encode('utf-8'))
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

def run_fixture_api(file_path, fname):
    print(f"\n==================================================")
    print(f"RUNNING FIXTURE: {fname}")
    print(f"==================================================")
    file_bytes = open(file_path, 'rb').read()

    status_preview, text_preview = http_post_multipart('http://localhost:7999/v1/reconciliation/imports', token, {}, {'file': (fname, file_bytes, 'text/csv')})
    print('=== 1. POST /v1/reconciliation/imports ===')
    print('Status:', status_preview)
    print('Response:', text_preview)

    preview_data = json.loads(text_preview)

    lines = open(file_path, 'r', encoding='utf-8', errors='ignore').readlines()
    reader = csv.DictReader(lines)
    rows = [row for row in reader]

    confirm_payload = {
        'account_id': str(account.id),
        'column_mapping': preview_data['detected_mapping'],
        'rows': rows
    }

    status_confirm, text_confirm = http_post_json('http://localhost:7999/v1/reconciliation/imports/confirm', token, confirm_payload)
    print('\n=== 2. POST /v1/reconciliation/imports/confirm ===')
    print('Status:', status_confirm)
    print('Response:', text_confirm)

    confirm_res = json.loads(text_confirm)
    batch_id = confirm_res.get('batch_id')

    run_payload = {
        'account_id': str(account.id),
        'period_from': '2025-01-01',
        'period_to': '2025-01-31',
        'import_batch_id': str(batch_id),
        'force': True
    }

    status_run, text_run = http_post_json('http://localhost:7999/v1/reconciliation/runs', token, run_payload)
    print('\n=== 3. POST /v1/reconciliation/runs ===')
    print('Status:', status_run)
    print('Response:', text_run)

    run_res = json.loads(text_run)
    run_id = run_res.get('run_id') or run_res.get('id')

    status_get, text_get = http_get(f'http://localhost:7999/v1/reconciliation/runs/{run_id}', token)
    print(f'\n=== 4. GET /v1/reconciliation/runs/{run_id} RAW JSON ===')
    print(text_get)

run_fixture_api(r'fixtures/books_ledger_broken.csv', 'books_ledger_broken.csv')
run_fixture_api(r'fixtures/books_ledger_exceptions.csv', 'books_ledger_exceptions.csv')

db.close()
