"""Every screen narrowed by the filter bar must describe the same rows.

Audit of 2026-10-07. Two entities, two accounts, three months of rows; every
endpoint that takes the entity / account / date filters is asked the same
question and must give the same answer.
"""
import uuid
from datetime import date

import pytest

import app.database.session as dbm
from app.models.account import Account
from app.models.compliance import AnomalyFinding
from app.models.entity import Bank, Entity
from app.models.transaction import Direction, SourceType, Transaction
from app.models.user import User
from app.utils.security import decode_token


def _login(client, email):
    client.post("/auth/register", json={"email": email, "password": "Secret123!", "full_name": "F",
                                        "entity_type": "BUSINESS"})
    tok = client.post("/auth/login", json={"email": email, "password": "Secret123!"}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}, uuid.UUID(decode_token(tok)["sub"])


@pytest.fixture
def world(client):
    H, uid = _login(client, f"flt_{uuid.uuid4().hex[:8]}@kredo.in")
    db = dbm.SessionLocal()
    try:
        bank = db.query(Bank).filter(Bank.code == "FLTB").first() or Bank(id=uuid.uuid4(), code="FLTB", name="Filter Bank")
        db.merge(bank)
        e1 = Entity(id=uuid.uuid4(), user_id=uid, name="E-One")
        e2 = Entity(id=uuid.uuid4(), user_id=uid, name="E-Two")
        db.add_all([e1, e2]); db.flush()
        a1 = Account(id=uuid.uuid4(), user_id=uid, entity_id=e1.id, bank_id=bank.id, bank_code="FLTB",
                     account_number_masked="****0001", currency="INR")
        a2 = Account(id=uuid.uuid4(), user_id=uid, entity_id=e2.id, bank_id=bank.id, bank_code="FLTB",
                     account_number_masked="****0002", currency="INR")
        db.add_all([a1, a2]); db.flush()
        rows = []
        for acc, base in ((a1, 1_000_000), (a2, 500_000)):
            bal = base
            for i, (d, dr, cr, cat) in enumerate([
                (date(2026, 1, 5), 0, 200_000, "Income"), (date(2026, 1, 20), 50_000, 0, "Food & Dining"),
                (date(2026, 2, 3), 75_000, 0, "Transfers"), (date(2026, 2, 3), 75_000, 0, "Transfers"),
                (date(2026, 3, 10), 0, 30_000, "Income"), (date(2026, 3, 31), 12_345, 0, "Financial"),
            ]):
                bal += cr - dr
                rows.append(Transaction(
                    user_id=uid, account_id=acc.id, txn_date=d, value_date=d, debit_paise=dr or None,
                    credit_paise=cr or None, balance_paise=bal, row_index=i, narration_raw=f"ROW {i}",
                    category=cat, legacy_category="Flat " + cat,
                    direction=Direction.DEBIT if dr else Direction.CREDIT, source_type=SourceType.STATEMENT))
        # A row whose stored entity copy is stale: its account now belongs to E2.
        rows[-1].entity_id = e1.id
        db.add_all(rows)
        db.add_all([
            AnomalyFinding(user_id=uid, account_id=a1.id, anomaly_type="large_txn", severity="high", status="open",
                           title="Jan finding", occurred_on=date(2026, 1, 20), amount_paise=50_000, fingerprint=uuid.uuid4().hex),
            AnomalyFinding(user_id=uid, account_id=a1.id, anomaly_type="large_txn", severity="high", status="open",
                           title="Mar finding", occurred_on=date(2026, 3, 31), amount_paise=12_345, fingerprint=uuid.uuid4().hex),
        ])
        db.commit()
        ids = dict(e1=str(e1.id), e2=str(e2.id), a1=str(a1.id), a2=str(a2.id))
    finally:
        db.close()
    return client, H, ids


def _get(client, H, url, **params):
    r = client.get(url, params={k: v for k, v in params.items() if v}, headers=H)
    assert r.status_code == 200, (url, r.text)
    return r


SCOPES = [
    {}, {"ent": "e1"}, {"ent": "e2"}, {"acc": "a1"},
    {"f": "2026-02-01", "t": "2026-02-28"}, {"ent": "e2", "f": "2026-01-01", "t": "2026-02-28"},
]


@pytest.mark.parametrize("scope", SCOPES)
def test_all_panels_describe_the_same_rows(world, scope):
    client, H, ids = world
    ent, acc = ids.get(scope.get("ent")), ids.get(scope.get("acc"))
    f, t = scope.get("f"), scope.get("t")

    r = _get(client, H, "/transactions", limit=1000, entity_id=ent, bank_id=acc, start_date=f, end_date=t)
    rows = r.json()
    n, cr, dr = len(rows), round(sum(x["credit"] for x in rows), 2), round(sum(x["debit"] for x in rows), 2)
    assert int(r.headers["x-total-count"]) == n

    s = _get(client, H, "/dashboard/summary", from_date=f, to_date=t, entity_id=ent, bank_id=acc).json()
    assert (s["total_transactions"], s["total_credit"], s["total_debit"]) == (n, cr, dr)
    assert s["net_movement"] == round(cr - dr, 2)          # "this period", not all time

    cf = _get(client, H, "/analytics/cash-flow", interval="daily", start_date=f, end_date=t,
              entity_id=ent, bank_id=acc).json()
    assert round(sum(x["inflow"] for x in cf), 2) == cr and round(sum(x["outflow"] for x in cf), 2) == dr

    tr = _get(client, H, "/reports/treasury", period="custom" if (f or t) else "all_time",
              start_date=f, end_date=t, entity_id=ent, account_id=acc).json()
    assert (tr["group_totals"]["total_inflows"], tr["group_totals"]["total_outflows"]) == (cr, dr)

    dd = _get(client, H, "/v1/categories/drilldown", account_id=acc, entity_id=ent, date_from=f, date_to=t).json()
    assert sum(c["transaction_count"] for c in dd["children"]) == n

    # Closing position is as at the end of the period, on every panel.
    cp = _get(client, H, "/analytics/cash-position", start_date=f, end_date=t, entity_id=ent, bank_id=acc).json()
    if cp.get("points"):
        assert s["consolidated_liquidity"] == cp["points"][-1]["balance"]


def test_entities_partition_the_whole(world):
    client, H, ids = world
    whole = _get(client, H, "/dashboard/summary").json()
    parts = [_get(client, H, "/dashboard/summary", entity_id=ids[e]).json() for e in ("e1", "e2")]
    # The stale-entity row is counted once, under its account's entity.
    assert whole["total_transactions"] == sum(p["total_transactions"] for p in parts)
    assert whole["total_debit"] == round(sum(p["total_debit"] for p in parts), 2)
    assert whole["consolidated_liquidity"] == round(sum(p["consolidated_liquidity"] for p in parts), 2)


def test_treasury_account_filter_excludes_other_accounts(world):
    client, H, ids = world
    one = _get(client, H, "/reports/treasury", period="all_time", account_id=ids["a1"]).json()
    tx = _get(client, H, "/transactions", limit=1000, bank_id=ids["a1"]).json()
    assert one["group_totals"]["total_outflows"] == round(sum(x["debit"] for x in tx), 2)
    assert sum(m["transaction_count"] for m in one["monthly_trend"]) == len(tx)
    assert [e["entity_name"] for e in one["entities"]] == ["E-One"]


def test_findings_header_and_list_agree_under_dates(world):
    client, H, ids = world
    counts = []
    for f, t in ((None, None), ("2026-03-01", "2026-03-31")):
        s = _get(client, H, "/dashboard/summary", from_date=f, to_date=t).json()
        lst = _get(client, H, "/compliance/anomalies", status="open", start_date=f, end_date=t).json()
        ov = _get(client, H, "/compliance/overview", start_date=f, end_date=t).json()
        assert s["anomalies"] == len(lst) == ov["anomalies"]["open"]
        assert all(a["occurred_on"] is None or (not f or a["occurred_on"] >= f) for a in lst)
        counts.append(len(lst))
    # (the scan owns which findings exist; what matters is that all three agree)
    assert counts[0] >= counts[1]


def test_category_filter_matches_the_column(world):
    client, H, ids = world
    options = _get(client, H, "/analytics/filters").json()["categories"]
    rows = _get(client, H, "/transactions", limit=1000).json()
    shown = {x["category"] or x["purpose"] or x["final_category"] for x in rows}
    assert set(options) == shown
    for cat in options:
        picked = _get(client, H, "/transactions", limit=1000, category=cat).json()
        assert picked and all((x["category"] or x["purpose"] or x["final_category"]) == cat for x in picked)
