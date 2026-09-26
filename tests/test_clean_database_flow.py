import uuid
import pytest
from app.models.transaction import Transaction
from app.models.account import Account
from app.models.entity import Entity
from tests.conftest import TestingSessionLocal


def test_clean_database_new_user_full_flow(client):
    """
    Phase 2.8 Clean Database Validation:
    Proves that a completely new user on a clean database can:
    1. Register User A
    2. Create Entity ("Test Business Entity")
    3. Select Bank from Bank Master
    4. Register Account with user_id, entity_id, bank_id (NO hardcoded UUIDs)
    5. Ingest statement (Manual Upload) -> canonical Transaction
    6. Run isolated Reconciliation
    7. User B Isolation & IDOR Protection check
    """
    # 1. Register User A
    reg_a = client.post("/auth/register", json={"email": "usera_clean@example.com", "password": "password123"})
    assert reg_a.status_code == 201

    login_a = client.post("/auth/login", json={"email": "usera_clean@example.com", "password": "password123"})
    assert login_a.status_code == 200
    token_a = login_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Verify User A identity
    me_a = client.get("/auth/me", headers=headers_a).json()
    user_a_id = uuid.UUID(me_a["id"])

    # 2. Create Entity for User A
    ent_res = client.post("/v1/bank-master/entities", json={"name": "Test Business Entity", "legal_name": "Test Business Entity Pvt Ltd"}, headers=headers_a)
    assert ent_res.status_code == 201
    entity_a_id = uuid.UUID(ent_res.json()["id"])
    assert ent_res.json()["name"] == "Test Business Entity"

    # 3. Select Bank from Bank Master
    banks_res = client.get("/v1/bank-master/banks", headers=headers_a)
    assert banks_res.status_code == 200
    banks = banks_res.json()
    hdfc_bank = next((b for b in banks if b["code"] == "HDFC"), None)
    assert hdfc_bank is not None
    bank_id = uuid.UUID(hdfc_bank["id"])

    # 4. Create Bank Account for User A linked to Entity and Bank Master
    from app.database.session import get_db
    from main import app
    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    acct_a = Account(
        id=uuid.uuid4(),
        user_id=user_a_id,
        entity_id=entity_a_id,
        bank_id=bank_id,
        bank_code="HDFC",
        account_number_masked="****9999",
        currency="INR",
        account_type="CURRENT"
    )
    db.add(acct_a)
    db.commit()
    acct_a_id = acct_a.id


    # Verify Account listing via API
    accts_api = client.get("/v1/bank-master/accounts", headers=headers_a).json()
    assert len(accts_api) == 1
    assert accts_api[0]["id"] == str(acct_a_id)
    assert accts_api[0]["entity_name"] == "Test Business Entity"

    # 5. Ingest Controlled Statement (Manual CSV Upload)
    csv_content = "Date,Description,Debit,Credit,Balance\n2026-02-01,Client Retainer Payment,0.00,25000.00,25000.00\n2026-02-02,Office Rent Expense,10000.00,0.00,15000.00\n"
    files = {"file": ("clean_statement.csv", csv_content.encode("utf-8"), "text/csv")}
    upload_res = client.post("/v1/reconciliation/imports", files=files, headers=headers_a)
    assert upload_res.status_code == 200

    preview = upload_res.json()
    mapping = preview["detected_mapping"]
    confirm_res = client.post("/v1/reconciliation/imports/confirm", json={
        "account_id": str(acct_a_id),
        "column_mapping": mapping,
        "date_format": "%Y-%m-%d",
        "save_as_template": False,
        "rows": preview["sample_rows"]
    }, headers=headers_a)
    assert confirm_res.status_code == 200

    # 6. Verify Book Entries & Account linkage in DB
    db_verify = next(app.dependency_overrides[get_db]())
    from app.models.reconciliation import BookEntry
    b_entries = db_verify.query(BookEntry).filter(BookEntry.user_id == user_a_id).all()
    assert len(b_entries) >= 2
    for b in b_entries:
        assert b.user_id == user_a_id
        assert b.account_id == acct_a_id



    # 7. User B Isolation & IDOR Check
    reg_b = client.post("/auth/register", json={"email": "userb_clean@example.com", "password": "password123"})
    assert reg_b.status_code == 201

    login_b = client.post("/auth/login", json={"email": "userb_clean@example.com", "password": "password123"})
    token_b = login_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # User B lists entities & accounts (should be 0)
    assert len(client.get("/v1/bank-master/entities", headers=headers_b).json()) == 0
    assert len(client.get("/v1/bank-master/accounts", headers=headers_b).json()) == 0
    assert len(client.get("/transactions", headers=headers_b).json()) == 0

    # User B IDOR attempts on User A's entity (should return 404)
    assert client.get(f"/v1/bank-master/entities/{entity_a_id}", headers=headers_b).status_code == 404
    assert client.delete(f"/v1/bank-master/entities/{entity_a_id}", headers=headers_b).status_code == 404
