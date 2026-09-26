import uuid
import pytest
from datetime import date
from fastapi.testclient import TestClient

from app.models.transaction import Transaction, Direction, SourceType
from app.models.reconciliation import ImportBatch, BookEntry, ReconciliationRun, ReconciliationItem, ReconciliationMatch
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


def test_full_api_reconciliation_pipeline_integration(client: TestClient):
    """
    Full End-to-End Production API Integration Test.
    Exercises:
    1. Register user & account
    2. Post controlled bank transactions (Bank ₹12,460 Credit)
    3. Import controlled ledger (Ledger ₹12,400 Credit)
    4. Execute run via POST /v1/reconciliation/runs
    5. Retrieve report via GET /v1/reconciliation/runs/{run_id}
    6. Verify all 10 invariants on the actual API report response.
    """
    headers = get_auth_headers(client, "e2e_recon_user2@kredo.in")

    me_res = client.get("/auth/me", headers=headers)
    assert me_res.status_code == 200
    user_id = uuid.UUID(me_res.json()["id"])

    db_gen = client.app.dependency_overrides.get(get_db, get_db)()
    db = next(db_gen)

    acc_res = client.post("/v1/bank-master/accounts", headers=headers, json={
        "bank_code": "HDFC",
        # Bank Master requires a 9-18 digit account number (it masks the last 4).
        "account_number": "502000005242",
        "account_type": "CURRENT"
    })
    assert acc_res.status_code == 201
    account_id = uuid.UUID(acc_res.json()["id"])

    # 1. Post Controlled Bank Transactions via /transactions API
    # Controlled Case 3: Bank ₹12,460 CREDIT
    bank_data = [
        ("2025-03-30", "UPI/508966282233/04:20:46/UPI/bharatpe payouts@yes", 0, 1246000, "508966282233"), # Bank ₹12,460 Credit
        ("2025-03-29", "EBANK:SELF/1448664605/COASTAL TO COASTAL 24", 10000000, 0, "1448664605"),
        ("2025-03-29", "NEFT-YESCB50880037964-RESILIENT INNOVATIONS PVT LTD", 0, 2587400, "YESCB50880037964"), # Date 29-Mar in Bank
        ("2025-03-28", "NEFT-YESCB50870093682-RESILIENT INNOVATIONS PVT LTD", 0, 2186100, "YESCB50870093682"),
        ("2025-03-27", "NEFT-YESCB50860036730-RESILIENT INNOVATIONS PVT LTD", 0, 1351100, "YESCB50860036730"),
        ("2025-03-26", "TO CONSEPT STUDIO FINAL PAYMENT-VJRNBA", 334820600, 0, "VJRNBA"),
        ("2025-03-26", "74460600005241 Disbursement Credit", 0, 334556100, "74460600005241"),
        ("2025-03-26", "NEFT-YESCB50850137853-RESILIENT INNOVATIONS PVT LTD", 0, 980200, "YESCB50850137853"),
        ("2025-03-25", "NEFT-YESAP50842316559-RESILIENT INNOVATIONS PVT LTD", 0, 1961500, "YESAP50842316559"),
        ("2025-03-24", "NEFT-YESCB50830047799-RESILIENT INNOVATIONS PVT LTD", 0, 4546700, "YESCB50830047799"),
        ("2025-03-23", "NEFT-YESAP50820082224-RESILIENT INNOVATIONS PVT LTD", 0, 4319500, "YESAP50820082224"),
        ("2025-03-22", "NEFT-YESAP50810059389-RESILIENT INNOVATIONS PVT LTD", 0, 3743400, "YESAP50810059389"),
        ("2025-03-21", "NEFT-YESAP50800083567-RESILIENT INNOVATIONS PVT LTD", 0, 1589300, "YESAP50800083567"),
        ("2025-03-20", "NEFT-YESCB50790121574-RESILIENT INNOVATIONS PVT LTD", 0, 2998800, "YESCB50790121574"),
        ("2025-03-19", "NEFT-YESCB50780030062-RESILIENT INNOVATIONS PVT LTD", 0, 4274300, "YESCB50780030062"),
        ("2025-03-18", "EBANK:SELF/1446721903/Coastal to coastal", 29500000, 0, "1446721903"),
        ("2025-03-18", "NEFT-YESAP50770622093-RESILIENT INNOVATIONS PVT LTD", 0, 1906300, "YESAP50770622093"),
        ("2025-03-17", "NEFT-YESCB50760088553-RESILIENT INNOVATIONS PVT LTD", 0, 9135900, "YESCB50760088553"),
        ("2025-03-16", "NEFT-YESAP50750056357-RESILIENT INNOVATIONS PVT LTD", 0, 9618600, "YESAP50750056357"),
        ("2025-03-15", "NEFT-YESAP50740040948-RESILIENT INNOVATIONS PVT LTD", 0, 9341000, "YESAP50740040948"),
        ("2025-03-14", "EBANK:SELF/1446320023/coastal 108 to coastal 24", 14500000, 0, "1446320023"),
        ("2025-03-14", "UPI/507320829025/17:08:02/UPI/bharatpe payouts@yes", 0, 5193100, "507320829025"),
        ("2025-03-14", "NEFT-YESAP50730508455-RESILIENT INNOVATIONS PVT LTD", 0, 4812400, "YESAP50730508455"),
        ("2025-03-13", "NEFT-YESCB50720020206-RESILIENT INNOVATIONS PVT LTD", 0, 4752400, "YESCB50720020206"),
        ("2025-03-12", "EBANK:SELF/1446033730/coastal 108 to coastal 24", 24000000, 0, "1446033730"),
        ("2025-03-12", "NEFT-YESCB50710080363-RESILIENT INNOVATIONS PVT LTD", 0, 3377800, "YESCB50710080363"),
        ("2025-03-11", "Loan Recovery For74460600005241", 3152100, 0, "74460600005241"),
        ("2025-03-11", "NEFT-YESAP50700118538-RESILIENT INNOVATIONS PVT LTD", 0, 2878700, "YESAP50700118538"),
        ("2025-03-10", "NEFT-YESAP50690049799-RESILIENT INNOVATIONS PVT LTD", 0, 3051100, "YESAP50690049799"),
        ("2025-03-09", "NEFT-YESAP50680052083-RESILIENT INNOVATIONS PVT LTD", 0, 3605400, "YESAP50680052083"),
        ("2025-03-08", "NEFT-YESAP50670111143-RESILIENT INNOVATIONS PVT LTD", 0, 4359000, "YESAP50670111143"),
        ("2025-03-07", "NEFT-YESAP50660052780-RESILIENT INNOVATIONS PVT LTD", 0, 362600, "YESAP50660052780"),
        ("2025-03-06", "NEFT-YESAP50650563834-RESILIENT INNOVATIONS PVT LTD", 0, 4150500, "YESAP50650563834"),
        ("2025-03-05", "CONCEPT STUDIO-VJRNBA", 742600000, 0, "VJRNBA"),
        ("2025-03-05", "74460600005241 Disbursement Credit", 0, 742600000, "74460600005241"),
        ("2025-03-05", "NEFT-FBBT250646263316-BHARATPEPPG", 0, 18000, "FBBT250646263316"),
        ("2025-03-04", "NEFT-AXNFCN0934241034-RESILIENT INNOVATIONS PRIVAT", 0, 301000, "AXNFCN0934241034"),
        ("2025-03-03", "NEFT-YESAP50620151206-RESILIENT INNOVATIONS PVT LTD", 0, 1476300, "YESAP50620151206"),
        ("2025-03-02", "NEFT-YESAP50610065489-RESILIENT INNOVATIONS PVT LTD", 0, 1653100, "YESAP50610065489"),
        ("2025-03-15", "UNEXPLAINED BANK CHARGE TEST", 655416, 0, "BANK-ONLY-CHARGE"), # BANK_ONLY
    ]

    for dt, narr, deb, cred, ref in bank_data:
        tx = Transaction(
            user_id=user_id,
            account_id=account_id,
            txn_date=date.fromisoformat(dt),
            narration_raw=narr,
            reference_no=ref,
            debit_paise=deb if deb > 0 else None,
            credit_paise=cred if cred > 0 else None,
            balance_paise=24743498,
            direction=Direction.DEBIT if deb > 0 else Direction.CREDIT,
            source_type=SourceType.STATEMENT
        )
        db.add(tx)
    db.commit()

    # 2. Confirm Ledger Import via API /v1/reconciliation/imports/confirm
    # Controlled Case 3: Ledger ₹12,400 CREDIT
    ledger_rows = [
        {"entry_date": "2025-03-30", "narration": "UPI/508966282233/04:20:46/UPI/bharatpe payouts@yes", "money_in": "12400.0", "money_out": "", "instrument_no": "508966282233"},
        {"entry_date": "2025-03-29", "narration": "EBANK:SELF/1448664605/COASTAL TO COASTAL 24", "money_in": "", "money_out": "100000.0", "instrument_no": "1448664605"},
        {"entry_date": "2025-03-28", "narration": "NEFT-YESCB50880037964-RESILIENT INNOVATIONS PVT LTD", "money_in": "25874.0", "money_out": "", "instrument_no": "YESCB50880037964"},
        {"entry_date": "2025-03-28", "narration": "NEFT-YESCB50870093682-RESILIENT INNOVATIONS PVT LTD", "money_in": "21861.0", "money_out": "", "instrument_no": "YESCB50870093682"},
        {"entry_date": "2025-03-27", "narration": "NEFT-YESCB50860036730-RESILIENT INNOVATIONS PVT LTD", "money_in": "13511.0", "money_out": "", "instrument_no": "YESCB50860036730"},
        {"entry_date": "2025-03-27", "narration": "NEFT-YESCB50860036730-RESILIENT INNOVATIONS PVT LTD DUPLICATE TEST", "money_in": "13511.0", "money_out": "", "instrument_no": "YESCB50860036730-DUP"},
        {"entry_date": "2025-03-26", "narration": "TO CONSEPT STUDIO FINAL PAYMENT-VJRNBA", "money_in": "", "money_out": "3348206.0", "instrument_no": "VJRNBA"},
        {"entry_date": "2025-03-26", "narration": "74460600005241 Disbursement Credit", "money_in": "3345561.0", "money_out": "", "instrument_no": "74460600005241"},
        {"entry_date": "2025-03-26", "narration": "NEFT-YESCB50850137853-RESILIENT INNOVATIONS PVT LTD", "money_in": "9802.0", "money_out": "", "instrument_no": "YESCB50850137853"},
        {"entry_date": "2025-03-25", "narration": "NEFT-YESAP50842316559-RESILIENT INNOVATIONS PVT LTD", "money_in": "19615.0", "money_out": "", "instrument_no": "YESAP50842316559"},
        {"entry_date": "2025-03-24", "narration": "NEFT-YESCB50830047799-RESILIENT INNOVATIONS PVT LTD", "money_in": "45467.0", "money_out": "", "instrument_no": "YESCB50830047799"},
        {"entry_date": "2025-03-23", "narration": "NEFT-YESAP50820082224-RESILIENT INNOVATIONS PVT LTD", "money_in": "43195.0", "money_out": "", "instrument_no": "YESAP50820082224"},
        {"entry_date": "2025-03-22", "narration": "NEFT-YESAP50810059389-RESILIENT INNOVATIONS PVT LTD", "money_in": "37434.0", "money_out": "", "instrument_no": "YESAP50810059389"},
        {"entry_date": "2025-03-21", "narration": "NEFT-YESAP50800083567-RESILIENT INNOVATIONS PVT LTD", "money_in": "15893.0", "money_out": "", "instrument_no": "YESAP50800083567"},
        {"entry_date": "2025-03-20", "narration": "NEFT-YESCB50790121574-RESILIENT INNOVATIONS PVT LTD", "money_in": "29988.0", "money_out": "", "instrument_no": "YESCB50790121574"},
        {"entry_date": "2025-03-19", "narration": "NEFT-YESCB50780030062-RESILIENT INNOVATIONS PVT LTD", "money_in": "42743.0", "money_out": "", "instrument_no": "YESCB50780030062"},
        {"entry_date": "2025-03-18", "narration": "EBANK:SELF/1446721903/Coastal to coastal", "money_in": "", "money_out": "295000.0", "instrument_no": "1446721903"},
        {"entry_date": "2025-03-18", "narration": "NEFT-YESAP50770622093-RESILIENT INNOVATIONS PVT LTD", "money_in": "19063.0", "money_out": "", "instrument_no": "YESAP50770622093"},
        {"entry_date": "2025-03-17", "narration": "NEFT-YESCB50760088553-RESILIENT INNOVATIONS PVT LTD", "money_in": "91359.0", "money_out": "", "instrument_no": "YESCB50760088553"},
        {"entry_date": "2025-03-16", "narration": "NEFT-YESAP50750056357-RESILIENT INNOVATIONS PVT LTD", "money_in": "96186.0", "money_out": "", "instrument_no": "YESAP50750056357"},
        {"entry_date": "2025-03-15", "narration": "NEFT-YESAP50740040948-RESILIENT INNOVATIONS PVT LTD", "money_in": "93410.0", "money_out": "", "instrument_no": "YESAP50740040948"},
        {"entry_date": "2025-03-14", "narration": "EBANK:SELF/1446320023/coastal 108 to coastal 24", "money_in": "", "money_out": "145000.0", "instrument_no": "1446320023"},
        {"entry_date": "2025-03-14", "narration": "UPI/507320829025/17:08:02/UPI/bharatpe payouts@yes", "money_in": "51931.0", "money_out": "", "instrument_no": "507320829025"},
        {"entry_date": "2025-03-14", "narration": "NEFT-YESAP50730508455-RESILIENT INNOVATIONS PVT LTD", "money_in": "48124.0", "money_out": "", "instrument_no": "YESAP50730508455"},
        {"entry_date": "2025-03-13", "narration": "NEFT-YESCB50720020206-RESILIENT INNOVATIONS PVT LTD", "money_in": "47524.0", "money_out": "", "instrument_no": "YESCB50720020206"},
        {"entry_date": "2025-03-12", "narration": "EBANK:SELF/1446033730/coastal 108 to coastal 24", "money_in": "", "money_out": "240000.0", "instrument_no": "1446033730"},
        {"entry_date": "2025-03-12", "narration": "NEFT-YESCB50710080363-RESILIENT INNOVATIONS PVT LTD", "money_in": "33778.0", "money_out": "", "instrument_no": "YESCB50710080363"},
        {"entry_date": "2025-03-11", "narration": "Loan Recovery For74460600005241", "money_in": "", "money_out": "31521.0", "instrument_no": "74460600005241"},
        {"entry_date": "2025-03-11", "narration": "NEFT-YESAP50700118538-RESILIENT INNOVATIONS PVT LTD", "money_in": "28787.0", "money_out": "", "instrument_no": "YESAP50700118538"},
        {"entry_date": "2025-03-10", "narration": "NEFT-YESAP50690049799-RESILIENT INNOVATIONS PVT LTD", "money_in": "30511.0", "money_out": "", "instrument_no": "YESAP50690049799"},
        {"entry_date": "2025-03-09", "narration": "NEFT-YESAP50680052083-RESILIENT INNOVATIONS PVT LTD", "money_in": "36054.0", "money_out": "", "instrument_no": "YESAP50680052083"},
        {"entry_date": "2025-03-08", "narration": "NEFT-YESAP50670111143-RESILIENT INNOVATIONS PVT LTD", "money_in": "43590.0", "money_out": "", "instrument_no": "YESAP50670111143"},
        {"entry_date": "2025-03-07", "narration": "NEFT-YESAP50660052780-RESILIENT INNOVATIONS PVT LTD", "money_in": "3626.0", "money_out": "", "instrument_no": "YESAP50660052780"},
        {"entry_date": "2025-03-06", "narration": "NEFT-YESAP50650563834-RESILIENT INNOVATIONS PVT LTD", "money_in": "41505.0", "money_out": "", "instrument_no": "YESAP50650563834"},
        {"entry_date": "2025-03-05", "narration": "CONCEPT STUDIO-VJRNBA", "money_in": "", "money_out": "7426000.0", "instrument_no": "VJRNBA"},
        {"entry_date": "2025-03-05", "narration": "74460600005241 Disbursement Credit", "money_in": "7426000.0", "money_out": "", "instrument_no": "74460600005241"},
        {"entry_date": "2025-03-05", "narration": "NEFT-FBBT250646263316-BHARATPEPPG", "money_in": "180.0", "money_out": "", "instrument_no": "FBBT250646263316"},
        {"entry_date": "2025-03-04", "narration": "NEFT-AXNFCN0934241034-RESILIENT INNOVATIONS PRIVAT", "money_in": "3010.0", "money_out": "", "instrument_no": "AXNFCN0934241034"},
        {"entry_date": "2025-03-03", "narration": "NEFT-YESAP50620151206-RESILIENT INNOVATIONS PVT LTD", "money_in": "14763.0", "money_out": "", "instrument_no": "YESAP50620151206"},
        {"entry_date": "2025-03-02", "narration": "NEFT-YESAP50610065489-RESILIENT INNOVATIONS PVT LTD", "money_in": "16531.0", "money_out": "", "instrument_no": "YESAP50610065489"},
        {"entry_date": "2025-03-01", "narration": "OFFICE RENT - TEST LEDGER ONLY", "money_in": "", "money_out": "12000.0", "instrument_no": "LEDGER-ONLY-001"}
    ]

    import_res = client.post("/v1/reconciliation/imports/confirm", headers=headers, json={
        "account_id": str(account_id),
        "column_mapping": {
            "entry_date": "entry_date",
            "narration": "narration",
            "money_in": "money_in",
            "money_out": "money_out",
            "instrument_no": "instrument_no"
        },
        "rows": ledger_rows
    })
    assert import_res.status_code == 200
    batch_id = import_res.json()["batch_id"]

    # 3. Create Reconciliation Run via POST /v1/reconciliation/runs API
    run_res = client.post("/v1/reconciliation/runs", headers=headers, json={
        "account_id": str(account_id),
        "period_from": "2025-03-01",
        "period_to": "2025-03-30",
        "import_batch_id": str(batch_id),
        "force": True
    })
    assert run_res.status_code == 200
    run_id = run_res.json()["run_id"]

    # 4. Fetch Actual Report via GET /v1/reconciliation/runs/{run_id} API
    report_res = client.get(f"/v1/reconciliation/runs/{run_id}", headers=headers)
    assert report_res.status_code == 200
    report = report_res.json()

    # 5. Retrieve Debug Log Audit from Report
    debug_log = report["debug_log"]
    assert debug_log is not None and len(debug_log) > 0

    # 6. Verify Exact Assertions on Production API Report Output
    # Assert Exact Matches (₹74,26,000, ₹33,45,561, ₹96,186, ₹93,410, ₹51,931, ₹48,124, ₹47,524, etc.)
    exact_matches = [d for d in debug_log if d["match_status"] == "EXACT_MATCH"]
    assert len(exact_matches) >= 37

    timing_items = report["items"]
    timing_book_ids = {it["book_entry_id"] for it in timing_items if it["book_entry_id"]}
    timing_bank_ids = {it["bank_txn_id"] for it in timing_items if it["bank_txn_id"]}

    for match_item in exact_matches:
        assert match_item["ledger_transaction_id"] not in timing_book_ids
        assert match_item["bank_transaction_id"] not in timing_bank_ids

    # Assert DATE_MISMATCH
    date_mismatches = [d for d in debug_log if d["match_status"] == "DATE_MISMATCH"]
    assert len(date_mismatches) == 1
    assert date_mismatches[0]["bank_date"] == "2025-03-29"
    assert date_mismatches[0]["ledger_date"] == "2025-03-28"
    assert date_mismatches[0]["bank_amount"] == 25874.0
    assert date_mismatches[0]["ledger_transaction_id"] not in timing_book_ids
    assert date_mismatches[0]["bank_transaction_id"] not in timing_bank_ids

    # Assert AMOUNT_MISMATCH: Bank=₹12,460, Ledger=₹12,400
    amount_mismatches = [d for d in debug_log if d["match_status"] == "AMOUNT_MISMATCH"]
    assert len(amount_mismatches) == 1
    assert amount_mismatches[0]["bank_amount"] == 12460.0, f"Expected Bank Amount 12460.0 but got {amount_mismatches[0]['bank_amount']}"
    assert amount_mismatches[0]["ledger_amount"] == 12400.0, f"Expected Ledger Amount 12400.0 but got {amount_mismatches[0]['ledger_amount']}"
    assert amount_mismatches[0]["ledger_transaction_id"] not in timing_book_ids
    assert amount_mismatches[0]["bank_transaction_id"] not in timing_bank_ids

    # Assert DUPLICATE: Amount MUST match canonical match exactly!
    duplicates = [d for d in debug_log if d["match_status"] == "DUPLICATE"]
    assert len(duplicates) == 1
    dup = duplicates[0]
    assert dup["bank_amount"] == dup["ledger_amount"] == 13511.0
    # Duplicate ledger entries MUST appear in BRS timing_items as a SUBTRACT adjustment
    # (they inflate books so the bridge must subtract them).
    assert dup["ledger_transaction_id"] in timing_book_ids, (
        "Duplicate ledger credit must be in BRS items (SUBTRACT direction) to correct the bridge"
    )
    dup_brs = next(it for it in timing_items if it["book_entry_id"] == dup["ledger_transaction_id"])
    assert dup_brs["direction"] == "subtract", "Duplicate ledger credit BRS item must have direction=subtract"
    assert dup_brs["category"] == "duplicate_ledger_credit"

    # Hard Regression Assert: Bank ₹51,931 (14-Mar) MUST NEVER be marked as DUPLICATE against Ledger ₹12,400!
    for d in duplicates:
        assert not (d["bank_amount"] == 51931.0 and d["ledger_amount"] == 12400.0)

    # Assert LEDGER_ONLY (exclude duplicate BRS items which also have side=book)
    DUPLICATE_CATEGORIES = {"duplicate_ledger_credit", "duplicate_ledger_debit"}
    ledger_only_items = [
        it for it in timing_items
        if it["side"] == "book" and it.get("category") not in DUPLICATE_CATEGORIES
    ]
    assert len(ledger_only_items) == 1

    # Assert BANK_ONLY
    bank_only_items = [it for it in timing_items if it["side"] == "bank"]
    assert len(bank_only_items) == 1
    assert bank_only_items[0]["category"] in ["bank_charge", "UNMATCHED_BANK_TRANSACTION"]

    # Hard Mathematical Invariants:
    # 1. Total In-Scope Bank Transactions = EXACT_MATCH + DATE_MISMATCH + AMOUNT_MISMATCH + GROUP_MATCH + BANK_ONLY
    # 2. Total In-Scope Ledger Transactions = EXACT_MATCH + DATE_MISMATCH + AMOUNT_MISMATCH + GROUP_MATCH + LEDGER_ONLY + DUPLICATE
    n_bank = len(bank_data)
    n_book = len(ledger_rows)

    n_exact = len(exact_matches)
    n_date_mis = len(date_mismatches)
    n_amt_mis = len(amount_mismatches)
    n_group = 0
    n_bank_only = len(bank_only_items)
    n_ledger_only = len(ledger_only_items)
    n_duplicates = len(duplicates)

    assert (n_exact + n_date_mis + n_amt_mis + n_group + n_bank_only) == n_bank, (
        f"Bank bucket sum mismatch: {n_exact}+{n_date_mis}+{n_amt_mis}+{n_group}+{n_bank_only} != {n_bank}"
    )
    assert (n_exact + n_date_mis + n_amt_mis + n_group + n_ledger_only + n_duplicates) == n_book, (
        f"Book bucket sum mismatch: {n_exact}+{n_date_mis}+{n_amt_mis}+{n_group}+{n_ledger_only}+{n_duplicates} != {n_book}"
    )

    # Assert Single-Bucket Invariant: No transaction present in multiple buckets.
    # Exception: DUPLICATE entries are intentionally dual-tracked — they appear in
    # debug_log (as a DUPLICATE classification) AND in timing_items (as a SUBTRACT
    # BRS adjustment).  Exclude them from the uniqueness check for debug_log.
    duplicate_book_ids_set = {d["ledger_transaction_id"] for d in debug_log if d["match_status"] == "DUPLICATE"}
    all_classified_book_ids = set()
    for d in debug_log:
        if d.get("ledger_transaction_id") and d["match_status"] != "DUPLICATE":
            l_id = d["ledger_transaction_id"]
            assert l_id not in all_classified_book_ids, f"Book ID {l_id} appears in multiple buckets!"
            all_classified_book_ids.add(l_id)

    for it in timing_items:
        if it.get("book_entry_id"):
            b_id = it["book_entry_id"]
            # Duplicate BRS items legitimately appear in both debug_log and timing_items
            if b_id in duplicate_book_ids_set:
                continue
            assert b_id not in all_classified_book_ids, f"Timing Book ID {b_id} appears in multiple buckets!"
            all_classified_book_ids.add(b_id)
