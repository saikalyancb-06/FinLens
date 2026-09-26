import sys, json, urllib.request, uuid
sys.path.insert(0, '.')
from app.database.session import SessionLocal
from app.api.auth import create_access_token
from app.models.user import User
from app.models.account import Account
from app.models.statement import Statement
from app.parsers.pipeline import TransactionParsingPipeline

db = SessionLocal()
user = db.query(User).filter(User.email == 'demo@kredo.in').first()
account = db.query(Account).filter(Account.user_id == user.id).first()
token = create_access_token({'sub': str(user.id)})

pdf_path = r'C:\Users\Saikalyan-Kredo\.gemini\antigravity\brain\b63b8291-9a12-45f0-9290-f3843f5fda2d\.user_uploaded\media_1786098849276.pdf'
pipeline = TransactionParsingPipeline()
pipeline_output = pipeline.process_file_with_validation(pdf_path)

cont_pass = pipeline_output['continuity_passed']
cont_rate = pipeline_output['continuity_pass_rate']

stmt = Statement(
    id=uuid.uuid4(),
    user_id=user.id,
    account_id=account.id,
    original_filename='Current Account.pdf',
    file_sha256='sha256_current_account_pdf',
    reconciled=cont_pass,
    reconciliation_note=f'continuity_gate_failed: pass_rate={cont_rate*100:.1f}%' if not cont_pass else None
)
db.add(stmt)
db.commit()

print('Stored Statement ID:', stmt.id, '| Reconciled:', stmt.reconciled, '| Note:', stmt.reconciliation_note)

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

run_payload = {
    'account_id': str(account.id),
    'period_from': '2025-01-01',
    'period_to': '2025-01-31',
    'force': False
}

status_run, text_run = http_post_json('http://localhost:7999/v1/reconciliation/runs', token, run_payload)
print('\n=== POST /runs WITH force=False (UNRECONCILED STATEMENT PRESENT) ===')
print('Status Code:', status_run)
print('Response Body:', text_run)

db.delete(stmt)
db.commit()
db.close()
