"""Tests for the categorisation review queue.

Before this endpoint existed there was no way to see an uncategorised
transaction, and no way to correct one: the /review-queue page showed only
deduplication and reconciliation matches, and the transactions API had no
category-update route at all.
"""

import uuid

import pytest

from app.categorization.dual_taxonomy import PURPOSES as BUSINESS_CATEGORIES
from app.categorization.taxonomy import UNCATEGORIZED


def _auth(client, email):
    from conftest import register_bank_account
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    token = client.post("/auth/login", json={"email": email, "password": "Password123!"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    account = register_bank_account(client, headers)
    return headers, account


def _make_transaction(client, headers, narration, category_id=None):
    """Insert an uncategorised transaction directly through the ORM."""
    import app.database.session as dbs
    from app.models.transaction import Direction, SourceType, Transaction
    from app.models.user import User

    db = dbs.SessionLocal()
    try:
        user_id = uuid.UUID(client.get("/auth/me", headers=headers).json()["id"])
        acct_id = uuid.UUID(client.get("/v1/bank-master/accounts", headers=headers).json()[0]["id"])
        tx = Transaction(
            id=uuid.uuid4(),
            user_id=user_id,
            account_id=acct_id,
            direction=Direction.DEBIT,
            debit_paise=45000,
            narration_raw=narration,
            narration_clean=narration,
            txn_date=__import__("datetime").date(2026, 3, 15),
            source_type=SourceType.STATEMENT,
            category_id=category_id,
        )
        db.add(tx)
        db.commit()
        return tx.id
    finally:
        db.close()


def test_categories_endpoint_returns_canonical_taxonomy(client):
    """The dropdown offers the BUSINESS taxonomy, not the legacy one.

    This endpoint used to return the personal-finance list (Groceries, Shopping,
    Entertainment). That list is still understood on input so historical rows
    keep resolving, but offering it here let a restaurant file a supplier
    payment under "Entertainment".
    """
    headers, _ = _auth(client, "rq_cats@example.com")
    res = client.get("/v1/review-queue/categories", headers=headers)
    assert res.status_code == 200
    assert res.json() == BUSINESS_CATEGORIES
    assert "Groceries" not in res.json()


def test_uncategorized_transaction_appears_in_queue(client):
    headers, _ = _auth(client, "rq_list@example.com")
    _make_transaction(client, headers, "ZZQX UNKNOWN MERCHANT 8891")

    res = client.get("/v1/review-queue", headers=headers)
    assert res.status_code == 200
    items = res.json()
    assert len(items) >= 1

    item = items[0]
    assert item["current_category"] == UNCATEGORIZED
    assert item["narration"]
    assert "suggestions" in item


def test_queue_summary_counts_uncategorized(client):
    headers, _ = _auth(client, "rq_summary@example.com")
    _make_transaction(client, headers, "ANOTHER UNKNOWN THING")

    summary = client.get("/v1/review-queue/summary", headers=headers).json()
    assert summary["total_requiring_review"] >= 1
    assert summary["uncategorized"] >= 1
    assert summary["available_categories"] == BUSINESS_CATEGORIES
    # Same list under the old field name, so an existing client keeps working.
    assert summary["available_purposes"] == BUSINESS_CATEGORIES


def test_manual_reclassification_round_trip(client):
    headers, _ = _auth(client, "rq_patch@example.com")
    tx_id = _make_transaction(client, headers, "MYSTERY VENDOR PAYMENT")

    res = client.patch(f"/v1/review-queue/{tx_id}", headers=headers,
                       json={"category": "Groceries", "note": "corrected by finance"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["new_category"] == "Groceries"
    assert body["classification_method"] == "manual"

    # It must leave the queue once resolved.
    remaining = [i["transaction_id"] for i in client.get("/v1/review-queue", headers=headers).json()]
    assert str(tx_id) not in remaining


def test_reclassification_rejects_invalid_category(client):
    headers, _ = _auth(client, "rq_invalid@example.com")
    tx_id = _make_transaction(client, headers, "SOMETHING ELSE")

    res = client.patch(f"/v1/review-queue/{tx_id}", headers=headers,
                       json={"category": "Not A Real Category"})
    assert res.status_code == 400
    # `category` is the field name the app uses everywhere a user can see it,
    # so the error has to name it the same way.
    assert "Invalid category" in res.json()["detail"]


def test_reclassification_accepts_legacy_alias(client):
    """Aliases normalise onto canonical names rather than creating variants."""
    headers, _ = _auth(client, "rq_alias@example.com")
    tx_id = _make_transaction(client, headers, "ALIAS TEST TXN")

    res = client.patch(f"/v1/review-queue/{tx_id}", headers=headers, json={"category": "food"})
    assert res.status_code == 200
    assert res.json()["new_category"] == "Food & Dining"


def test_queue_is_scoped_to_the_owning_user(client):
    """A reviewer must never see or edit another user's transactions."""
    headers_a, _ = _auth(client, "rq_owner@example.com")
    headers_b, _ = _auth(client, "rq_other@example.com")
    tx_id = _make_transaction(client, headers_a, "OWNER PRIVATE TXN")

    other_items = [i["transaction_id"] for i in client.get("/v1/review-queue", headers=headers_b).json()]
    assert str(tx_id) not in other_items

    res = client.patch(f"/v1/review-queue/{tx_id}", headers=headers_b, json={"category": "Shopping"})
    assert res.status_code == 404


def test_review_queue_requires_authentication(client):
    assert client.get("/v1/review-queue").status_code == 401
    assert client.get("/v1/review-queue/summary").status_code == 401


def test_ingested_statement_populates_review_queue(client, tmp_path):
    """A real upload must route unclassifiable rows into the review queue.

    This is the integration the whole feature depends on: the classifier can be
    perfect and the UI can be perfect, but if `store_transactions` does not write
    `requires_review` and the provenance fields, the queue stays empty on real
    data and only demo-seeded rows ever appear.
    """
    import os
    import uuid as _uuid
    from app.models.uploaded_file import UploadedFile
    from app.services.parsing_queue import process_file_parsing_task
    import app.database.session as dbs

    headers, _ = _auth(client, "rq_ingest@example.com")

    csv_path = os.path.join(tmp_path, "stmt.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("Date,Description,Debit,Credit,Balance\n")
        # One obviously classifiable row, one that no rule covers and whose
        # narration the model has never seen.
        f.write("2026-05-01,UPI-SWIGGY ORDER-8821,430.00,0.00,50000.00\n")
        f.write("2026-05-02,UPI-SRI LAKSHMI TRADERS-8871,2450.00,0.00,47550.00\n")

    with open(csv_path, "rb") as fh:
        res = client.post("/files/upload", files={"file": ("stmt.csv", fh, "text/csv")}, headers=headers)
    assert res.status_code == 202, res.text
    file_id = _uuid.UUID(res.json()["file_id"])

    db = dbs.SessionLocal()
    try:
        stored = db.query(UploadedFile).filter(UploadedFile.id == file_id).first()
        path = stored.file_path
    finally:
        db.close()

    user_id = client.get("/auth/me", headers=headers).json()["id"]
    summary = process_file_parsing_task(file_id=file_id, file_path=path, user_id=user_id)
    assert summary["status"] == "COMPLETED", summary

    queue = client.get("/v1/review-queue", headers=headers).json()
    narrations = [i["narration"] for i in queue]

    assert any("LAKSHMI" in n for n in narrations), (
        f"Unclassifiable row did not reach the review queue. Queue held: {narrations}"
    )
    assert not any("SWIGGY" in n for n in narrations), (
        "A confidently-classified row was sent to review"
    )

    item = next(i for i in queue if "LAKSHMI" in i["narration"])
    assert item["current_category"] == UNCATEGORIZED

    # The provenance explanation is asserted on the Prediction row, not on the
    # queue response. The invariant this test cares about is that INGESTION
    # recorded why it could not decide — that is a property of what was written
    # to the database, and it stays true regardless of which fields the review
    # screen happens to render. It used to be checked through the API's
    # `explanation` field, which meant a UI change could break a test about
    # ingestion.
    from app.models.prediction import Prediction
    from app.models.transaction import Transaction

    db = dbs.SessionLocal()
    try:
        txn = (db.query(Transaction)
                 .filter(Transaction.id == _uuid.UUID(item["transaction_id"]))
                 .first())
        assert txn is not None
        pred = (db.query(Prediction)
                  .filter(Prediction.transaction_id == txn.id)
                  .first())
        assert pred is not None, "ingestion wrote no Prediction row"
        assert pred.explanation, "provenance explanation was not persisted by ingestion"
    finally:
        db.close()
