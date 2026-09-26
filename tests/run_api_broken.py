import sys, json, urllib.request, urllib.parse, io
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
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8')

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

file_path = r'C:\Users\Saikalyan-Kredo\.gemini\antigravity\brain\b63b8291-9a12-45f0-9290-f3843f5fda2d\.user_uploaded\media_1786100924434.csv'
file_bytes = open(file_path, 'rb').read()

status, text = http_post_multipart('http://localhost:7999/v1/reconciliation/imports', token, {'account_id': str(account.id)}, {'file': ('books_ledger_broken.csv', file_bytes, 'text/csv')})
print('=== POST /v1/reconciliation/imports RESPONSE ===')
print(status)
print(text)

if status == 200:
    res = json.loads(text)
    batch_id = res.get('batch_id') or res.get('id')
    run_payload = {
        'account_id': str(account.id),
        'period_from': '2025-01-01',
        'period_to': '2025-01-31'
    }
    if batch_id:
        run_payload['import_batch_id'] = str(batch_id)
        
    status_run, text_run = http_post_json('http://localhost:7999/v1/reconciliation/runs', token, run_payload)
    print('\n=== POST /v1/reconciliation/runs RESPONSE ===')
    print(status_run)
    print(text_run)

    if status_run == 200:
        run_res = json.loads(text_run)
        run_id = run_res.get('id')
        status_get, text_get = http_get(f'http://localhost:7999/v1/reconciliation/runs/{run_id}', token)
        print(f'\n=== GET /v1/reconciliation/runs/{run_id} RAW RESPONSE ===')
        print(text_get)

db.close()
