"""The Credit Lens consolidation requirement, one test per example in the spec.

Statements are built directly as extracted rows so each test states exactly the
situation the spec describes; the PDF extractor is tested separately against
the real statements (skipped when those files are not on disk).
"""
from __future__ import annotations

import datetime as dt
import glob
import os

import pytest

from app.b2b.consolidate.extract import CREDIT, DEBIT, RawTxn, StatementExtract
from app.b2b.consolidate.metadata import StatementMeta
from app.b2b.consolidate.reconcile import SourceStatement
from app.b2b.consolidate.service import consolidate_statements
from app.b2b.consolidate.categorize import categorize
from app.b2b.consolidate.tokens import balance_paise, parse_amount, parse_date

D = dt.date


def row(day, narr, amount, direction, balance, month=4):
    return RawTxn(date=D(2026, month, day), narration=narr, amount_paise=int(round(amount * 100)),
                  direction=direction, balance_paise=int(round(balance * 100)), page=1)


def stmt(name, account, rows, idx, bank="HDFC Bank", opening=None, closing=None, holder=None):
    meta = StatementMeta(bank_name=bank, account_number=account, account_holder=holder)
    ex = StatementExtract(meta=meta, rows=rows, opening_paise=None if opening is None else int(opening * 100),
                          closing_paise=None if closing is None else int(closing * 100))
    return SourceStatement(file_name=name, sha256=name, extract=ex, account_no=account,
                           bank_name=bank, index=idx)


def flags_of(result, kind):
    return [f for f in result["flags"] if f["type"] == kind]


# ---------------------------------------------------------------- Check 1

def test_example1_overlapping_day_kept_once():
    # Statement 1: 1–5 April. Statement 2: 5–7 April. 5 April is in both.
    s1 = [row(1, "UPI/111/CR/A", 1000, CREDIT, 11000), row(3, "UPI/222/DR/B", 500, DEBIT, 10500),
          row(5, "UPI/333/DR/C", 200, DEBIT, 10300), row(5, "UPI/444/CR/D", 700, CREDIT, 11000)]
    s2 = [row(5, "UPI/333/DR/C", 200, DEBIT, 10300), row(5, "UPI/444/CR/D", 700, CREDIT, 11000),
          row(6, "UPI/555/DR/E", 1000, DEBIT, 10000), row(7, "UPI/666/CR/F", 50, CREDIT, 10050)]
    r = consolidate_statements([stmt("s1.pdf", "111", s1, 0), stmt("s2.pdf", "111", s2, 1)])
    assert r["summary"]["transactions_output"] == 6
    assert r["summary"]["duplicates_removed"] == 2
    assert [t["date"] for t in r["transactions"]].count("2026-04-05") == 2
    assert not flags_of(r, "MISSING_TRANSACTIONS_BETWEEN_STATEMENTS")
    assert r["summary"]["balance_check"] == "PASSED"


def test_example2_partial_day_merges_to_all_four_once():
    # Statement 1 downloaded mid-day: 2 of the 4 entries of 5 April.
    day = [row(5, "UPI/901/DR/SHOP", 100, DEBIT, 9900), row(5, "UPI/902/DR/SHOP", 100, DEBIT, 9800),
           row(5, "UPI/903/DR/SHOP", 100, DEBIT, 9700), row(5, "UPI/904/CR/REFUND", 50, CREDIT, 9750)]
    s1 = [row(4, "OPEN CR", 10000, CREDIT, 10000)] + day[:2]
    s2 = day
    r = consolidate_statements([stmt("s1.pdf", "111", s1, 0), stmt("s2.pdf", "111", s2, 1)])
    on_5th = [t for t in r["transactions"] if t["date"] == "2026-04-05"]
    assert len(on_5th) == 4                                   # not 2, not 6
    assert [t["balance"] for t in on_5th] == [9900.0, 9800.0, 9700.0, 9750.0]
    assert r["summary"]["duplicates_removed"] == 2
    assert r["summary"]["balance_check"] == "PASSED"


def test_genuine_same_day_repeats_are_not_duplicates():
    # Same date, narration and amount — different balances — both stay.
    s1 = [row(5, "UPI/SWIGGY", 250, DEBIT, 9750), row(5, "UPI/SWIGGY", 250, DEBIT, 9500)]
    r = consolidate_statements([stmt("s1.pdf", "111", s1, 0)])
    assert r["summary"]["transactions_output"] == 2
    assert r["summary"]["duplicates_removed"] == 0


def test_example3_internal_transfer_kept_both_sides_and_tagged():
    hdfc = [row(10, "NEFT/N123456789/TO SBI A/C XX5678/SELF", 10000, DEBIT, 40000)]
    sbi = [row(10, "NEFT/N123456789/FROM HDFC XX1234", 10000, CREDIT, 25000)]
    r = consolidate_statements([stmt("hdfc.pdf", "50100001234", hdfc, 0, bank="HDFC Bank"),
                                stmt("sbi.pdf", "30000005678", sbi, 1, bank="State Bank of India")])
    rows = {t["bank_account_no"]: t for t in r["transactions"]}
    assert len(r["transactions"]) == 2                          # not a duplicate
    assert rows["50100001234"]["category_1"] == "Internal Transfer"
    assert rows["50100001234"]["category_2"] == "30000005678"
    assert rows["30000005678"]["category_1"] == "Internal Transfer"
    assert rows["30000005678"]["category_2"] == "50100001234"
    assert rows["50100001234"]["type"] == "Money Out"
    assert rows["30000005678"]["type"] == "Money In"


def test_equal_amounts_without_evidence_are_not_called_transfers():
    a = [row(10, "UPI/777/DR/GROCER", 500, DEBIT, 9500)]
    b = [row(10, "UPI/888/CR/STUDENT FEE", 500, CREDIT, 20500)]
    r = consolidate_statements([stmt("a.pdf", "1111111111", a, 0), stmt("b.pdf", "2222222222", b, 1)])
    assert all(t["category_1"] != "Internal Transfer" for t in r["transactions"])


# ---------------------------------------------------------------- Check 2

def test_example4_gap_between_statements_flagged_with_date_and_difference():
    s1 = [row(1, "CR", 100, CREDIT, 200), row(5, "DR", 100, DEBIT, 100)]            # closes at 100
    s2 = [row(6, "DR", 30, DEBIT, 90), row(7, "CR", 10, CREDIT, 100)]               # 90 = 120 - 30
    r = consolidate_statements([stmt("s1.pdf", "111", s1, 0), stmt("s2.pdf", "111", s2, 1)])
    gaps = flags_of(r, "MISSING_TRANSACTIONS_BETWEEN_STATEMENTS")
    assert len(gaps) == 1
    assert gaps[0]["date"] == "2026-04-06"
    assert gaps[0]["difference"] == 20.0
    assert gaps[0]["previous_statement"] == ["s1.pdf"] and gaps[0]["statement"] == ["s2.pdf"]
    assert r["summary"]["balance_check"] == "FAILED"
    flagged = [t for t in r["transactions"] if t["flags"]]
    assert flagged and flagged[0]["date"] == "2026-04-06"


def test_example5_closing_balance_mismatch_within_statement():
    # Opening 5,000; extracted entries net to 3,500; the statement closes at 3,200.
    rows = [row(2, "DR", 1000, DEBIT, 4000), row(3, "DR", 500, DEBIT, 3500)]
    r = consolidate_statements([stmt("s.pdf", "111", rows, 0, opening=5000, closing=3200)])
    f = flags_of(r, "CLOSING_BALANCE_MISMATCH")
    assert len(f) == 1
    assert f[0]["difference"] == -300.0
    assert f[0]["computed_closing_balance"] == 3500.0 and f[0]["stated_closing_balance"] == 3200.0
    st = r["statements"][0]
    assert st["status"] == "FAILED" and st["difference"] == -300.0


def test_example5_missed_row_inside_statement_breaks_continuity():
    rows = [row(2, "DR", 1000, DEBIT, 4000), row(3, "DR", 500, DEBIT, 3200)]  # a 300 row is missing
    r = consolidate_statements([stmt("s.pdf", "111", rows, 0, opening=5000)])
    f = flags_of(r, "BALANCE_BREAK_WITHIN_STATEMENT")
    assert len(f) == 1 and f[0]["date"] == "2026-04-03" and f[0]["difference"] == -300.0


def test_output_record_has_the_eight_fields_in_order():
    r = consolidate_statements([stmt("s.pdf", "111", [row(1, "ACH-DR-BAJAJ FINANCE-123456", 5000, DEBIT, 5000)], 0)])
    t = r["transactions"][0]
    assert list(t)[:8] == ["bank_account_no", "bank_name", "date", "narration", "amount", "type",
                           "category_1", "category_2"]
    assert t["type"] == "Money Out" and t["category_1"] == "EMI" and t["category_2"] == "Bajaj Finance"


@pytest.mark.parametrize("narr,direction,cat1", [
    ("ACH-DR-DEUTSCHE BANK-350041027230019-UTIB70128022", DEBIT, "EMI"),
    ("ECS/SME000006475684/BAJAJ FINANCE LIMITED", DEBIT, "EMI"),
    ("Loan Recovery For29940600013134", DEBIT, "Loan Deduction"),
    ("920030019009029:Int.Coll:06-07-2025 to 05-08-2025", DEBIT, "Loan Deduction"),
    ("RTGS/HDFCR52025091059438331/BAJAJ FINANCE LIMITED/HDFC BANK///C21", CREDIT, "Loan Received"),
    ("NEFT/SCBLH27301738459/ALSTOM TRANSPORT INDIA LIMIT/STANDARD CHARTERED B/", CREDIT, "Business Receipt"),
    ("920030019009029:ECS Return CHGS_Oct-25", DEBIT, "Bounce Charges"),
    ("UPI/120440066952/DR/DRMMULTISPECIA/FDR/UPI", DEBIT, "Medical Expenses"),
    ("WDL TFR UPI/DR/711400899380/Fresh", DEBIT, "Food Expenses"),
    ("IRCTC WEB UPI", DEBIT, "Travel Expenses"),
    ("521619024201-ATM-JAYALAKSHMIPURAM MYSORE KAIN-N02", DEBIT, "Cash Withdrawal"),
    ("BY CASH RANJITH", CREDIT, "Cash Deposit"),
    ("INB/724443268/TIN 2.0 CBDT TAX PAYMENT/", DEBIT, "Tax Payment"),
    ("NEFT/EB/AXOEB21971423233/SUGUMARAN M/STATE BANK OF INDIA//SALARY/////", DEBIT, "Salary Paid"),
    ("SALARY", CREDIT, "Salary Received"),
    ("Int.Pd:01-11-2025 to 31-01-2026:165501000014311", CREDIT, "Interest Received"),
    ("UPI/608245215708/CR/ VIJAYAN G/IOB/UPI", CREDIT, "Transfer In"),
])
def test_category_1(narr, direction, cat1):
    assert categorize(narr, direction, 100000)[0] == cat1


def test_category_2_is_No_when_nothing_to_add():
    assert categorize("SMS Charges for APR 25", DEBIT, 24)[1] == "No"


def test_tokens():
    assert parse_date("01-JAN-2026") == D(2026, 1, 1)
    assert parse_date("23-Mar-26") == D(2026, 3, 23)
    assert parse_date("2025-04-01") == D(2025, 4, 1)
    assert parse_amount("1,03,07,226.42") == (1030722642, None)
    assert parse_amount("70,875.00Cr") == (7087500, "CR")
    assert balance_paise("-10786320.64") == -1078632064
    assert balance_paise("7,97,675.48CR") == 79767548
    assert balance_paise("1,000.00Dr") == -100000


# ------------------------------------------------ the real Credit Lens files

CASES = os.getenv("CREDITLENS_SAMPLES", "/tmp/cl/Creditlens")


@pytest.mark.skipif(not os.path.isdir(CASES), reason="Credit Lens sample statements not present")
@pytest.mark.parametrize("case,accounts,out,dups", [
    ("Case 2", 3, 2787, 0),
    ("Case 3", 5, 1261, 376),
])
def test_real_cases_reconcile_end_to_end(case, accounts, out, dups):
    from app.b2b.consolidate.service import consolidate
    files = sorted(glob.glob(f"{CASES}/{case}/**/*.[pP][dD][fF]", recursive=True))
    r = consolidate([(p, os.path.basename(p)) for p in files])
    s = r["summary"]
    assert s["accounts"] == accounts
    assert s["transactions_output"] == out
    assert s["duplicates_removed"] == dups
    # Every statement's arithmetic closes, across statements too.
    assert all(a["balance_check"] == "PASSED" for a in r["accounts"])
    assert all(st["status"] == "PASSED" for st in r["statements"])
    assert all(t["category_1"] and t["category_2"] for t in r["transactions"])


# ------------------------------------------------------------ HTTP endpoint

@pytest.fixture
def api():
    import uuid
    from fastapi.testclient import TestClient
    from app.b2b import auth as b2b_auth
    from main import app
    from tests.conftest import TestingSessionLocal
    db = TestingSessionLocal()
    c = b2b_auth.create_client(db, name="CL", slug=f"cl-{uuid.uuid4().hex[:8]}")
    c.rate_limit_per_minute, c.rate_limit_per_day, c.rate_limit_per_month = 1000, 10000, 100000
    db.commit()
    issued = b2b_auth.issue_key(db, c, name="t")
    secret = issued.secret if hasattr(issued, "secret") else issued[1]
    db.close()
    with TestClient(app) as tc:
        yield tc, {"Authorization": f"Bearer {secret}"}


SAMPLES = os.path.join(os.path.dirname(__file__), "..", "..", "postman", "samples")


def test_endpoint_dedupes_two_copies_of_one_statement(api):
    import json as _json
    tc, h = api
    files = [("files", ("a.csv", open(os.path.join(SAMPLES, "sample_statement.csv"), "rb"))),
             ("files", ("b.tsv", open(os.path.join(SAMPLES, "sample_statement.tsv"), "rb")))]
    r = tc.post("/v1/statements/consolidate", headers=h, files=files,
                data={"account_numbers": _json.dumps({"a.csv": "ACC1", "b.tsv": "ACC1"})})
    assert r.status_code == 200, r.text
    s = r.json()["data"]["summary"]
    assert (s["duplicates_removed"], s["transactions_output"], s["balance_check"]) == (24, 24, "PASSED")
    rid = r.json()["request_id"]
    g = tc.get(f"/v1/statements/consolidate/{rid}", headers=h)
    assert g.status_code == 200 and g.json()["data"]["summary"]["transactions_output"] == 24


def test_endpoint_accepts_a_zip_of_statements(api):
    import io, json as _json, zipfile
    tc, h = api
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(os.path.join(SAMPLES, "sample_statement.csv"), "case/a.csv")
        z.write(os.path.join(SAMPLES, "sample_statement.json"), "case/b.json")
        z.writestr("case/notes.docx", b"not a statement")          # ignored
        z.writestr("../../evil.csv", b"Date,Narration\n")             # never written outside
    r = tc.post("/v1/statements/consolidate", headers=h,
                files=[("files", ("case.zip", buf.getvalue(), "application/zip"))],
                data={"account_numbers": _json.dumps({"case/a.csv": "Z1", "case/b.json": "Z1"})})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["summary"]["transactions_output"] == 24 and d["summary"]["duplicates_removed"] == 24
    assert not os.path.exists(os.path.join(os.path.dirname(SAMPLES), "..", "evil.csv"))


def test_endpoint_rejects_missing_key_and_bad_json(api):
    tc, h = api
    f = [("files", ("a.csv", open(os.path.join(SAMPLES, "sample_statement.csv"), "rb")))]
    assert tc.post("/v1/statements/consolidate", files=f).json()["error"]["code"] == "MISSING_API_KEY"
    f = [("files", ("a.csv", open(os.path.join(SAMPLES, "sample_statement.csv"), "rb")))]
    r = tc.post("/v1/statements/consolidate", headers=h, files=f, data={"passwords": "[1,2]"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_PARAMETER"


# ------------------------------------------------ production hardening

def test_busy_service_answers_503_not_a_hang(monkeypatch):
    import threading
    from app.b2b import jobs
    monkeypatch.setattr(jobs, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(jobs, "WAIT_SECONDS", 0.05)
    from app.b2b.errors import ApiError
    with jobs.heavy_slot():
        with pytest.raises(ApiError) as exc:
            with jobs.heavy_slot():
                pass
    assert exc.value.code == "SERVICE_UNAVAILABLE" and exc.value.headers["Retry-After"] == "30"
    with jobs.heavy_slot():                       # released again afterwards
        pass


def test_continuity_does_not_bridge_a_row_without_balance():
    from app.b2b.analysis.balances import continuity_check
    from app.b2b.canonical import CanonicalTxn
    rows = [CanonicalTxn(txn_date=D(2025, 3, 1), direction="credit", credit_paise=10000, balance_paise=19508, row_index=0),
            CanonicalTxn(txn_date=D(2025, 3, 10), direction="debit", debit_paise=19508, balance_paise=None, row_index=1),
            CanonicalTxn(txn_date=D(2025, 3, 11), direction="credit", credit_paise=700000, balance_paise=700000, row_index=2)]
    r = continuity_check(rows)
    assert r["breaks"] == []          # the None row is skipped, not bridged


@pytest.mark.skipif(not os.path.isdir(CASES), reason="Credit Lens sample statements not present")
def test_analyze_uses_the_reconciled_extractor_for_real_pdfs():
    from app.b2b.parsers import legacy
    p = os.path.join(CASES, "Case 3", "Bank statements", "IOB 323 Account Statement jan 206 to Apr 2026.pdf")
    out = legacy.parse(p)
    assert len(out.transactions) == 238                 # the old pipeline read 5
    assert out.continuity_passed is True and out.statement_meta["account_number"] == "165502000000323"
