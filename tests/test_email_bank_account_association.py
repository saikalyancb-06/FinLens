import uuid
import datetime
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from main import app
from app.database.session import get_db
from app.models.user import User
from app.models.account import Account
from app.models.entity import Entity, Bank
from app.models.transaction import Transaction, Direction
from app.models.statement import Statement
from app.email.models import ConnectedAccount, EmailAttachment, ImportHistory
from app.models.oauth_state import OAuthState
from app.services.parsing_queue import process_file_parsing_task
from tests.conftest import test_engine, Base

client = TestClient(app)


@pytest.fixture(autouse=True)
def init_test_db():
    Base.metadata.create_all(bind=test_engine)
    yield


def register_and_login_user(client: TestClient, prefix: str):
    email = f"{prefix}_{uuid.uuid4().hex[:6]}@example.com"
    password = "StrongPassword123!"
    reg_res = client.post("/auth/register", json={"email": email, "password": password})
    assert reg_res.status_code == 201
    user_id_str = reg_res.json()["id"]

    login_res = client.post("/auth/login", json={"email": email, "password": password})
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, user_id_str


def connect_user_gmail(client: TestClient, headers: dict):
    res_conn = client.post("/email/connect", headers=headers)
    assert res_conn.status_code == 200
    import urllib.parse
    state_param = urllib.parse.parse_qs(urllib.parse.urlparse(res_conn.json()["authorization_url"]).query)["state"][0]
    res_cb = client.get(f"/email/oauth/callback?code=demo_code_{uuid.uuid4().hex[:6]}&state={state_param}", headers=headers)
    assert res_cb.status_code == 200


def create_user_bank_account(client: TestClient, headers: dict, bank_code="HDFC", account_num="50200012345678"):
    res = client.post(
        "/v1/bank-master/accounts",
        headers=headers,
        json={
            "bank_code": bank_code,
            "account_number": account_num,
            "account_type": "CURRENT",
            "currency": "INR"
        }
    )
    assert res.status_code == 201
    return res.json()


class TestEmailBankAccountAssociation:
    """Complete test suite verifying mandatory bank account selection for email transaction imports."""

    def test_1_gmail_connected_and_bank_account_selected_import_succeeds(self, client):
        """1. Gmail connected + bank account selected -> import succeeds."""
        headers, user_id = register_and_login_user(client, "user_t1")
        acc = create_user_bank_account(client, headers, "HDFC", "50200011112222")
        connect_user_gmail(client, headers)

        # Scan mailbox
        res_scan = client.post("/email/scan", headers=headers)
        assert res_scan.status_code == 200
        stmts = client.get("/email/statements", headers=headers).json()
        target_stmt = next(s for s in stmts if s.get("attachment_type") != "ALERT")

        # Import with selected bank account
        res_imp = client.post(
            f"/email/import/{target_stmt['id']}",
            headers=headers,
            json={"bank_account_id": acc["id"]}
        )
        assert res_imp.status_code == 200
        data = res_imp.json()
        assert data["status"] == "success"
        assert data["bank_account_id"] == acc["id"]
        assert data["transactions_created"] >= 1

    def test_2_gmail_connected_no_bank_account_selected_import_blocked(self, client):
        """2. Gmail connected + no bank account selected -> import blocked."""
        headers, user_id = register_and_login_user(client, "user_t2")
        connect_user_gmail(client, headers)

        res_scan = client.post("/email/scan", headers=headers)
        assert res_scan.status_code == 200
        stmts = client.get("/email/statements", headers=headers).json()
        target_stmt = next(s for s in stmts if s.get("attachment_type") != "ALERT")

        # Attempt import with empty payload
        res_imp = client.post(f"/email/import/{target_stmt['id']}", headers=headers, json={})
        assert res_imp.status_code == 400
        assert "required" in res_imp.json()["detail"].lower()

    def test_3_api_called_without_bank_account_id_rejected(self, client):
        """3. API called without bank_account_id -> rejected."""
        headers, user_id = register_and_login_user(client, "user_t3")
        connect_user_gmail(client, headers)
        client.post("/email/scan", headers=headers)
        stmts = client.get("/email/statements", headers=headers).json()
        target_stmt = next(s for s in stmts if s.get("attachment_type") != "ALERT")

        # None / omitted payload
        res_imp = client.post(f"/email/import/{target_stmt['id']}", headers=headers)
        assert res_imp.status_code == 400
        assert "required" in res_imp.json()["detail"].lower()

    def test_4_invalid_bank_account_id_rejected(self, client):
        """4. Invalid bank_account_id -> rejected."""
        headers, user_id = register_and_login_user(client, "user_t4")
        connect_user_gmail(client, headers)
        client.post("/email/scan", headers=headers)
        stmts = client.get("/email/statements", headers=headers).json()
        target_stmt = next(s for s in stmts if s.get("attachment_type") != "ALERT")

        # 4a. Malformed UUID
        res_bad_uuid = client.post(
            f"/email/import/{target_stmt['id']}",
            headers=headers,
            json={"bank_account_id": "not-a-uuid"}
        )
        assert res_bad_uuid.status_code == 400

        # 4b. Non-existent random UUID
        random_uuid = str(uuid.uuid4())
        res_not_found = client.post(
            f"/email/import/{target_stmt['id']}",
            headers=headers,
            json={"bank_account_id": random_uuid}
        )
        assert res_not_found.status_code == 404

    def test_5_bank_account_belonging_to_another_user_rejected(self, client):
        """5. Bank account belonging to another user -> rejected (Anti-IDOR)."""
        headers_a, user_a_id = register_and_login_user(client, "user_a_sec")
        acc_a = create_user_bank_account(client, headers_a, "HDFC", "50200099990001")

        headers_b, user_b_id = register_and_login_user(client, "user_b_sec")
        connect_user_gmail(client, headers_b)
        client.post("/email/scan", headers=headers_b)
        stmts_b = client.get("/email/statements", headers=headers_b).json()
        target_stmt_b = next(s for s in stmts_b if s.get("attachment_type") != "ALERT")

        # User B attempts to import into User A's account
        res_idor = client.post(
            f"/email/import/{target_stmt_b['id']}",
            headers=headers_b,
            json={"bank_account_id": acc_a["id"]}
        )
        assert res_idor.status_code == 403
        assert "belongs to another user" in res_idor.json()["detail"].lower()

    def test_6_valid_bank_account_belonging_to_current_user_succeeds(self, client):
        """6. Valid bank account belonging to current user -> import succeeds."""
        headers, user_id = register_and_login_user(client, "user_t6")
        acc = create_user_bank_account(client, headers, "ICICI", "112233445566")
        connect_user_gmail(client, headers)
        client.post("/email/scan", headers=headers)
        stmts = client.get("/email/statements", headers=headers).json()
        target_stmt = next(s for s in stmts if s.get("attachment_type") != "ALERT")

        res_imp = client.post(
            f"/email/import/{target_stmt['id']}",
            headers=headers,
            json={"bank_account_id": acc["id"]}
        )
        assert res_imp.status_code == 200
        assert res_imp.json()["status"] == "success"

    def test_7_imported_transactions_contain_correct_bank_account_id(self, client):
        """7. Imported transactions contain the correct bank_account_id."""
        headers, user_id = register_and_login_user(client, "user_t7")
        acc = create_user_bank_account(client, headers, "SBI", "204988887777")
        connect_user_gmail(client, headers)
        client.post("/email/scan", headers=headers)
        stmts = client.get("/email/statements", headers=headers).json()
        target_stmt = next(s for s in stmts if s.get("attachment_type") != "ALERT")

        res_imp = client.post(
            f"/email/import/{target_stmt['id']}",
            headers=headers,
            json={"bank_account_id": acc["id"]}
        )
        assert res_imp.status_code == 200

        # Query transactions via API
        res_txs = client.get("/transactions", headers=headers)
        assert res_txs.status_code == 200
        txs_data = res_txs.json()
        assert len(txs_data) >= 1

        db: Session = next(get_db())
        txns = db.query(Transaction).filter(Transaction.user_id == uuid.UUID(user_id)).all()
        assert len(txns) >= 1
        for t in txns:
            assert str(t.account_id) == str(acc["id"])
            assert t.source_channel == "GMAIL"

        # Verify Statement record
        stmt_db = db.query(Statement).filter(Statement.user_id == uuid.UUID(user_id)).first()
        assert stmt_db is not None
        assert str(stmt_db.account_id) == str(acc["id"])

        # Verify ImportHistory record
        hist_db = db.query(ImportHistory).filter(ImportHistory.user_id == uuid.UUID(user_id)).first()
        assert hist_db is not None
        assert str(hist_db.account_id) == str(acc["id"])
        assert hist_db.status == "SUCCESS"

    def test_8_multiple_imports_from_same_gmail_can_target_different_bank_accounts(self, client):
        """8. Multiple imports from the same Gmail account can target different bank accounts."""
        headers, user_id = register_and_login_user(client, "user_t8")
        acc1 = create_user_bank_account(client, headers, "HDFC", "50200088881111")
        acc2 = create_user_bank_account(client, headers, "SBI", "304900002222")

        connect_user_gmail(client, headers)
        client.post("/email/scan", headers=headers)
        stmts = client.get("/email/statements", headers=headers).json()
        real_stmts = [s for s in stmts if s.get("attachment_type") != "ALERT"]
        assert len(real_stmts) >= 2

        # Import 1 into Account 1
        res1 = client.post(
            f"/email/import/{real_stmts[0]['id']}",
            headers=headers,
            json={"bank_account_id": acc1["id"]}
        )
        assert res1.status_code == 200

        # Import 2 into Account 2
        res2 = client.post(
            f"/email/import/{real_stmts[1]['id']}",
            headers=headers,
            json={"bank_account_id": acc2["id"]}
        )
        assert res2.status_code == 200

        db: Session = next(get_db())
        txs_acc1 = db.query(Transaction).filter(Transaction.account_id == uuid.UUID(acc1["id"])).all()
        txs_acc2 = db.query(Transaction).filter(Transaction.account_id == uuid.UUID(acc2["id"])).all()
        assert len(txs_acc1) >= 1
        assert len(txs_acc2) >= 1

    def test_9_existing_non_email_transaction_imports_continue_working(self, client):
        """9. Existing non-email transaction imports continue working."""
        headers, user_id = register_and_login_user(client, "user_t9")
        acc = create_user_bank_account(client, headers, "AXIS", "91200033334444")

        # Direct transaction creation
        res_txn = client.post(
            "/transactions/",
            headers=headers,
            json={
                "narration_raw": "Manual direct transaction",
                "credit_paise": 45000,
                "txn_date": "2026-08-10"
            }
        )
        assert res_txn.status_code == 201

    def test_10_existing_historical_data_remains_intact(self, client):
        """10. Existing historical data remains intact."""
        headers, user_id = register_and_login_user(client, "user_t10")
        u_uuid = uuid.UUID(user_id)

        # Create historical transaction without account_id
        db: Session = next(get_db())
        hist_tx = Transaction(
            user_id=u_uuid,
            account_id=None,
            txn_date=datetime.date(2025, 1, 1),
            narration_raw="Historical legacy transaction",
            credit_paise=100000,
            direction=Direction.CREDIT
        )
        db.add(hist_tx)
        db.commit()

        # Query transactions via API
        res = client.get("/transactions", headers=headers)
        assert res.status_code == 200
        txs = res.json()
        assert any(t["description"] == "Historical legacy transaction" or t.get("narration_raw") == "Historical legacy transaction" for t in txs)

    def test_11_async_background_imports_preserve_selected_bank_account_id(self, client):
        """11. Asynchronous/background imports preserve the selected bank_account_id."""
        headers, user_id = register_and_login_user(client, "user_t11")
        acc = create_user_bank_account(client, headers, "KOTAK", "71200055556666")
        connect_user_gmail(client, headers)
        client.post("/email/scan", headers=headers)

        # Import All with bank_account_id payload
        res_all = client.post(
            "/email/import-all",
            headers=headers,
            json={"bank_account_id": acc["id"]}
        )
        assert res_all.status_code == 200
        data = res_all.json()
        assert data["status"] == "success"

        db: Session = next(get_db())
        txns = db.query(Transaction).filter(Transaction.user_id == uuid.UUID(user_id)).all()
        assert len(txns) >= 1
        for t in txns:
            assert str(t.account_id) == str(acc["id"])

    def test_12_map_account_endpoint_validates_ownership(self, client):
        """12. POST /email/statements/{id}/map-account strictly validates user ownership."""
        headers_a, user_a_id = register_and_login_user(client, "user_map_a")
        acc_a = create_user_bank_account(client, headers_a, "HDFC", "50200077778888")

        headers_b, user_b_id = register_and_login_user(client, "user_map_b")
        connect_user_gmail(client, headers_b)
        client.post("/email/scan", headers=headers_b)
        stmts_b = client.get("/email/statements", headers=headers_b).json()
        target_stmt_b = next(s for s in stmts_b if s.get("attachment_type") != "ALERT")

        # User B maps to User A's account -> 403 Forbidden
        res_map_fail = client.post(
            f"/email/statements/{target_stmt_b['id']}/map-account",
            headers=headers_b,
            json={"bank_account_id": acc_a["id"]}
        )
        assert res_map_fail.status_code == 403

        # User B creates own account and maps -> 200 OK
        acc_b = create_user_bank_account(client, headers_b, "ICICI", "60200077778888")
        res_map_ok = client.post(
            f"/email/statements/{target_stmt_b['id']}/map-account",
            headers=headers_b,
            json={"bank_account_id": acc_b["id"]}
        )
        assert res_map_ok.status_code == 200
        assert res_map_ok.json()["bank_account_id"] == acc_b["id"]
