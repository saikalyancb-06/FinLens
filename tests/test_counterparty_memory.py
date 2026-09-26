"""Counterparty extraction and the memory that learns from review decisions.

Background — why this layer exists at all. On a real 1,823-row restaurant
statement, 599 rows survived every rule and the ML model. Inspecting them showed
the reason: they are transfers whose only informative content is a proper noun
("KUMAR FISH", "SHOBHA G RENT", "MEYER ORGANICS"). No regex can know what a
supplier sells, and the ML model's median confidence on those rows was 0.167 —
it abstained on 599 of 600 sampled, correctly, because the signal is not in the
text. The information exists only in the account holder's head.

So the design goal is not "classify these automatically". It is "ask the user
once per PARTY instead of once per ROW" — 599 questions collapse to 181, and the
answers persist for every future upload.
"""

import datetime
import uuid

import pytest

from app.categorization.counterparty import extract, extract_key
from app.categorization.counterparty_memory import (
    forget, load_memory, lookup, remember, suggest_merges,
)
from app.models.account import Account
from app.models.counterparty_memory import CounterpartyMemory
from app.models.transaction import Direction, SourceType, Transaction
from tests.conftest import TestingSessionLocal, register_bank_account


# ---------------------------------------------------------------------------
# Extraction — pure string work, no database
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("narration,expected", [
    # NEFT with a full trailing bank name
    ("NEFT-HDFCH25081234567-MEYER ORGANICS PVT LTD-HDFC BANK LTD.",
     "MEYER ORGANICS"),
    # RTGS, no trailing bank
    ("RTGS-HDFCR52026081112-EMMVEE ENERGY PRIVATE LIMITED", "EMMVEE ENERGY"),
    # Internet-banking transfer
    ("EBANK:WIB/1501906475/JAYAPRAKASH SHETTY", "JAYAPRAKASH SHETTY"),
    # IMPS puts a free-text remark after the name; only the name is the party
    ("IMPS/P2A/518912345678/CASA2STAYSPRIVA/PayoutforC2S", "CASA2STAYSPRIVA"),
    # A returned transfer is the same counterparty
    ("RTN:NEFT-BARBZ123-DINAKAR SHETTY-STATE BANK OF INDIA", "DINAKAR SHETTY"),
    # Card batch settlement that names the merchant
    ("BT25112544566229/PRAKASH ASPHALTINGS", "PRAKASH ASPHALTINGS"),
    # Blank intermediate fields, seen on some exports
    ("EBANK:1476898338///KSBCL", "KSBCL"),
    # A bare name with no channel scaffolding at all
    ("KUMAR FISH", "KUMAR FISH"),
])
def test_extract_pulls_the_party_out_of_the_scaffolding(narration, expected):
    assert extract_key(narration) == expected


@pytest.mark.parametrize("narration", [
    "BY CASH",
    "TO CASH 12345",
    "CASH DEPOSIT",
    "Int.Coll:2026-07-01",
    "SERVICE CHARGE FOR JUNE",
    "ATM WDL 4455",
    "BT12",            # a bare reference, no name
    "AC/1234",
])
def test_non_counterparty_narrations_yield_nothing(narration):
    """A bank charge has no counterparty.

    Returning a key here would be worse than returning nothing: "SERVICE" would
    become a memory entry, and a later "SERVICE TAX" row would inherit whatever
    category the user once gave a service charge.
    """
    assert extract(narration) is None


def test_truncated_trailing_bank_names_are_stripped():
    """Fixed-width statement exports cut the line mid-word.

    The same supplier appears as "-CANARA BANK", "-CANARA BA" and "-STA"
    depending on how long the name before it was. If the key depended on where
    the export happened to cut, one supplier would become three memory entries
    and the user would be asked three times.
    """
    keys = {
        extract_key("NEFT-BARBZ1-ARUN KUMAR SHETTY-CANARA BANK"),
        extract_key("NEFT-BARBZ2-ARUN KUMAR SHETTY-CANARA BA"),
        extract_key("NEFT-BARBZ3-ARUN KUMAR SHETTY-CAN"),
    }
    assert keys == {"ARUN KUMAR SHETTY"}


def test_corporate_suffixes_do_not_split_one_supplier_into_several():
    keys = {
        extract_key("NEFT-HDFCH1-MEYER ORGANICS PVT LTD"),
        extract_key("NEFT-HDFCH2-MEYER ORGANICS PRIVATE LIMITED"),
        extract_key("NEFT-HDFCH3-MEYER ORGANICS LIMITE"),   # truncated export
    }
    assert keys == {"MEYER ORGANICS"}


def test_fuzzy_key_collapses_transliteration_drift():
    """Indian names transliterate several ways; the fuzzy key survives that.

    This key is used ONLY to suggest a merge to the user. It is deliberately not
    used to auto-apply a category, because two genuinely different parties can
    share a consonant skeleton and merging them silently books their spend
    together.
    """
    a = extract("EBANK:WIB/1/NARASIMHAIAH CHIKEN")
    b = extract("EBANK:WIB/2/NARASIMHA CHIKEN")
    assert a.key != b.key            # exact keys stay distinct
    assert a.fuzzy_key == b.fuzzy_key


# ---------------------------------------------------------------------------
# Memory — reading and writing decisions
# ---------------------------------------------------------------------------

@pytest.fixture
def auth(client):
    email = f"cpm_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    tok = client.post("/auth/login",
                      json={"email": email, "password": "Password123!"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}
    user_id = uuid.UUID(client.get("/auth/me", headers=headers).json()["id"])
    yield headers, user_id
    db = TestingSessionLocal()
    try:
        db.query(CounterpartyMemory).filter(
            CounterpartyMemory.user_id == user_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_remember_then_lookup_round_trips(auth):
    _headers, user_id = auth
    db = TestingSessionLocal()
    try:
        row = remember(db, user_id,
                       "NEFT-HDFCH25081234567-MEYER ORGANICS PVT LTD-HDFC BANK LTD.",
                       category="Cost of Goods", event_type=None)
        db.commit()
        assert row is not None
        assert row.counterparty_key == "MEYER ORGANICS"

        memory = load_memory(db, user_id)
        # A DIFFERENT narration for the same party resolves to the same decision.
        hit = lookup(memory, "RTGS-ICIC99-MEYER ORGANICS PRIVATE LIMITED-ICICI BANK")
        assert hit is not None
        assert hit.category == "Cost of Goods"
    finally:
        db.close()


def test_a_charge_with_no_counterparty_is_remembered_by_its_shape(auth):
    """A bank charge names nobody, but the decision still has to carry forward.

    On a real statement 695 of 1,823 rows are charges, POS rent, interest and
    cash — no counterparty anywhere. If those decisions were not remembered the
    user would answer the same question on every upload, forever.

    They are keyed on the SHAPE of the narration with reference numbers and
    dates stripped, and recorded as kind='pattern' so the UI can say what kind
    of claim it is.
    """
    _headers, user_id = auth
    db = TestingSessionLocal()
    try:
        row = remember(db, user_id, "SERVICE CHARGE FOR JUNE 2026",
                       category="Bank Fees")
        db.commit()
        assert row is not None
        assert row.kind == "pattern"

        memory = load_memory(db, user_id)
        # A different month, a different reference — same charge.
        hit = lookup(memory, "SERVICE CHARGE FOR SEPTEMBER 2026")
        assert hit is not None
        assert hit.category == "Bank Fees"
        assert hit.kind == "pattern"
    finally:
        db.close()


def test_a_shape_key_does_not_swallow_unrelated_narrations(auth):
    """The risk this design carries, pinned.

    A shape key that is too loose books unrelated rows together. "SERVICE
    CHARGE" must not match "SERVICE TAX PAYMENT" or a payment to a supplier
    whose name happens to contain the word.
    """
    _headers, user_id = auth
    db = TestingSessionLocal()
    try:
        remember(db, user_id, "SERVICE CHARGE FOR JUNE 2026", category="Bank Fees")
        db.commit()
        memory = load_memory(db, user_id)
        for unrelated in [
            "SERVICE TAX PAYMENT 4402",
            "NEFT-HDFCH1-SERVICE MASTERS PVT LTD-HDFC BANK LTD.",
            "LEDGER FOLIO CHARGES - CC/OD",
            "POSRENT_JUN25_TID_65100728",
        ]:
            assert lookup(memory, unrelated) is None, unrelated
    finally:
        db.close()


def test_a_narration_with_nothing_but_a_reference_is_not_remembered(auth):
    """Some rows have no counterparty AND no shape.

    "A00021802260044311" is a reference number and nothing else. Inventing a key
    for it would put unrelated rows in one bucket, which is worse than leaving
    it for individual review.
    """
    _headers, user_id = auth
    db = TestingSessionLocal()
    try:
        assert remember(db, user_id, "A00021802260044311",
                        category="Bank Fees") is None
        db.commit()
        assert load_memory(db, user_id) == {}
    finally:
        db.close()


def test_repeating_a_decision_confirms_it_but_changing_it_replaces_it(auth):
    """A correction must take effect immediately, not be outvoted by history.

    If a changed decision merely incremented a counter, a user who mislabelled a
    supplier four times would need four corrections to undo it.
    """
    _headers, user_id = auth
    db = TestingSessionLocal()
    try:
        remember(db, user_id, "EBANK:WIB/1/KUMAR FISH", category="Cost of Goods")
        remember(db, user_id, "EBANK:WIB/2/KUMAR FISH", category="Cost of Goods")
        db.commit()
        row = db.query(CounterpartyMemory).filter(
            CounterpartyMemory.user_id == user_id).one()
        assert row.times_confirmed == 2

        remember(db, user_id, "EBANK:WIB/3/KUMAR FISH", category="Food & Dining")
        db.commit()
        db.refresh(row)
        assert row.category == "Food & Dining"
        assert row.times_confirmed == 1
    finally:
        db.close()


def test_memory_is_scoped_to_one_user(auth, client):
    """SHETTY is a supplier to one account holder and a friend to another."""
    _headers, user_a = auth
    email = f"cpm2_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    tok = client.post("/auth/login",
                      json={"email": email, "password": "Password123!"}).json()["access_token"]
    user_b = uuid.UUID(
        client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()["id"])

    db = TestingSessionLocal()
    try:
        remember(db, user_a, "EBANK:WIB/1/DINAKAR SHETTY", category="Cost of Goods")
        db.commit()
        assert lookup(load_memory(db, user_b), "EBANK:WIB/9/DINAKAR SHETTY") is None
        assert lookup(load_memory(db, user_a), "EBANK:WIB/9/DINAKAR SHETTY") is not None
    finally:
        db.query(CounterpartyMemory).filter(
            CounterpartyMemory.user_id == user_b).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_merge_suggestions_are_reported_never_applied(auth):
    _headers, user_id = auth
    db = TestingSessionLocal()
    try:
        remember(db, user_id, "EBANK:WIB/1/NARASIMHAIAH CHIKEN", category="Cost of Goods")
        remember(db, user_id, "EBANK:WIB/2/NARASIMHA CHIKEN", category="Cost of Goods")
        db.commit()

        suggestions = suggest_merges(db, user_id)
        assert len(suggestions) == 1
        assert {m["counterparty_key"] for m in suggestions[0]["members"]} == {
            "NARASIMHAIAH CHIKEN", "NARASIMHA CHIKEN"}
        assert suggestions[0]["categories_agree"] is True

        # Still two separate entries — nothing was merged behind the user's back.
        assert db.query(CounterpartyMemory).filter(
            CounterpartyMemory.user_id == user_id).count() == 2
    finally:
        db.close()


def test_forget_removes_only_the_named_mapping(auth):
    _headers, user_id = auth
    db = TestingSessionLocal()
    try:
        remember(db, user_id, "EBANK:WIB/1/KUMAR FISH", category="Cost of Goods")
        remember(db, user_id, "EBANK:WIB/1/SHIVKUMAR VEG", category="Cost of Goods")
        db.commit()
        assert forget(db, user_id, "KUMAR FISH") is True
        db.commit()
        # Asserted against the TABLE, not against load_memory's keys.
        # load_memory returns a lookup INDEX: alongside each stored key it
        # carries the OCR-repaired spelling and the consonant skeleton, so one
        # decision reaches the variants the bank prints on other statements.
        # Counting its keys therefore counts index entries, not mappings.
        remaining = {
            r.counterparty_key
            for r in db.query(CounterpartyMemory)
            .filter(CounterpartyMemory.user_id == user_id).all()
        }
        assert remaining == {"SHIVKUMAR VEG"}

        # And the forgotten one is genuinely unreachable, by any spelling.
        index = load_memory(db, user_id)
        assert lookup(index, "EBANK:WIB/9/KUMAR FISH") is None
        assert lookup(index, "EBANK:WIB/9/SHIVKUMAR VEG") is not None

        assert forget(db, user_id, "KUMAR FISH") is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The API surface the review UI drives
# ---------------------------------------------------------------------------

def _seed_counterparty_rows(user_id, account_id):
    """Twelve rows for one supplier and three for another, all uncategorised."""
    db = TestingSessionLocal()
    try:
        for i in range(12):
            db.add(Transaction(
                id=uuid.uuid4(), user_id=user_id, account_id=account_id,
                direction=Direction.DEBIT, debit_paise=5000_00 + i,
                balance_paise=100000_00,
                txn_date=datetime.date(2026, 7, 1) + datetime.timedelta(days=i),
                narration_raw=f"EBANK:WIB/15019{i:05d}/KUMAR FISH",
                narration_clean=f"EBANK:WIB/15019{i:05d}/KUMAR FISH",
                source_type=SourceType.STATEMENT, booked_currency="INR",
                category_id=None,
            ))
        for i in range(3):
            db.add(Transaction(
                id=uuid.uuid4(), user_id=user_id, account_id=account_id,
                direction=Direction.DEBIT, debit_paise=9000_00 + i,
                balance_paise=100000_00,
                txn_date=datetime.date(2026, 7, 20) + datetime.timedelta(days=i),
                narration_raw=f"NEFT-HDFCH{i}-MEYER ORGANICS PVT LTD-HDFC BANK LTD.",
                narration_clean=f"NEFT-HDFCH{i}-MEYER ORGANICS PVT LTD-HDFC BANK LTD.",
                source_type=SourceType.STATEMENT, booked_currency="INR",
                category_id=None,
            ))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def seeded(client, auth):
    headers, user_id = auth
    acct = register_bank_account(client, headers,
                                 account_number=f"5020{uuid.uuid4().int % 10**8:08d}")
    db = TestingSessionLocal()
    try:
        account_id = db.query(Account).filter(
            Account.user_id == user_id).first().id
    finally:
        db.close()
    _seed_counterparty_rows(user_id, account_id)
    yield headers, user_id, account_id
    db = TestingSessionLocal()
    try:
        db.query(Transaction).filter(
            Transaction.user_id == user_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_review_queue_groups_pending_rows_by_counterparty(client, seeded):
    """Fifteen review items become two decisions."""
    headers, _user_id, _acct = seeded
    res = client.get("/v1/review-queue/counterparties", headers=headers)
    assert res.status_code == 200, res.text
    groups = {g["counterparty_key"]: g for g in res.json()}

    assert groups["KUMAR FISH"]["transaction_count"] == 12
    assert groups["MEYER ORGANICS"]["transaction_count"] == 3
    # Sorted by leverage: the party that clears the most rows comes first.
    assert res.json()[0]["counterparty_key"] == "KUMAR FISH"
    # Rupees, not paise — the storage unit must not leak into the API.
    assert groups["KUMAR FISH"]["total_debit"] == pytest.approx(60000.66, abs=0.05)


def test_categorising_a_counterparty_clears_all_its_pending_rows(client, seeded):
    headers, user_id, _acct = seeded
    res = client.post("/v1/review-queue/counterparties/KUMAR FISH",
                      headers=headers, json={"category": "Cost of Goods"})
    assert res.status_code == 200, res.text
    assert res.json()["transactions_updated"] == 12

    # The other supplier is untouched — a bulk action is scoped to its party.
    remaining = client.get("/v1/review-queue/counterparties", headers=headers).json()
    assert {g["counterparty_key"] for g in remaining} == {"MEYER ORGANICS"}

    # And the decision was learned, so the next upload will not ask again.
    memory = client.get("/v1/review-queue/memory", headers=headers).json()
    assert [m["counterparty_key"] for m in memory] == ["Kumar Fish"] or \
           any(m["counterparty_key"] == "KUMAR FISH" for m in memory)


def test_reviewing_one_row_teaches_the_rest(client, seeded):
    """The core promise: answer once, and the other eleven rows follow."""
    headers, _user_id, _acct = seeded
    items = client.get("/v1/review-queue?limit=200", headers=headers).json()
    target = next(i for i in items if "KUMAR FISH" in i["narration"])

    res = client.patch(f"/v1/review-queue/{target['transaction_id']}",
                       headers=headers, json={"category": "Cost of Goods"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["counterparty"] == "Kumar Fish"
    assert body["also_updated"] == 11

    left = client.get("/v1/review-queue/counterparties", headers=headers).json()
    assert {g["counterparty_key"] for g in left} == {"MEYER ORGANICS"}


def test_apply_to_similar_false_categorises_only_the_chosen_row(client, seeded):
    """The bulk behaviour is a default, not a lock-in."""
    headers, _user_id, _acct = seeded
    items = client.get("/v1/review-queue?limit=200", headers=headers).json()
    target = next(i for i in items if "KUMAR FISH" in i["narration"])

    res = client.patch(f"/v1/review-queue/{target['transaction_id']}", headers=headers,
                       json={"category": "Cost of Goods", "apply_to_similar": False})
    assert res.status_code == 200, res.text
    assert res.json()["also_updated"] == 0

    left = {g["counterparty_key"]: g for g in
            client.get("/v1/review-queue/counterparties", headers=headers).json()}
    assert left["KUMAR FISH"]["transaction_count"] == 11


def test_truncated_names_are_suggested_as_one_party(auth):
    """A fixed-width export splits one supplier into two entries.

    The bank records this restaurant's payment aggregator as both
    "PHONEPE LIMITED-PAYMENT AGGR" and "PHONEPE PRIVATE LIMITED-PAYM". The
    fuzzy key cannot see this — the shorter form is missing letters rather than
    spelling them differently — so a prefix relationship is checked separately.
    """
    _headers, user_id = auth
    db = TestingSessionLocal()
    try:
        remember(db, user_id, "NEFT-AXNPN1-PHONEPE PRIVATE LIMITED-PAYM",
                 category="Sales Income")
        remember(db, user_id, "NEFT-AXNPN2-PHONEPE LIMITED-PAYMENT AGGR",
                 category="Sales Income")
        db.commit()

        suggestions = [s for s in suggest_merges(db, user_id)
                       if s["reason"] == "truncation"]
        assert len(suggestions) == 1
        assert suggestions[0]["categories_agree"] is True
    finally:
        db.close()


def test_a_short_name_is_not_treated_as_a_truncation_of_a_longer_one(auth):
    """SHETTY is not a truncated SHETTY TRADERS.

    Below eight characters a prefix match is coincidence. Reporting it would
    train the user to click through merge suggestions without reading them,
    which is how two real suppliers end up booked together.
    """
    _headers, user_id = auth
    db = TestingSessionLocal()
    try:
        remember(db, user_id, "EBANK:WIB/1/SHETTY", category="Cost of Goods")
        remember(db, user_id, "EBANK:WIB/2/SHETTY TRADERS", category="Rent & Leases")
        db.commit()
        assert [s for s in suggest_merges(db, user_id)
                if s["reason"] == "truncation"] == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Field naming
#
# The app calls this axis "category" everywhere a user can see it. "purpose" was
# the older internal name and is still accepted on input, because an integration
# written against the old field must not break on an upgrade that was, from the
# user's point of view, a relabelling.
# ---------------------------------------------------------------------------

def test_category_is_the_field_name_everywhere_a_client_reads(client, seeded):
    headers, _user_id, _acct = seeded

    summary = client.get("/v1/review-queue/summary", headers=headers).json()
    assert "available_categories" in summary
    assert "Sales Income" in summary["available_categories"]
    # The legacy personal-finance taxonomy must not be offered: filing a
    # restaurant's supplier payment under "Groceries" is not a valid outcome.
    assert "Groceries" not in summary["available_categories"]

    listed = client.get("/v1/review-queue/categories", headers=headers).json()
    assert "Cost of Goods" in listed
    assert "Entertainment" not in listed

    groups = client.get("/v1/review-queue/counterparties", headers=headers).json()
    assert "suggested_category" in groups[0]
    assert "known_category" in groups[0]


def test_the_old_purpose_field_is_still_accepted_on_input(client, seeded):
    """An existing integration must keep working after the rename."""
    headers, _user_id, _acct = seeded
    res = client.post("/v1/review-queue/counterparties/KUMAR FISH",
                      headers=headers, json={"purpose": "Cost of Goods"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["category"] == "Cost of Goods"
    # Both names are returned, carrying the same value.
    assert body["purpose"] == body["category"]
    assert body["transactions_updated"] == 12


def test_category_wins_when_a_client_sends_both(client, seeded):
    headers, _user_id, _acct = seeded
    items = client.get("/v1/review-queue?limit=200", headers=headers).json()
    target = next(i for i in items if "MEYER ORGANICS" in i["narration"])

    res = client.patch(f"/v1/review-queue/{target['transaction_id']}", headers=headers,
                       json={"category": "Cost of Goods", "purpose": "Travel"})
    assert res.status_code == 200, res.text
    assert res.json()["new_category"] == "Cost of Goods"


def test_a_missing_category_is_reported_in_the_users_vocabulary(client, seeded):
    headers, _user_id, _acct = seeded
    items = client.get("/v1/review-queue?limit=200", headers=headers).json()
    res = client.patch(f"/v1/review-queue/{items[0]['transaction_id']}",
                       headers=headers, json={})
    assert res.status_code == 400
    assert "category is required" in res.json()["detail"]
