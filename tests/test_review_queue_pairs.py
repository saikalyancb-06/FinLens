"""Cross-Source Duplicates and BRS Match Candidates show transactions the way
Manual Review does (2026-10-07).

Both endpoints return `rows` in the Manual Review row shape (counterparty,
group_kind, group_channel, narration, txn_date, amount, direction), built by
the same `group_for`, and the BRS list covers the latest run of every account.
"""
import uuid
from datetime import date

import pytest

import app.database.session as dbm
from app.models.account import Account
from app.models.reconciliation import BookEntry, ImportBatch
from app.models.transaction import Direction, SourceType, Transaction
from app.services.deduplication_engine import DeduplicationEngine
from app.services.reconciliation_engine import ReconciliationMatchingEngine
from app.utils.security import decode_token

ROW_KEYS = {"transaction_id", "txn_date", "narration", "amount", "direction",
            "counterparty", "group_kind", "group_channel", "side"}


def _login(client):
    email = f"rq_{uuid.uuid4().hex[:8]}@kredo.in"
    client.post("/auth/register", json={"email": email, "password": "Secret123!", "full_name": "R",
                                        "entity_type": "BUSINESS"})
    tok = client.post("/auth/login", json={"email": email, "password": "Secret123!"}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}, uuid.UUID(decode_token(tok)["sub"])


def _txn(uid, acc, d, dr=0, cr=0, narr="", ref=None, src=SourceType.STATEMENT, bal=None, i=0):
    return Transaction(user_id=uid, account_id=acc, txn_date=d, value_date=d, debit_paise=dr or None,
                       credit_paise=cr or None, balance_paise=bal, narration_raw=narr, reference_no=ref,
                       direction=Direction.DEBIT if dr else Direction.CREDIT, source_type=src, row_index=i)


@pytest.fixture
def world(client):
    H, uid = _login(client)
    db = dbm.SessionLocal()
    try:
        accs = [Account(id=uuid.uuid4(), user_id=uid, bank_code="HDFC", account_number_masked=f"****000{n}")
                for n in (1, 2)]
        db.add_all(accs)
        db.flush()
        # A statement row and the email alert for (perhaps) the same payment.
        db.add_all([
            _txn(uid, accs[0].id, date(2026, 3, 3), dr=250000, narr="NEFT-HDFCN52026030311-KUMAR FISH",
                 ref="HDFCN52026030311", bal=99_000_00, i=1),
            _txn(uid, accs[0].id, date(2026, 3, 4), dr=250000, narr="Rs 2500 debited to KUMAR FISH",
                 ref="AXN778899001", src=SourceType.EMAIL_ALERT, i=2),
        ])
        db.commit()
        DeduplicationEngine(db, uid, accs[0].id).run_deduplication()

        # Each account gets a ledger and a BRS run with candidates for review.
        for acc in accs:
            batch = ImportBatch(id=uuid.uuid4(), user_id=uid, account_id=acc.id, filename="l.csv",
                                file_sha256="x", column_mapping_json={}, book_opening_paise=None)
            db.add(batch)
            db.flush()
            db.add_all([
                _txn(uid, acc.id, date(2026, 2, 28), cr=1, bal=1_000_000_00, narr="BAL", i=10),
                _txn(uid, acc.id, date(2026, 3, 6), dr=999000, bal=990_010_00,
                     narr="CHQ PAID 445566", ref="445566", i=11),
                _txn(uid, acc.id, date(2026, 3, 20), dr=500000, bal=985_010_00,
                     narr="NEFT-YESB0000001-SHARMA PROPERTIES", i=12),
            ])
            db.add_all([
                BookEntry(user_id=uid, account_id=acc.id, import_batch_id=batch.id, entry_date=date(2026, 3, 5),
                          money_out_paise=1000000, narration="Vendor A", party_name="VENDOR A",
                          instrument_no="445566", row_index=1, source_row_hash=uuid.uuid4().hex),
                BookEntry(user_id=uid, account_id=acc.id, import_batch_id=batch.id, entry_date=date(2026, 3, 10),
                          money_out_paise=500000, narration="Rent SHARMA PROPERTIES",
                          row_index=2, source_row_hash=uuid.uuid4().hex),
            ])
            db.commit()
            ReconciliationMatchingEngine(db, uid, acc.id, date(2026, 3, 1), date(2026, 3, 31)).execute_run(
                import_batch_id=batch.id, force=True, book_opening_paise=1_000_000_00)
        ids = [str(a.id) for a in accs]
    finally:
        db.close()
    return client, H, ids


def test_duplicate_candidates_carry_manual_review_rows(world):
    client, H, _ = world
    res = client.get("/v1/deduplication/matches?status_filter=pending_review", headers=H).json()
    assert len(res) == 1
    rows = res[0]["rows"]
    assert [r["role"] for r in rows] == ["duplicate", "kept"]
    assert all(ROW_KEYS <= set(r) for r in rows)
    kept = rows[1]
    assert (kept["source_type"], kept["direction"], kept["amount"]) == ("statement", "debit", 2500.0)
    assert kept["counterparty"] and "KUMAR" in kept["counterparty"].upper()


def test_brs_candidates_cover_every_account_with_both_sides(world):
    client, H, ids = world
    res = client.get("/v1/reconciliation/review-candidates", headers=H)
    assert res.status_code == 200, res.text
    out = res.json()
    assert {m["account_id"] for m in out} == set(ids)
    for m in out:
        sides = [r["side"] for r in m["rows"]]
        assert sides[0] == "books" and "bank" in sides
        assert all(ROW_KEYS <= set(r) for r in m["rows"])
    diff = [m for m in out if m["tier"] == "amount_difference"]
    assert diff and all(m["difference"] == -10.0 for m in diff)    # bank 9,990 - books 10,000
    book = diff[0]["rows"][0]
    assert (book["direction"], book["amount"], book["counterparty"]) == ("debit", 10000.0, "VENDOR A")


def test_run_matches_endpoint_returns_the_same_rows(world):
    client, H, _ = world
    cands = client.get("/v1/reconciliation/review-candidates", headers=H).json()
    run_id = cands[0]["run_id"]
    via_run = client.get(f"/v1/reconciliation/runs/{run_id}/matches?status=pending_review", headers=H).json()
    mine = sorted((m["match_id"], [r["transaction_id"] for r in m["rows"]]) for m in cands if m["run_id"] == run_id)
    theirs = sorted((m["match_id"], [r["transaction_id"] for r in m["rows"]]) for m in via_run)
    assert mine == theirs


def test_decision_removes_candidate(world):
    client, H, _ = world
    cands = client.get("/v1/reconciliation/review-candidates", headers=H).json()
    n = len(cands)
    r = client.post(f"/v1/reconciliation/matches/{cands[0]['match_id']}/confirm", headers=H)
    assert r.status_code == 200
    assert len(client.get("/v1/reconciliation/review-candidates", headers=H).json()) == n - 1


def test_brs_bank_side_is_the_statement_only(world):
    """An email/SMS alert is not the bank statement: a BRS built from it would
    count the same payment twice while the duplicate awaits review."""
    client, H, _ = world
    out = client.get("/v1/reconciliation/review-candidates", headers=H).json()
    assert all(r["source_type"] in ("statement", "ledger") for m in out for r in m["rows"])
