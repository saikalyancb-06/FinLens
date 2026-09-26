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

file_path = r'C:\Users\Saikalyan-Kredo\.gemini\antigravity\brain\b63b8291-9a12-45f0-9290-f3843f5fda2d\.user_uploaded\media_1786100924434.csv'
file_bytes = open(file_path, 'rb').read()

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

status, text = http_post_multipart('http://localhost:7999/v1/reconciliation/imports', token, {}, {'file': ('books_ledger_broken.csv', file_bytes, 'text/csv')})
preview_data = json.loads(text)
print('Detected mapping:', preview_data['detected_mapping'])
print('Sample row 0:', preview_data['sample_rows'][0])
db.close()
