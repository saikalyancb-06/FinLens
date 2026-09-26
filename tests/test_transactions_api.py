import uuid
import pytest
from datetime import date
from fastapi.testclient import TestClient
from app.models.transaction import Transaction, Direction
from app.models.account import Account
from app.models.statement import Statement
from app.database.session import get_db


def get_auth_headers(client: TestClient, email: str, password: str = "Secret123!"):
    reg_res = client.post("/auth/register", json={
        "email": email,
        "password": password,
        "full_name": email.split("@")[0].title(),
        "entity_type": "BUSINESS"
    })
    login_res = client.post("/auth/login", json={"email": email, "password": password})
    token = login_res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_1_create_canonical_transaction(client: TestClient):
    headers = get_auth_headers(client, "tx_user1@example.com")
    payload = {
        "txn_date": "2026-01-15",
        "narration_raw": "SALARY CREDIT HDFC BANK",
        "debit_paise": 0,
        "credit_paise": 25000000,
        "balance_paise": 55000000,
        "reference_no": "REF123456"
    }
    res = client.post("/transactions/", headers=headers, json=payload)
    assert res.status_code == 201
    data = res.json()
    assert "id" in data
    assert data["narration_raw"] == "SALARY CREDIT HDFC BANK"
    assert data["credit_paise"] == 25000000
    assert data["direction"] == "credit"
    assert data["credit"] == 250000.0


def test_2_get_canonical_transactions_list(client: TestClient):
    headers = get_auth_headers(client, "tx_user2@example.com")
    client.post("/transactions/", headers=headers, json={
        "txn_date": "2026-01-16",
        "narration_raw": "SWIGGY BANGALORE UPI",
        "debit_paise": 45000,
        "credit_paise": 0,
        "balance_paise": 54955000
    })

    res = client.get("/transactions", headers=headers)
    assert res.status_code == 200
    txns = res.json()
    assert len(txns) >= 1
    t = txns[0]
    assert t["debit_paise"] == 45000
    assert t["direction"] == "debit"
    assert t["debit"] == 450.0


def test_3_get_canonical_transaction_detail(client: TestClient):
    headers = get_auth_headers(client, "tx_user3@example.com")
    created = client.post("/transactions/", headers=headers, json={
        "txn_date": "2026-01-18",
        "narration_raw": "AWS CLOUD SERVICES",
        "debit_paise": 890000,
        "credit_paise": 0,
        "balance_paise": 50000000
    }).json()

    tx_id = created["id"]
    detail_res = client.get(f"/transactions/{tx_id}", headers=headers)
    assert detail_res.status_code == 200
    detail = detail_res.json()

    # Both list and detail representations must be identical
    assert detail["id"] == created["id"]
    assert detail["debit_paise"] == created["debit_paise"]
    assert detail["direction"] == created["direction"]
    assert detail["narration_raw"] == created["narration_raw"]


def test_4_paise_preservation_roundtrip(client: TestClient):
    headers = get_auth_headers(client, "tx_user4@example.com")
    exact_debit_paise = 1234567
    exact_balance_paise = 987654321

    created = client.post("/transactions/", headers=headers, json={
        "txn_date": "2026-01-20",
        "narration_raw": "VENDOR PAYMENT EXACT PAISE",
        "debit_paise": exact_debit_paise,
        "credit_paise": 0,
        "balance_paise": exact_balance_paise
    }).json()

    assert created["debit_paise"] == exact_debit_paise
    assert created["balance_paise"] == exact_balance_paise

    detail = client.get(f"/transactions/{created['id']}", headers=headers).json()
    assert detail["debit_paise"] == exact_debit_paise
    assert detail["balance_paise"] == exact_balance_paise


def test_5_direction_derivation(client: TestClient):
    headers = get_auth_headers(client, "tx_user5@example.com")

    # Debit direction
    deb_tx = client.post("/transactions/", headers=headers, json={
        "txn_date": "2026-01-21",
        "narration_raw": "DEBIT TEST",
        "debit_paise": 1000,
        "credit_paise": 0
    }).json()
    assert deb_tx["direction"] == "debit"

    # Credit direction
    cred_tx = client.post("/transactions/", headers=headers, json={
        "txn_date": "2026-01-21",
        "narration_raw": "CREDIT TEST",
        "debit_paise": 0,
        "credit_paise": 2000
    }).json()
    assert cred_tx["direction"] == "credit"


def test_6_transaction_date_handling(client: TestClient):
    headers = get_auth_headers(client, "tx_user6@example.com")
    target_date = "2026-02-28"

    created = client.post("/transactions/", headers=headers, json={
        "txn_date": target_date,
        "narration_raw": "DATE VERIFICATION",
        "debit_paise": 5000,
        "credit_paise": 0
    }).json()

    assert created["txn_date"] == target_date
    assert created["date"] == target_date


def test_7_user_isolation_get_detail(client: TestClient):
    headers_a = get_auth_headers(client, "tx_user7_a@example.com")
    headers_b = get_auth_headers(client, "tx_user7_b@example.com")

    created_a = client.post("/transactions/", headers=headers_a, json={
        "txn_date": "2026-01-22",
        "narration_raw": "USER A TX",
        "debit_paise": 1000,
        "credit_paise": 0
    }).json()

    # User B attempts to fetch User A's transaction
    res_b = client.get(f"/transactions/{created_a['id']}", headers=headers_b)
    assert res_b.status_code == 404
    assert res_b.json()["detail"] == "Transaction not found"


def test_8_account_ownership_validation(client: TestClient):
    headers_a = get_auth_headers(client, "tx_user8_a@example.com")
    headers_b = get_auth_headers(client, "tx_user8_b@example.com")

    # Create bank account for User A via API
    acc_res = client.post("/v1/bank-master/accounts", headers=headers_a, json={
        "bank_code": "HDFC",
        "account_number": "502000123456",
        "account_type": "CURRENT"
    })
    assert acc_res.status_code == 201
    user_a_acc_id = acc_res.json()["id"]

    # User B attempts to create transaction using User A's account_id
    res_b = client.post("/transactions/", headers=headers_b, json={
        "account_id": user_a_acc_id,
        "txn_date": "2026-01-23",
        "narration_raw": "UNAUTHORIZED ACCOUNT ID",
        "debit_paise": 1000,
        "credit_paise": 0
    })
    assert res_b.status_code == 404
    assert "Account not found" in res_b.json()["detail"]


def test_9_statement_ownership_validation(client: TestClient):
    headers_a = get_auth_headers(client, "tx_user9_a@example.com")
    headers_b = get_auth_headers(client, "tx_user9_b@example.com")

    db_gen = client.app.dependency_overrides.get(get_db, get_db)()
    db = next(db_gen)
    user_a_res = client.get("/auth/me", headers=headers_a).json()
    user_a_id = uuid.UUID(user_a_res["id"])

    stmt_a = Statement(
        user_id=user_a_id,
        source_channel="upload"
    )
    db.add(stmt_a)
    db.commit()
    stmt_a_id = str(stmt_a.id)

    # User B attempts to create transaction referencing User A's statement_id
    res_b = client.post("/transactions/", headers=headers_b, json={
        "statement_id": stmt_a_id,
        "txn_date": "2026-01-24",
        "narration_raw": "UNAUTHORIZED STATEMENT ID",
        "debit_paise": 1000,
        "credit_paise": 0
    })
    assert res_b.status_code == 404
    assert "Statement not found" in res_b.json()["detail"]






def test_10_delete_clear_user_isolation(client: TestClient):
    headers_a = get_auth_headers(client, "tx_user10_a@example.com")
    headers_b = get_auth_headers(client, "tx_user10_b@example.com")

    tx_a = client.post("/transactions/", headers=headers_a, json={
        "txn_date": "2026-01-25",
        "narration_raw": "USER A KEEP TX",
        "debit_paise": 5000,
        "credit_paise": 0
    }).json()

    tx_b = client.post("/transactions/", headers=headers_b, json={
        "txn_date": "2026-01-25",
        "narration_raw": "USER B CLEAR TX",
        "debit_paise": 2000,
        "credit_paise": 0
    }).json()

    # User B clears transactions
    clear_res = client.delete("/transactions/clear", headers=headers_b)
    assert clear_res.status_code == 200

    # User B's transactions should be gone
    b_txns = client.get("/transactions", headers=headers_b).json()
    assert len(b_txns) == 0

    # User A's transactions MUST REMAIN
    a_txns = client.get("/transactions", headers=headers_a).json()
    assert len(a_txns) >= 1
    assert a_txns[0]["id"] == tx_a["id"]


def test_11_invalid_money_input_rejected(client: TestClient):
    headers = get_auth_headers(client, "tx_user11@example.com")

    # Negative debit paise
    res = client.post("/transactions/", headers=headers, json={
        "txn_date": "2026-01-26",
        "narration_raw": "NEGATIVE DEBIT",
        "debit_paise": -500,
        "credit_paise": 0
    })
    assert res.status_code in [400, 422]


def test_12_zero_debit_credit_rejected(client: TestClient):
    headers = get_auth_headers(client, "tx_user12@example.com")

    # Both zero debit and zero credit with no direction
    res = client.post("/transactions/", headers=headers, json={
        "txn_date": "2026-01-27",
        "narration_raw": "ZERO MONEY TX",
        "debit_paise": 0,
        "credit_paise": 0
    })
    assert res.status_code == 400
    assert "Invalid transaction money values" in res.json()["detail"]


def test_13_shared_schema_consistency(client: TestClient):
    headers = get_auth_headers(client, "tx_user13@example.com")

    created = client.post("/transactions/", headers=headers, json={
        "txn_date": "2026-01-28",
        "narration_raw": "SCHEMA CONSISTENCY TEST",
        "debit_paise": 150000,
        "credit_paise": 0,
        "balance_paise": 1000000
    }).json()

    list_res = client.get("/transactions", headers=headers).json()
    detail_res = client.get(f"/transactions/{created['id']}", headers=headers).json()

    # Both outputs share identical key structure
    assert set(list_res[0].keys()) == set(detail_res.keys())
