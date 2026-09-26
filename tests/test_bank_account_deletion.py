import uuid
import datetime
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
from app.models.prediction import Prediction
from app.models.duplicate_match import DuplicateMatch, MatchStatus, DuplicateTier
from app.models.reconciliation import (
    ImportBatch, BookEntry, ReconciliationRun, ReconciliationMatch,
    ReconciliationMatchLine, ReconciliationItem, RunVerdictEnum
)
from app.utils.security import hash_password, create_access_token

client = TestClient(app)


def create_test_user(prefix: str, db):
    email = f"{prefix}_{uuid.uuid4().hex[:8]}@example.com"
    user = User(email=email, hashed_password=hash_password("Pass123!"))
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(data={"sub": str(user.id)})
    headers = {"Authorization": f"Bearer {token}"}
    return user, headers


@pytest.fixture
def setup_bank_account_data():
    db = next(get_db())
    user_a, headers_a = create_test_user("user_a", db)
    user_b, headers_b = create_test_user("user_b", db)

    # Entity & Bank
    entity = Entity(name="Corp Entity", user_id=user_a.id)
    db.add(entity)
    db.commit()

    bank = Bank(name="HDFC Bank", code=f"HDFC_{uuid.uuid4().hex[:4]}")
    db.add(bank)
    db.commit()

    # Account 1 (to be deleted) & Account 2 (sibling, to remain)
    acc1 = Account(account_number_masked="****1001", bank_id=bank.id, bank_code="HDFC", entity_id=entity.id, user_id=user_a.id)
    acc2 = Account(account_number_masked="****2002", bank_id=bank.id, bank_code="HDFC", entity_id=entity.id, user_id=user_a.id)
    db.add_all([acc1, acc2])
    db.commit()

    # Files & Statements for Acc 1 and Acc 2
    uf1 = UploadedFile(filename="acc1_stmt.pdf", file_path="uploads/acc1.pdf", file_size=100, user_id=user_a.id)
    uf2 = UploadedFile(filename="acc2_stmt.pdf", file_path="uploads/acc2.pdf", file_size=200, user_id=user_a.id)
    db.add_all([uf1, uf2])
    db.commit()

    stmt1 = Statement(account_id=acc1.id, uploaded_file_id=uf1.id, user_id=user_a.id)
    stmt2 = Statement(account_id=acc2.id, uploaded_file_id=uf2.id, user_id=user_a.id)
    db.add_all([stmt1, stmt2])
    db.commit()

    # Transactions: 2 for Acc1, 1 for Acc2
    tx1_acc1 = Transaction(
        user_id=user_a.id, account_id=acc1.id, statement_id=stmt1.id, entity_id=entity.id,
        narration_raw="TX 1 ACC 1", narration_clean="TX 1", credit_paise=0, debit_paise=100000,
        direction=Direction.DEBIT, txn_date=datetime.date.today()
    )
    tx2_acc1 = Transaction(
        user_id=user_a.id, account_id=acc1.id, statement_id=stmt1.id, entity_id=entity.id,
        narration_raw="TX 2 ACC 1", narration_clean="TX 2", credit_paise=500000, debit_paise=0,
        direction=Direction.CREDIT, txn_date=datetime.date.today()
    )
    tx1_acc2 = Transaction(
        user_id=user_a.id, account_id=acc2.id, statement_id=stmt2.id, entity_id=entity.id,
        narration_raw="TX 1 ACC 2", narration_clean="TX 1 ACC 2", credit_paise=0, debit_paise=250000,
        direction=Direction.DEBIT, txn_date=datetime.date.today()
    )
    db.add_all([tx1_acc1, tx2_acc1, tx1_acc2])
    db.commit()

    # Prediction for Acc 1 tx
    pred = Prediction(transaction_id=tx1_acc1.id, predicted_category="Office Expenses", confidence=0.99)
    db.add(pred)

    # Deduplication reference
    dup_match = DuplicateMatch(
        user_id=user_a.id, duplicate_txn_id=tx1_acc2.id, kept_txn_id=tx2_acc1.id,
        confidence=0.9, tier=DuplicateTier.TIER_1, status=MatchStatus.CONFIRMED
    )
    db.add(dup_match)

    # Reconciliation Run & Book Entry for Acc 1 and Acc 2
    rec_run1 = ReconciliationRun(user_id=user_a.id, account_id=acc1.id, period_from=datetime.date.today(), period_to=datetime.date.today(), status="COMPLETED")
    rec_run2 = ReconciliationRun(user_id=user_a.id, account_id=acc2.id, period_from=datetime.date.today(), period_to=datetime.date.today(), status="COMPLETED")
    db.add_all([rec_run1, rec_run2])
    db.commit()

    batch1 = ImportBatch(user_id=user_a.id, account_id=acc1.id, filename="b1.csv", file_sha256="sha1", column_mapping_json={})
    batch2 = ImportBatch(user_id=user_a.id, account_id=acc2.id, filename="b2.csv", file_sha256="sha2", column_mapping_json={})
    db.add_all([batch1, batch2])
    db.commit()

    book1 = BookEntry(user_id=user_a.id, account_id=acc1.id, import_batch_id=batch1.id, entry_date=datetime.date.today(), narration="Book 1", money_out_paise=100000, money_in_paise=0, row_index=1, source_row_hash="h1")
    book2 = BookEntry(user_id=user_a.id, account_id=acc2.id, import_batch_id=batch2.id, entry_date=datetime.date.today(), narration="Book 2", money_out_paise=250000, money_in_paise=0, row_index=1, source_row_hash="h2")
    db.add_all([book1, book2])
    db.commit()

    from app.models.reconciliation import MatchTierEnum
    rec_match = ReconciliationMatch(run_id=rec_run1.id, user_id=user_a.id, tier=MatchTierEnum.TIER_1, status="auto_matched")
    db.add(rec_match)
    db.commit()

    data = {
        "db": db,
        "user_a": user_a, "headers_a": headers_a,
        "user_b": user_b, "headers_b": headers_b,
        "entity": entity, "bank": bank,
        "acc1": acc1, "acc2": acc2,
        "uf1": uf1, "uf2": uf2,
        "stmt1": stmt1, "stmt2": stmt2,
        "tx1_acc1": tx1_acc1, "tx2_acc1": tx2_acc1, "tx1_acc2": tx1_acc2,
        "rec_run1": rec_run1, "rec_run2": rec_run2
    }

    yield data

    # Teardown
    db.query(Prediction).delete()
    db.query(DuplicateMatch).delete()
    db.query(ReconciliationMatchLine).delete()
    db.query(ReconciliationMatch).delete()
    db.query(ReconciliationItem).delete()
    db.query(ReconciliationRun).delete()
    db.query(BookEntry).delete()
    db.query(ImportBatch).delete()
    db.query(Transaction).delete()
    db.query(Statement).delete()
    db.query(UploadedFile).delete()
    db.query(Account).delete()
    db.query(Bank).filter(Bank.id == bank.id).delete()
    db.query(Entity).delete()
    db.query(User).filter(User.id.in_([user_a.id, user_b.id])).delete()
    db.commit()


def test_basic_deletion_and_dashboard_metrics(setup_bank_account_data):
    headers_a = setup_bank_account_data["headers_a"]
    acc1 = setup_bank_account_data["acc1"]
    acc2 = setup_bank_account_data["acc2"]

    # 1. Verify initial Dashboard Summary includes both accounts (Acc 1 debit 1000 + Acc 2 debit 2500 = 3500)
    dash_before = client.get("/dashboard/summary", headers=headers_a).json()
    assert dash_before["total_debit"] == 3500.0
    assert dash_before["total_credit"] == 5000.0

    # 2. Delete Acc 1
    res_del = client.delete(f"/v1/bank-master/accounts/{acc1.id}", headers=headers_a)
    assert res_del.status_code == 200

    # 3. Assert Acc 1 is purged and absent from account query
    accs = client.get("/v1/bank-master/accounts", headers=headers_a).json()
    assert not any(a["id"] == str(acc1.id) for a in accs)
    assert any(a["id"] == str(acc2.id) for a in accs)

    # 4. Assert Dashboard summary excludes Acc 1 completely (only Acc 2 debit 2500 remaining)
    dash_after = client.get("/dashboard/summary", headers=headers_a).json()
    assert dash_after["total_debit"] == 2500.0
    assert dash_after["total_credit"] == 0.0


def test_multiple_accounts_and_entity_preservation(setup_bank_account_data):
    headers_a = setup_bank_account_data["headers_a"]
    entity = setup_bank_account_data["entity"]
    acc1 = setup_bank_account_data["acc1"]
    acc2 = setup_bank_account_data["acc2"]

    # Delete Account 1
    res = client.delete(f"/v1/bank-master/accounts/{acc1.id}", headers=headers_a)
    assert res.status_code == 200

    # Entity remains completely intact
    ent_res = client.get(f"/v1/bank-master/entities/{entity.id}", headers=headers_a)
    assert ent_res.status_code == 200
    assert ent_res.json()["name"] == "Corp Entity"

    # Account 2 remains completely functional
    txs_acc2 = client.get(f"/transactions?bank_id={acc2.id}", headers=headers_a).json()
    assert len(txs_acc2) == 1


def test_cross_user_deletion_blocked(setup_bank_account_data):
    headers_b = setup_bank_account_data["headers_b"]
    acc1 = setup_bank_account_data["acc1"]

    # User B attempting to delete User A's account -> MUST BE BLOCKED (404/403)
    res = client.delete(f"/v1/bank-master/accounts/{acc1.id}", headers=headers_b)
    assert res.status_code in [404, 403]


def test_deduplication_pointer_unlinking(setup_bank_account_data):
    db = setup_bank_account_data["db"]
    headers_a = setup_bank_account_data["headers_a"]
    acc1 = setup_bank_account_data["acc1"]
    tx2_acc1 = setup_bank_account_data["tx2_acc1"]
    tx1_acc2 = setup_bank_account_data["tx1_acc2"]

    # Set tx1_acc2 to be superseded by tx2_acc1 (which belongs to acc1)
    tx1_acc2.superseded_by_id = tx2_acc1.id
    db.commit()
    db.refresh(tx1_acc2)
    assert tx1_acc2.superseded_by_id == tx2_acc1.id

    # Delete Acc 1
    client.delete(f"/v1/bank-master/accounts/{acc1.id}", headers=headers_a)

    # Verify tx1_acc2 is no longer superseded by a deleted transaction
    db.refresh(tx1_acc2)
    assert tx1_acc2.superseded_by_id is None


def test_entity_deletion_after_all_accounts_deleted(setup_bank_account_data):
    db = setup_bank_account_data["db"]
    headers_a = setup_bank_account_data["headers_a"]
    entity = setup_bank_account_data["entity"]
    acc1 = setup_bank_account_data["acc1"]
    acc2 = setup_bank_account_data["acc2"]

    # 1. Delete both bank accounts using normal endpoint
    res1 = client.delete(f"/v1/bank-master/accounts/{acc1.id}", headers=headers_a)
    assert res1.status_code == 200
    res2 = client.delete(f"/v1/bank-master/accounts/{acc2.id}", headers=headers_a)
    assert res2.status_code == 200

    # 2. Query database directly: Assert active bank accounts for Entity == 0
    active_acc_count = db.query(Account).filter(
        Account.entity_id == entity.id,
        Account.deleted_at == None
    ).count()
    assert active_acc_count == 0

    # 3. Assert entity deletion endpoint does NOT report linked accounts and succeeds
    del_ent_res = client.delete(f"/v1/bank-master/entities/{entity.id}", headers=headers_a)
    assert del_ent_res.status_code == 200
    assert del_ent_res.json()["status"] == "success"

    # 4. Verify entity is deleted from database
    ent_in_db = db.query(Entity).filter(Entity.id == entity.id).first()
    assert ent_in_db is None



def test_update_account_entity_and_type(setup_bank_account_data):
    db = setup_bank_account_data["db"]
    headers_a = setup_bank_account_data["headers_a"]
    user_a = setup_bank_account_data["user_a"]
    acc1 = setup_bank_account_data["acc1"]

    # Create a second entity
    new_entity = Entity(name="Entity Two", user_id=user_a.id)
    db.add(new_entity)
    db.commit()

    # Update account entity link and account_type via PATCH API
    res = client.patch(
        f"/v1/bank-master/accounts/{acc1.id}",
        json={"entity_id": str(new_entity.id), "account_type": "OVERDRAFT"},
        headers=headers_a
    )
    assert res.status_code == 200
    res_data = res.json()
    assert res_data["entity_id"] == str(new_entity.id)
    assert res_data["entity_name"] == "Entity Two"
    assert res_data["account_type"] == "OVERDRAFT"

    # Verify changes directly in the database
    db.refresh(acc1)
    assert acc1.entity_id == new_entity.id
    assert acc1.account_type == "OVERDRAFT"


def test_dashboard_summary_filtered_counts():
    """Verify that CFO / Board Pack entity_count and account_count dynamically adapt to active filters."""
    db = next(get_db())
    user, headers = create_test_user("dash_count_user", db)

    # Create Entity 1 (with Acc 1 and Acc 2) & Entity 2 (with Acc 3)
    ent1 = Entity(name="Entity One", user_id=user.id)
    ent2 = Entity(name="Entity Two", user_id=user.id)
    db.add_all([ent1, ent2])
    db.commit()

    bank = Bank(name="State Bank", code=f"SBI_{uuid.uuid4().hex[:4]}")
    db.add(bank)
    db.commit()

    acc1 = Account(account_number_masked="****1111", bank_id=bank.id, bank_code="SBI", entity_id=ent1.id, user_id=user.id)
    acc2 = Account(account_number_masked="****2222", bank_id=bank.id, bank_code="SBI", entity_id=ent1.id, user_id=user.id)
    acc3 = Account(account_number_masked="****3333", bank_id=bank.id, bank_code="SBI", entity_id=ent2.id, user_id=user.id)
    db.add_all([acc1, acc2, acc3])
    db.commit()

    # Add transactions
    # Acc 1 (Ent 1): 2025-01-10
    # Acc 2 (Ent 1): 2025-01-15
    # Acc 3 (Ent 2): 2025-02-01
    tx1 = Transaction(user_id=user.id, account_id=acc1.id, entity_id=ent1.id, txn_date=datetime.date(2025, 1, 10), narration_raw="Tx1", credit_paise=10000, direction=Direction.CREDIT, source_type=SourceType.STATEMENT)
    tx2 = Transaction(user_id=user.id, account_id=acc2.id, entity_id=ent1.id, txn_date=datetime.date(2025, 1, 15), narration_raw="Tx2", credit_paise=20000, direction=Direction.CREDIT, source_type=SourceType.STATEMENT)
    tx3 = Transaction(user_id=user.id, account_id=acc3.id, entity_id=ent2.id, txn_date=datetime.date(2025, 2, 1), narration_raw="Tx3", credit_paise=30000, direction=Direction.CREDIT, source_type=SourceType.STATEMENT)
    db.add_all([tx1, tx2, tx3])
    db.commit()

    # 1. No filters: 2 entities, 3 accounts
    r1 = client.get("/dashboard/summary", headers=headers)
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["entities_count"] == 2
    assert d1["accounts_count"] == 3

    # 2. Entity 1 filter: 1 entity, 2 accounts (Acc 1 & Acc 2)
    r2 = client.get(f"/dashboard/summary?entity_id={ent1.id}", headers=headers)
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["entities_count"] == 1
    assert d2["accounts_count"] == 2

    # 3. Entity 2 filter: 1 entity, 1 account (Acc 3)
    r3 = client.get(f"/dashboard/summary?entity_id={ent2.id}", headers=headers)
    assert r3.status_code == 200
    d3 = r3.json()
    assert d3["entities_count"] == 1
    assert d3["accounts_count"] == 1

    # 4. Specific Bank Account filter (Acc 1): 1 entity, 1 account
    r4 = client.get(f"/dashboard/summary?bank_id={acc1.id}", headers=headers)
    assert r4.status_code == 200
    d4 = r4.json()
    assert d4["entities_count"] == 1
    assert d4["accounts_count"] == 1

    # 5. Date filter reducing to Jan 2025: full entity/account scope retained (2 entities, 3 accounts)
    r5 = client.get("/dashboard/summary?from_date=2025-01-01&to_date=2025-01-31", headers=headers)
    assert r5.status_code == 200
    d5 = r5.json()
    assert d5["entities_count"] == 2
    assert d5["accounts_count"] == 3
    assert d5["total_transactions"] == 2

    # 6. Date filter with no transactions (2030): full scope retained (2 entities, 3 accounts), metrics = 0
    r6 = client.get("/dashboard/summary?from_date=2030-01-01&to_date=2030-01-31", headers=headers)
    assert r6.status_code == 200
    d6 = r6.json()
    assert d6["entities_count"] == 2
    assert d6["accounts_count"] == 3
    assert d6["total_transactions"] == 0
    assert d6["total_credit"] == 0.0



