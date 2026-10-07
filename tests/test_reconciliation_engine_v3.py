"""Reconciliation engine v3 — the rules agreed for the BRS (2026-10-06).

Each test is one rule, written as the accountant would state it:

* book opening comes from the user, the previous contiguous run, or the
  ledger file's opening row — never from the bank balance;
* outstanding items carry forward until they clear;
* only transaction IDs (cheque/instrument no, UTR, voucher no) auto-match, within
  7 days, or 90 days on a cheque number; shared words only suggest;
* identical ledger rows are both kept; an extra one is a possible duplicate;
* an amount difference on a paired entry is its own BRS line;
* group matches both ways, always for review, and always fast;
* reviewer decisions rebuild the run and survive a re-run.
"""
import time
import uuid
from datetime import date

import pytest

from app.database.session import Base
from app.models.account import Account
from app.models.reconciliation import (
    BookEntry, ImportBatch, MatchStatusEnum, ReconciliationItem, ReconciliationMatch,
    RunVerdictEnum,
)
from app.models.transaction import Direction, SourceType, Transaction
from app.models.user import User
from app.services.reconciliation_engine import (
    BookOpeningRequired, ReconciliationMatchingEngine, rebuild_run, strong_ids,
)
from tests.pgtestdb import make_isolated_engine


@pytest.fixture
def db():
    engine, Session = make_isolated_engine("recon_v3")
    Base.metadata.create_all(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


class World:
    def __init__(self, db):
        self.db = db
        self.user = User(id=uuid.uuid4(), email=f"v3_{uuid.uuid4().hex[:8]}@kredo.in", hashed_password="h")
        self.acct = Account(id=uuid.uuid4(), user_id=self.user.id, bank_code="HDFC", account_number_masked="****0001")
        db.add_all([self.user, self.acct])
        db.commit()
        self.batch = self.new_batch()
        self._row = 0

    def new_batch(self, opening=None, source=None, period_from=None):
        b = ImportBatch(id=uuid.uuid4(), user_id=self.user.id, account_id=self.acct.id, filename="l.csv",
                        file_sha256="x", column_mapping_json={}, book_opening_paise=opening,
                        book_opening_source=source, period_from=period_from)
        self.db.add(b)
        self.db.commit()
        return b

    def book(self, d, inn=0, out=0, narr="", inst=None, voucher=None, batch=None):
        self._row += 1
        e = BookEntry(user_id=self.user.id, account_id=self.acct.id, import_batch_id=(batch or self.batch).id,
                      entry_date=d, money_in_paise=inn, money_out_paise=out, narration=narr,
                      instrument_no=inst, voucher_no=voucher, row_index=self._row,
                      source_row_hash=uuid.uuid4().hex)
        self.db.add(e)
        self.db.commit()
        return e

    def bank(self, d, dr=0, cr=0, bal=None, narr="", ref=None):
        self._row += 1
        t = Transaction(user_id=self.user.id, account_id=self.acct.id, txn_date=d, value_date=d,
                        debit_paise=dr, credit_paise=cr, balance_paise=bal, narration_raw=narr,
                        reference_no=ref, direction=Direction.DEBIT if dr else Direction.CREDIT,
                        source_type=SourceType.STATEMENT, row_index=self._row)
        self.db.add(t)
        self.db.commit()
        return t

    def run(self, frm, to, opening=None, batch=None):
        eng = ReconciliationMatchingEngine(self.db, self.user.id, self.acct.id, frm, to)
        return eng.execute_run(import_batch_id=(batch or self.batch).id, force=True, book_opening_paise=opening)

    def items(self, run, **filters):
        q = self.db.query(ReconciliationItem).filter(ReconciliationItem.run_id == run.id)
        return [i for i in q.all() if all(getattr(i, k) == v for k, v in filters.items())]

    def matches(self, run, **filters):
        q = self.db.query(ReconciliationMatch).filter(ReconciliationMatch.run_id == run.id)
        return [m for m in q.all() if all(getattr(m, k) == v for k, v in filters.items())]


# --------------------------------------------------------------------------- IDs

def test_strong_ids_ignore_words_dates_and_the_amount():
    assert strong_ids(["RENT JANUARY SHARMA PROPERTIES"]) == set()
    assert strong_ids(["NEFT-YESCB50870093682-RESILIENT"]) == {"YESCB50870093682"}
    assert strong_ids(["CHQ PAID 000123"]) == {"123"}
    assert strong_ids(["SALARY 15032026"]) == set()            # a date
    assert strong_ids(["PAYMENT 125000"], amount_paise=12500000) == set()   # its own amount
    assert strong_ids(["UPI/508966282233/04:20:46"]) == {"508966282233"}


# ----------------------------------------------------------------- opening balance

def test_opening_is_never_taken_from_the_bank(db):
    w = World(db)
    w.bank(date(2026, 2, 28), cr=1, bal=500000, narr="BAL")
    w.book(date(2026, 3, 5), out=10000, narr="X")
    with pytest.raises(BookOpeningRequired):
        w.run(date(2026, 3, 1), date(2026, 3, 31))


def test_opening_from_previous_contiguous_run_and_carry_forward(db):
    """Feb: cheque 000123 written 27-Feb, not yet in bank. Mar: it clears on
    2-Mar. March must open at Feb's book closing, carry the cheque, match it,
    and reconcile clean — the case v2 reported as a -10,000 residual."""
    w = World(db)
    w.bank(date(2026, 1, 31), cr=1, bal=10000000, narr="BAL")              # 1,00,000.00
    chq = w.book(date(2026, 2, 27), out=1000000, narr="VENDOR", inst="000123")
    feb = w.run(date(2026, 2, 1), date(2026, 2, 28), opening=10000000)
    assert feb.residual_paise == 0
    assert [i.book_entry_id for i in w.items(feb)] == [chq.id]

    w.bank(date(2026, 3, 2), dr=1000000, bal=9000000, narr="CHQ PAID 000123", ref="000123")
    w.book(date(2026, 3, 15), inn=2000000, narr="CUSTOMER RECEIPT")
    w.bank(date(2026, 3, 15), cr=2000000, bal=11000000, narr="NEFT CUSTOMER")
    mar = w.run(date(2026, 3, 1), date(2026, 3, 31))

    assert mar.book_opening_source == "previous_run"
    assert mar.book_opening_paise == feb.book_closing_paise == 9000000
    assert mar.carried_from_run_id == feb.id
    assert mar.residual_paise == 0
    assert mar.verdict == RunVerdictEnum.RECONCILED_CLEAN.value
    assert mar.matched_count == 2


def test_gap_between_runs_requires_opening(db):
    w = World(db)
    w.bank(date(2026, 1, 31), cr=1, bal=0, narr="BAL")
    w.book(date(2026, 1, 10), out=100, narr="A")
    w.run(date(2026, 1, 1), date(2026, 1, 31), opening=0)
    w.book(date(2026, 3, 10), out=100, narr="B")
    with pytest.raises(BookOpeningRequired, match="ended on 2026-01-31"):
        w.run(date(2026, 3, 1), date(2026, 3, 31))


def test_ledger_file_opening_rolls_forward_to_the_period(db):
    w = World(db)
    b = w.new_batch(opening=500000, source="ledger_file", period_from=date(2026, 3, 1))
    w.book(date(2026, 3, 10), inn=100000, narr="EARLY MARCH", batch=b)        # before the period
    w.book(date(2026, 4, 5), out=50000, narr="APRIL", batch=b)
    w.bank(date(2026, 4, 30), cr=1, bal=550000, narr="BAL")
    run = w.run(date(2026, 4, 1), date(2026, 4, 30), batch=b)
    assert run.book_opening_source == "ledger_file"
    assert run.book_opening_paise == 600000
    assert run.book_closing_paise == 550000


# ------------------------------------------------------------- matching rules

def test_shared_words_never_auto_match_across_months(db):
    w = World(db)
    w.bank(date(2025, 12, 31), cr=1, bal=10000000, narr="BAL")
    w.book(date(2026, 1, 5), out=5000000, narr="RENT JANUARY SHARMA PROPERTIES")
    w.bank(date(2026, 3, 28), dr=5000000, bal=5000000, narr="NEFT SHARMA PROPERTIES RENT")
    run = w.run(date(2026, 1, 1), date(2026, 3, 31), opening=10000000)
    assert w.matches(run) == []                    # 82 days apart: not even a suggestion
    assert {i.brs_category for i in w.items(run)} == {"unpresented_cheque", "UNMATCHED_BANK_TRANSACTION"}
    assert run.residual_paise == 0


def test_shared_word_within_30_days_is_only_a_suggestion(db):
    w = World(db)
    w.bank(date(2026, 2, 28), cr=1, bal=100000, narr="BAL")
    w.book(date(2026, 3, 1), out=50000, narr="SHARMA PROPERTIES")
    w.bank(date(2026, 3, 20), dr=50000, bal=50000, narr="NEFT SHARMA")
    run = w.run(date(2026, 3, 1), date(2026, 3, 31), opening=100000)
    (m,) = w.matches(run)
    assert m.status == MatchStatusEnum.PENDING_REVIEW.value and m.tier == "suggested"
    assert run.residual_paise == 0
    assert run.verdict == RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value


def test_cheque_number_matches_up_to_90_days_other_ids_7(db):
    w = World(db)
    w.bank(date(2025, 12, 31), cr=1, bal=1000000, narr="BAL")
    w.book(date(2026, 1, 5), out=100000, narr="SUPPLIER", inst="004455")
    w.bank(date(2026, 3, 20), dr=100000, bal=970000, narr="CLG CHQ 004455")       # 74 days
    w.book(date(2026, 1, 5), inn=70000, narr="NEFT UTR HDFCN52026010512345")
    w.bank(date(2026, 1, 20), cr=70000, bal=1070000, narr="NEFT HDFCN52026010512345")  # 15 days
    run = w.run(date(2026, 1, 1), date(2026, 3, 31), opening=1000000)
    auto = w.matches(run, status=MatchStatusEnum.AUTO_MATCHED.value)
    assert len(auto) == 1 and "004455".lstrip("0") in auto[0].reason
    # the UTR pair is 15 days apart: outside 7, so not auto-matched
    assert run.residual_paise == 0


def test_same_amount_unique_both_sides_within_7_days_auto(db):
    w = World(db)
    w.bank(date(2026, 2, 28), cr=1, bal=100000, narr="BAL")
    w.book(date(2026, 3, 3), out=12345, narr="COURIER")
    w.bank(date(2026, 3, 6), dr=12345, bal=87655, narr="UPI BLUEDART")
    run = w.run(date(2026, 3, 1), date(2026, 3, 31), opening=100000)
    (m,) = w.matches(run)
    assert m.status == MatchStatusEnum.AUTO_MATCHED.value
    assert run.verdict == RunVerdictEnum.RECONCILED_CLEAN.value


def test_same_amount_two_candidates_is_not_auto(db):
    w = World(db)
    w.bank(date(2026, 2, 28), cr=1, bal=100000, narr="BAL")
    w.book(date(2026, 3, 3), out=12345, narr="COURIER")
    w.bank(date(2026, 3, 5), dr=12345, bal=87655, narr="UPI ONE")
    w.bank(date(2026, 3, 7), dr=12345, bal=75310, narr="UPI TWO")
    run = w.run(date(2026, 3, 1), date(2026, 3, 31), opening=100000)
    assert not w.matches(run, status=MatchStatusEnum.AUTO_MATCHED.value)
    assert w.matches(run, status=MatchStatusEnum.PENDING_REVIEW.value)


# --------------------------------------------------------------- duplicates

def test_identical_ledger_rows_are_both_kept(db):
    w = World(db)
    w.bank(date(2026, 2, 28), cr=1, bal=1000000, narr="BAL")
    w.book(date(2026, 3, 10), out=50000, narr="TEA")
    w.book(date(2026, 3, 10), out=50000, narr="COURIER")
    w.bank(date(2026, 3, 10), dr=50000, bal=950000, narr="UPI CHAIWALA")
    w.bank(date(2026, 3, 10), dr=50000, bal=900000, narr="UPI BLUEDART")
    run = w.run(date(2026, 3, 1), date(2026, 3, 31), opening=1000000)
    assert run.book_closing_paise == 900000          # v2: 950000 (one row dropped)
    assert run.matched_count == 2
    assert w.items(run) == []
    assert run.verdict == RunVerdictEnum.RECONCILED_CLEAN.value


def test_extra_identical_row_is_a_possible_duplicate(db):
    w = World(db)
    w.bank(date(2026, 2, 28), cr=1, bal=1000000, narr="BAL")
    w.book(date(2026, 3, 10), out=50000, narr="TEA")
    w.book(date(2026, 3, 10), out=50000, narr="TEA")
    w.bank(date(2026, 3, 10), dr=50000, bal=950000, narr="UPI CHAIWALA")
    run = w.run(date(2026, 3, 1), date(2026, 3, 31), opening=1000000)
    (dup,) = w.items(run)
    assert dup.brs_category == "duplicate_ledger_debit"
    assert dup.exception_reason == "possible_duplicate_ledger_row"
    assert run.residual_paise == 0


# ------------------------------------------------------- amount differences

def test_amount_difference_is_a_bridge_line(db):
    w = World(db)
    w.bank(date(2026, 2, 28), cr=1, bal=0, narr="BAL")
    w.book(date(2026, 3, 30), inn=1240000, narr="UPI/508966282233")
    w.bank(date(2026, 3, 30), cr=1246000, bal=1246000, narr="UPI/508966282233/BHARATPE")
    run = w.run(date(2026, 3, 1), date(2026, 3, 31), opening=0)
    (m,) = w.matches(run)
    assert m.tier == "amount_difference" and m.status == MatchStatusEnum.PENDING_REVIEW.value
    (d,) = w.items(run)
    assert (d.brs_category, d.amount_paise, d.direction) == ("amount_difference", 6000, "add")
    assert run.residual_paise == 0
    assert run.verdict == RunVerdictEnum.RECONCILED_WITH_EXCEPTIONS.value


# ------------------------------------------------------------------- groups

def test_group_match_both_directions(db):
    w = World(db)
    w.bank(date(2026, 2, 28), cr=1, bal=0, narr="BAL")
    # deposit slip: one bank credit = three ledger receipts
    for amt in (10000, 25000, 15000):
        w.book(date(2026, 3, 5), inn=amt, narr="CHEQUE RECEIVED")
    w.bank(date(2026, 3, 6), cr=50000, bal=50000, narr="CLEARING DEPOSIT")
    # one ledger payment the bank split into two debits
    w.book(date(2026, 3, 12), out=30000, narr="VENDOR PAYMENT")
    w.bank(date(2026, 3, 12), dr=20000, bal=30000, narr="NEFT PART 1")
    w.bank(date(2026, 3, 13), dr=10000, bal=20000, narr="NEFT PART 2")
    run = w.run(date(2026, 3, 1), date(2026, 3, 31), opening=0)
    groups = w.matches(run, tier="tier_3_group")
    assert len(groups) == 2
    assert all(g.status == MatchStatusEnum.PENDING_REVIEW.value for g in groups)
    assert sorted(len(g.lines) for g in groups) == [3, 4]
    assert run.residual_paise == 0


def test_group_search_is_bounded(db):
    w = World(db)
    w.bank(date(2026, 2, 28), cr=1, bal=0, narr="BAL")
    for k in range(200):
        w.db.add(BookEntry(user_id=w.user.id, account_id=w.acct.id, import_batch_id=w.batch.id,
                           entry_date=date(2026, 3, 10), money_in_paise=100000 + k * 37, money_out_paise=0,
                           narration=f"R{k}", row_index=1000 + k, source_row_hash=uuid.uuid4().hex))
    for k in range(20):
        w.db.add(Transaction(user_id=w.user.id, account_id=w.acct.id, txn_date=date(2026, 3, 11),
                             credit_paise=99 + k, debit_paise=0, direction=Direction.CREDIT,
                             source_type=SourceType.STATEMENT, narration_raw=f"ODD {k}", row_index=5000 + k))
    w.db.commit()
    t0 = time.time()
    w.run(date(2026, 3, 1), date(2026, 3, 31), opening=0)
    assert time.time() - t0 < 30            # v2: ~n^5 per bank entry -> hours here


# -------------------------------------------------------- reviewer decisions

def test_reject_rebuilds_the_run_and_survives_a_rerun(db):
    w = World(db)
    w.bank(date(2026, 2, 28), cr=1, bal=100000, narr="BAL")
    w.book(date(2026, 3, 1), out=50000, narr="SHARMA PROPERTIES")
    w.bank(date(2026, 3, 20), dr=50000, bal=50000, narr="NEFT SHARMA")
    run = w.run(date(2026, 3, 1), date(2026, 3, 31), opening=100000)
    (m,) = w.matches(run)
    assert w.items(run) == []

    m.status = MatchStatusEnum.REJECTED.value
    db.commit()
    rebuild_run(db, run)
    assert run.pending_review_count == 0
    assert {i.brs_category for i in w.items(run)} == {"unpresented_cheque", "UNMATCHED_BANK_TRANSACTION"}
    assert run.residual_paise == 0

    rerun = w.run(date(2026, 3, 1), date(2026, 3, 31), opening=100000)
    assert rerun.version == 2
    assert w.matches(rerun) == []               # the rejected pair is not proposed again


def test_confirmed_suggestion_is_reapplied_on_rerun(db):
    w = World(db)
    w.bank(date(2026, 2, 28), cr=1, bal=100000, narr="BAL")
    w.book(date(2026, 3, 1), out=50000, narr="SHARMA PROPERTIES")
    w.bank(date(2026, 3, 20), dr=50000, bal=50000, narr="NEFT SHARMA")
    run = w.run(date(2026, 3, 1), date(2026, 3, 31), opening=100000)
    (m,) = w.matches(run)
    m.status = MatchStatusEnum.CONFIRMED.value
    db.commit()
    rebuild_run(db, run)
    assert run.verdict == RunVerdictEnum.RECONCILED_CLEAN.value

    rerun = w.run(date(2026, 3, 1), date(2026, 3, 31), opening=100000)
    (m2,) = w.matches(rerun)
    assert m2.status == MatchStatusEnum.CONFIRMED.value
    assert rerun.verdict == RunVerdictEnum.RECONCILED_CLEAN.value


# --------------------------------------------------------------- ledger import

def _import(client, headers, account_id, rows, **extra):
    return client.post("/v1/reconciliation/imports/confirm", headers=headers, json={
        "account_id": account_id, "ledger_convention": "STANDARD",
        "column_mapping": {"entry_date": "Date", "narration": "Narration", "money_in": "Debit",
                           "money_out": "Credit", "instrument_no": "Chq"},
        "rows": rows, **extra})


def _client_user(client):
    from tests.conftest import register_bank_account
    email = f"imp_{uuid.uuid4().hex[:8]}@kredo.in"
    client.post("/auth/register", json={"email": email, "password": "Secret123!", "full_name": "Imp",
                                        "entity_type": "BUSINESS"})
    tok = client.post("/auth/login", json={"email": email, "password": "Secret123!"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}
    acct = register_bank_account(client, headers)["id"]
    return headers, acct


def test_import_keeps_closing_named_entries_and_reads_balance_rows(client):
    headers, acct = _client_user(client)
    rows = [
        {"Date": "", "Narration": "Opening Balance", "Debit": "", "Credit": "1,00,000.00", "Chq": ""},
        {"Date": "05-03-2026", "Narration": "Loan closing charges", "Debit": "2,500.00", "Credit": "", "Chq": ""},
        {"Date": "06-03-2026", "Narration": "Tea", "Debit": "500.00", "Credit": "", "Chq": ""},
        {"Date": "06-03-2026", "Narration": "Tea", "Debit": "500.00", "Credit": "", "Chq": ""},
        {"Date": "07-03-2026", "Narration": "Refund", "Debit": "-300.00", "Credit": "", "Chq": ""},
        {"Date": "", "Narration": "Closing Balance", "Debit": "", "Credit": "96,800.00", "Chq": ""},
    ]
    res = _import(client, headers, acct, rows)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["row_count"] == 4                       # closing-charges row kept, both Tea rows kept
    assert body["book_opening_paise"] == 10000000 and body["book_opening_source"] == "ledger_file"
    assert body["negative_amount_rows"] == [5]


def test_import_typed_opening_wins_and_ambiguous_rows_are_refused(client):
    headers, acct = _client_user(client)
    ok = _import(client, headers, acct, [
        {"Date": "05-03-2026", "Narration": "A", "Debit": "100", "Credit": "", "Chq": ""}], book_opening="(1,250.50)")
    assert ok.status_code == 200
    assert ok.json()["book_opening_paise"] == -125050 and ok.json()["book_opening_source"] == "manual"
    bad = _import(client, headers, acct, [
        {"Date": "05-03-2026", "Narration": "A", "Debit": "100", "Credit": "50", "Chq": ""}])
    assert bad.status_code == 400 and "rows [1]" in bad.json()["detail"]


def test_run_without_opening_answers_with_error_code(client):
    headers, acct = _client_user(client)
    _import(client, headers, acct, [{"Date": "05-03-2026", "Narration": "A", "Debit": "100", "Credit": "", "Chq": ""}])
    res = client.post("/v1/reconciliation/runs", headers=headers, json={
        "account_id": acct, "period_from": "2026-03-01", "period_to": "2026-03-31", "force": True})
    assert res.status_code == 400
    assert res.headers.get("X-Error-Code") == "BOOK_OPENING_REQUIRED"
    res = client.post("/v1/reconciliation/runs", headers=headers, json={
        "account_id": acct, "period_from": "2026-03-01", "period_to": "2026-03-31", "force": True,
        "book_opening": "0"})
    assert res.status_code == 200, res.text
    assert res.json()["book_opening_source"] == "manual"


def test_rebuild_keeps_a_category_a_person_set(db):
    w = World(db)
    w.bank(date(2026, 2, 28), cr=1, bal=100000, narr="BAL")
    w.book(date(2026, 3, 1), out=50000, narr="SHARMA PROPERTIES")
    w.bank(date(2026, 3, 20), dr=50000, bal=50000, narr="NEFT SHARMA")
    w.bank(date(2026, 3, 25), dr=1000, bal=49000, narr="MISC DEBIT")
    run = w.run(date(2026, 3, 1), date(2026, 3, 31), opening=100000)
    (misc,) = w.items(run)
    misc.brs_category, misc.overridden_by_user = "bank_charge", True
    misc_txn = misc.bank_txn_id
    db.commit()
    (m,) = w.matches(run)
    m.status = MatchStatusEnum.REJECTED.value
    db.commit()
    rebuild_run(db, run)
    kept = [i for i in w.items(run) if i.bank_txn_id == misc_txn]
    assert kept and kept[0].brs_category == "bank_charge" and kept[0].overridden_by_user
