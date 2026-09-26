import uuid
import pytest
from fastapi.testclient import TestClient
from main import app
from app.database.session import get_db
from app.models.user import User
from app.models.entity import Entity, Bank
from app.models.account import Account
from app.models.uploaded_file import UploadedFile
from app.models.statement import Statement
from app.models.transaction import Transaction, Direction, SourceType
from app.models.settings import EmailSchedule, UserPreference
from app.models.report import Report
from app.models.reconciliation import ImportTemplate, ImportBatch, ReconciliationRun
from app.models.rpa_job import RpaJob, RpaJobStatus
from app.email.models import ConnectedAccount, EmailAttachment
from app.utils.security import hash_password, create_access_token

client = TestClient(app)


def create_user(prefix: str, db_session):
    email = f"{prefix}_{uuid.uuid4().hex[:8]}@example.com"
    hashed = hash_password("TestPassword123!")
    user = User(email=email, hashed_password=hashed)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    token = create_access_token(data={"sub": str(user.id)})
    headers = {"Authorization": f"Bearer {token}"}
    return user, headers


@pytest.fixture
def test_setup():
    db = next(get_db())
    user_a, headers_a = create_user("tenant_a", db)
    user_b, headers_b = create_user("tenant_b", db)

    # 1. Entities & Accounts
    entity_a = Entity(name="Entity A", user_id=user_a.id)
    entity_b = Entity(name="Entity B", user_id=user_b.id)
    db.add_all([entity_a, entity_b])
    db.commit()

    bank_a = Bank(name="HDFC", code=f"HDFC_{uuid.uuid4().hex[:4]}")
    bank_b = Bank(name="ICICI", code=f"ICICI_{uuid.uuid4().hex[:4]}")
    db.add_all([bank_a, bank_b])
    db.commit()

    acc_a = Account(account_number_masked="****1111", bank_id=bank_a.id, bank_code="HDFC", entity_id=entity_a.id, user_id=user_a.id)
    acc_b = Account(account_number_masked="****2222", bank_id=bank_b.id, bank_code="ICICI", entity_id=entity_b.id, user_id=user_b.id)
    db.add_all([acc_a, acc_b])
    db.commit()

    # 2. Uploaded Files & Statements
    uf_a = UploadedFile(filename="statement_a.pdf", file_path="uploads/statement_a.pdf", file_size=1024, mime_type="application/pdf", user_id=user_a.id)
    uf_b = UploadedFile(filename="statement_b.pdf", file_path="uploads/statement_b.pdf", file_size=2048, mime_type="application/pdf", user_id=user_b.id)
    db.add_all([uf_a, uf_b])
    db.commit()

    stmt_a = Statement(uploaded_file_id=uf_a.id, user_id=user_a.id)
    stmt_b = Statement(uploaded_file_id=uf_b.id, user_id=user_b.id)
    db.add_all([stmt_a, stmt_b])
    db.commit()

    # 3. Transactions
    import datetime
    tx_a = Transaction(
        user_id=user_a.id, account_id=acc_a.id, statement_id=stmt_a.id, entity_id=entity_a.id,
        narration_raw="SWIGGY FOOD ORDER A", narration_clean="SWIGGY", credit_paise=0, debit_paise=50000,
        direction=Direction.DEBIT, txn_date=datetime.date.today()
    )
    tx_b = Transaction(
        user_id=user_b.id, account_id=acc_b.id, statement_id=stmt_b.id, entity_id=entity_b.id,
        narration_raw="SALARY CREDIT B", narration_clean="SALARY", credit_paise=500000, debit_paise=0,
        direction=Direction.CREDIT, txn_date=datetime.date.today()
    )
    db.add_all([tx_a, tx_b])
    db.commit()

    # 4. Settings (Schedules & Preferences)
    pref_a = UserPreference(user_id=user_a.id, default_currency="USD - $", date_format="YYYY-MM-DD")
    pref_b = UserPreference(user_id=user_b.id, default_currency="EUR - €", date_format="DD/MM/YYYY")
    db.add_all([pref_a, pref_b])
    db.commit()

    sched_a = EmailSchedule(user_id=user_a.id, report_type="Report A", frequency="Daily", scheduled_time="09:00", recipients="a@test.com")
    sched_b = EmailSchedule(user_id=user_b.id, report_type="Report B", frequency="Weekly", scheduled_time="10:00", recipients="b@test.com")
    db.add_all([sched_a, sched_b])
    db.commit()

    # 5. Reports
    rep_a = Report(user_id=user_a.id, title="CFO Pack A", report_type="monthly")
    rep_b = Report(user_id=user_b.id, title="CFO Pack B", report_type="monthly")
    db.add_all([rep_a, rep_b])
    db.commit()

    # 6. Reconciliation Runs
    rec_run_a = ReconciliationRun(user_id=user_a.id, account_id=acc_a.id, period_from=datetime.date.today(), period_to=datetime.date.today(), status="COMPLETED")
    rec_run_b = ReconciliationRun(user_id=user_b.id, account_id=acc_b.id, period_from=datetime.date.today(), period_to=datetime.date.today(), status="COMPLETED")
    db.add_all([rec_run_a, rec_run_b])
    db.commit()

    # 7. RPA Jobs
    now_dt = datetime.datetime.now()
    rpa_a = RpaJob(user_id=user_a.id, bank_name="hdfc", date_range_start=now_dt, date_range_end=now_dt, status=RpaJobStatus.SUCCESS)
    rpa_b = RpaJob(user_id=user_b.id, bank_name="icici", date_range_start=now_dt, date_range_end=now_dt, status=RpaJobStatus.SUCCESS)
    db.add_all([rpa_a, rpa_b])
    db.commit()

    data = {
        "user_a": user_a, "headers_a": headers_a,
        "user_b": user_b, "headers_b": headers_b,
        "tx_a": tx_a, "tx_b": tx_b,
        "uf_a": uf_a, "uf_b": uf_b,
        "entity_a": entity_a, "entity_b": entity_b,
        "acc_a": acc_a, "acc_b": acc_b,
        "sched_a": sched_a, "sched_b": sched_b,
        "rep_a": rep_a, "rep_b": rep_b,
        "rec_run_a": rec_run_a, "rec_run_b": rec_run_b,
        "rpa_a": rpa_a, "rpa_b": rpa_b
    }

    yield data

    # Teardown
    db.query(Transaction).filter(Transaction.user_id.in_([user_a.id, user_b.id])).delete()
    db.query(Statement).filter(Statement.user_id.in_([user_a.id, user_b.id])).delete()
    db.query(UploadedFile).filter(UploadedFile.user_id.in_([user_a.id, user_b.id])).delete()
    db.query(Account).filter(Account.user_id.in_([user_a.id, user_b.id])).delete()
    db.query(Bank).filter(Bank.id.in_([bank_a.id, bank_b.id])).delete()
    db.query(Entity).filter(Entity.user_id.in_([user_a.id, user_b.id])).delete()
    db.query(EmailSchedule).filter(EmailSchedule.user_id.in_([user_a.id, user_b.id])).delete()
    db.query(UserPreference).filter(UserPreference.user_id.in_([user_a.id, user_b.id])).delete()
    db.query(Report).filter(Report.user_id.in_([user_a.id, user_b.id])).delete()
    db.query(ReconciliationRun).filter(ReconciliationRun.user_id.in_([user_a.id, user_b.id])).delete()
    db.query(RpaJob).filter(RpaJob.user_id.in_([user_a.id, user_b.id])).delete()
    db.query(User).filter(User.id.in_([user_a.id, user_b.id])).delete()
    db.commit()


# --- 1. Transaction Isolation ---
def test_transaction_isolation(test_setup):
    headers_a = test_setup["headers_a"]
    headers_b = test_setup["headers_b"]
    tx_a = test_setup["tx_a"]
    tx_b = test_setup["tx_b"]

    # User A GET list contains tx_a but NOT tx_b
    res_a = client.get("/transactions", headers=headers_a)
    assert res_a.status_code == 200
    ids_a = [t["id"] for t in res_a.json()]
    assert str(tx_a.id) in ids_a
    assert str(tx_b.id) not in ids_a

    # User A GET tx_b specifically -> MUST FAIL (404)
    res_cross = client.get(f"/transactions/{tx_b.id}", headers=headers_a)
    assert res_cross.status_code == 404

    # User B GET tx_a specifically -> MUST FAIL (404)
    res_cross_b = client.get(f"/transactions/{tx_a.id}", headers=headers_b)
    assert res_cross_b.status_code == 404


# --- 2. Dashboard & Analytics Isolation ---
def test_dashboard_isolation(test_setup):
    headers_a = test_setup["headers_a"]
    headers_b = test_setup["headers_b"]

    dash_a = client.get("/dashboard/summary", headers=headers_a).json()
    dash_b = client.get("/dashboard/summary", headers=headers_b).json()

    assert dash_a["total_transactions"] == 1
    assert dash_a["total_debit"] == 500.0
    assert dash_a["total_credit"] == 0.0

    assert dash_b["total_transactions"] == 1
    assert dash_b["total_debit"] == 0.0
    assert dash_b["total_credit"] == 5000.0


# --- 3. Uploaded Files Isolation ---
def test_file_upload_isolation(test_setup):
    headers_a = test_setup["headers_a"]
    headers_b = test_setup["headers_b"]
    uf_a = test_setup["uf_a"]
    uf_b = test_setup["uf_b"]

    files_a = [f["file_id"] for f in client.get("/files/", headers=headers_a).json()]
    assert str(uf_a.id) in files_a
    assert str(uf_b.id) not in files_a

    # User A trying to fetch status of User B's file -> MUST FAIL (404)
    res = client.get(f"/files/{uf_b.id}", headers=headers_a)
    assert res.status_code == 404


# --- 4. Settings & Schedules Isolation ---
def test_settings_isolation(test_setup):
    headers_a = test_setup["headers_a"]
    headers_b = test_setup["headers_b"]
    sched_a = test_setup["sched_a"]
    sched_b = test_setup["sched_b"]

    pref_a = client.get("/settings/preferences", headers=headers_a).json()
    pref_b = client.get("/settings/preferences", headers=headers_b).json()
    assert pref_a["default_currency"] == "USD - $"
    assert pref_b["default_currency"] == "EUR - €"

    # User A modifying User B's schedule -> MUST FAIL (404)
    res_mod = client.put(f"/settings/email-schedules/{sched_b.id}", json={"scheduled_time": "12:00"}, headers=headers_a)
    assert res_mod.status_code == 404

    # User A deleting User B's schedule -> MUST FAIL (404)
    res_del = client.delete(f"/settings/email-schedules/{sched_b.id}", headers=headers_a)
    assert res_del.status_code == 404


# --- 5. Bank Master & Accounts Isolation ---
def test_bank_master_isolation(test_setup):
    headers_a = test_setup["headers_a"]
    headers_b = test_setup["headers_b"]
    ent_b = test_setup["entity_b"]
    acc_b = test_setup["acc_b"]

    ents_a = [e["id"] for e in client.get("/v1/bank-master/entities", headers=headers_a).json()]
    assert str(ent_b.id) not in ents_a

    accs_a = [a["id"] for a in client.get("/v1/bank-master/accounts", headers=headers_a).json()]
    assert str(acc_b.id) not in accs_a

    # User A updating User B entity -> MUST FAIL (404)
    res_ent_upd = client.patch(f"/v1/bank-master/entities/{ent_b.id}", json={"name": "Hacked Entity"}, headers=headers_a)
    assert res_ent_upd.status_code == 404

    # User A deleting User B account -> MUST FAIL (404)
    res_acc_del = client.delete(f"/v1/bank-master/accounts/{acc_b.id}", headers=headers_a)
    assert res_acc_del.status_code == 404


# --- 6. Reconciliation Isolation ---
def test_reconciliation_isolation(test_setup):
    headers_a = test_setup["headers_a"]
    rec_run_b = test_setup["rec_run_b"]

    runs_a = [r["id"] for r in client.get("/v1/reconciliation/runs", headers=headers_a).json()]
    assert str(rec_run_b.id) not in runs_a

    # User A fetching User B run -> MUST FAIL (404)
    res_run = client.get(f"/v1/reconciliation/runs/{rec_run_b.id}", headers=headers_a)
    assert res_run.status_code == 404


# --- 7. RPA Job Isolation ---
def test_rpa_job_isolation(test_setup):
    headers_a = test_setup["headers_a"]
    rpa_b = test_setup["rpa_b"]

    rpa_jobs_a = [j["job_id"] for j in client.get("/rpa/jobs", headers=headers_a).json()]
    assert str(rpa_b.id) not in rpa_jobs_a

    # User A status poll for User B job -> MUST FAIL (404)
    res_rpa = client.get(f"/rpa/status/{rpa_b.id}", headers=headers_a)
    assert res_rpa.status_code == 404


# --- 8. Bank Account Cascade Deletion ---
def test_bank_account_cascade_deletion(test_setup):
    headers_a = test_setup["headers_a"]
    acc_a = test_setup["acc_a"]
    tx_a = test_setup["tx_a"]

    # Verify transaction exists prior to account deletion
    txs = client.get("/transactions", headers=headers_a).json()
    assert any(t["id"] == str(tx_a.id) for t in txs)

    # Delete bank account
    res_del = client.delete(f"/v1/bank-master/accounts/{acc_a.id}", headers=headers_a)
    assert res_del.status_code == 200

    # Verify transactions & dashboard summary cleared for deleted account
    txs_after = client.get("/transactions", headers=headers_a).json()
    assert not any(t["id"] == str(tx_a.id) for t in txs_after)

    dash_after = client.get("/dashboard/summary", headers=headers_a).json()
    assert dash_after["total_transactions"] == 0
    assert dash_after["total_debit"] == 0.0
    assert dash_after["total_credit"] == 0.0

