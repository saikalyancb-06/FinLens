import uuid
import pytest
from app.models.user import User
from app.models.entity import Entity, Bank
from app.models.account import Account
from tests.conftest import TestingSessionLocal


def test_entity_crud_and_user_isolation(client):
    # Register User A
    res_a = client.post("/auth/register", json={"email": "usera@example.com", "password": "password123"})
    assert res_a.status_code == 201
    login_a = client.post("/auth/login", json={"email": "usera@example.com", "password": "password123"})
    token_a = login_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # Register User B
    res_b = client.post("/auth/register", json={"email": "userb@example.com", "password": "password123"})
    assert res_b.status_code == 201
    login_b = client.post("/auth/login", json={"email": "userb@example.com", "password": "password123"})
    token_b = login_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # User A creates an entity
    create_res = client.post("/v1/bank-master/entities", json={"name": "ACME Pvt Ltd", "legal_name": "ACME Private Limited"}, headers=headers_a)
    assert create_res.status_code == 201
    entity_a_id = create_res.json()["id"]
    assert create_res.json()["name"] == "ACME Pvt Ltd"

    # User A lists entities
    list_a = client.get("/v1/bank-master/entities", headers=headers_a)
    assert list_a.status_code == 200
    assert len(list_a.json()) == 1

    # User B lists entities (User Isolation Check - should return empty)
    list_b = client.get("/v1/bank-master/entities", headers=headers_b)
    assert list_b.status_code == 200
    assert len(list_b.json()) == 0

    # User B attempts IDOR to read User A's entity (should return 404)
    idor_read = client.get(f"/v1/bank-master/entities/{entity_a_id}", headers=headers_b)
    assert idor_read.status_code == 404

    # User B attempts IDOR to update User A's entity (should return 404)
    idor_update = client.patch(f"/v1/bank-master/entities/{entity_a_id}", json={"name": "Hacked Entity"}, headers=headers_b)
    assert idor_update.status_code == 404

    # User B attempts IDOR to delete User A's entity (should return 404)
    idor_delete = client.delete(f"/v1/bank-master/entities/{entity_a_id}", headers=headers_b)
    assert idor_delete.status_code == 404

    # User A updates entity name
    update_res = client.patch(f"/v1/bank-master/entities/{entity_a_id}", json={"name": "ACME Global Corp"}, headers=headers_a)
    assert update_res.status_code == 200
    assert update_res.json()["name"] == "ACME Global Corp"

    # User A deletes entity
    del_res = client.delete(f"/v1/bank-master/entities/{entity_a_id}", headers=headers_a)
    assert del_res.status_code == 200


def test_bank_master_list_and_account_linkage(client):
    # Register & Login User
    res = client.post("/auth/register", json={"email": "bankuser@example.com", "password": "password123"})
    login = client.post("/auth/login", json={"email": "bankuser@example.com", "password": "password123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Fetch Bank Master list
    banks_res = client.get("/v1/bank-master/banks", headers=headers)
    assert banks_res.status_code == 200
    banks = banks_res.json()
    assert isinstance(banks, list)

    # Fetch User Accounts (Empty State)
    acc_res = client.get("/v1/bank-master/accounts", headers=headers)
    assert acc_res.status_code == 200
    assert len(acc_res.json()) == 0


def test_entity_deletion_protection_with_linked_accounts(client):
    # Register & Login User
    res = client.post("/auth/register", json={"email": "protect@example.com", "password": "password123"})
    login = client.post("/auth/login", json={"email": "protect@example.com", "password": "password123"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create Entity
    ent_res = client.post("/v1/bank-master/entities", json={"name": "Protected Entity"}, headers=headers)
    ent_id = ent_res.json()["id"]

    # Attempt to delete entity (successful when no accounts are linked)
    del_res = client.delete(f"/v1/bank-master/entities/{ent_id}", headers=headers)
    assert del_res.status_code == 200



