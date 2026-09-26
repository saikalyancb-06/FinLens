"""End-to-end tests for POST /v1/classify — the rules-in, categories-out service.

These drive the real app through TestClient with a real API key and a real
Postgres session. They assert the CLASSIFICATION each row received, not just a
status code: a test that checked for 200 would pass against a service that put
every row in the same bucket, which is precisely the failure mode that matters
here.

Two invariants are worth naming because they are what a caller is trusting:

* a rule can never alter money, dates or direction (`test_rule_cannot_alter_money`);
* ranking is by priority and is reproducible, never first-match
  (`test_priority_beats_document_order`).
"""
import io
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.b2b import auth as b2b_auth
from app.b2b import rules as rulespec
from app.b2b.models import UsageRecord
from main import app
from tests.conftest import TestingSessionLocal


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _make_client_and_key(scopes=None):
    db = TestingSessionLocal()
    try:
        slug = f"sisterproduct_{uuid.uuid4().hex[:8]}"
        c = b2b_auth.create_client(db, name="Sister Product", slug=slug,
                                   contact_email="dev@sister.example")
        c.rate_limit_per_minute = 10000
        c.rate_limit_per_day = 100000
        c.rate_limit_per_month = 1000000
        db.commit()
        kwargs = {"name": "classify-test"}
        if scopes is not None:
            kwargs["scopes"] = scopes
        issued = b2b_auth.issue_key(db, c, **kwargs)
        secret = issued.secret if hasattr(issued, "secret") else issued[1]
        db.commit()
        return c.id, secret
    finally:
        db.close()


@pytest.fixture
def headers():
    _, secret = _make_client_and_key()
    return {"Authorization": f"Bearer {secret}"}


# A statement whose every figure is hand-checkable. One row per classification
# case the tests below exercise.
_ROWS = [
    # date,        narration,                        debit,  credit
    ("2026-03-01", "SALARY CREDIT MARCH PAYROLL",       0,   78000),
    ("2026-03-02", "UPI-SWIGGY-ORDER-8821",           450,       0),
    ("2026-03-03", "UPI TRANSFER TO SWIGGY WALLET",   900,       0),
    ("2026-03-04", "UPI-NAMMAYATRI-99112233",         120,       0),
    ("2026-03-05", "NEFT-HDFC0001234-MEYER ORGANICS", 250000,    0),
    ("2026-03-06", "DOMINOS PIZZA REFUND CREDIT",       0,     300),
    ("2026-03-07", "SERVICE CHARGE AMC ANNUAL",        590,      0),
    ("2026-03-08", "SOMETHING COMPLETELY UNKNOWN",      42,      0),
]


def _statement_csv() -> bytes:
    lines = ["Date,Narration,Debit,Credit,Balance"]
    balance = 100000.0
    for date, narr, debit, credit in _ROWS:
        balance += credit - debit
        lines.append(f"{date},{narr},{debit or ''},{credit or ''},{balance:.2f}")
    return ("\n".join(lines) + "\n").encode()


#: The ruleset the happy-path tests use. Deliberately exercises every clause
#: type: any_of, all_of, none_of, regex, direction, amount bounds.
_RULES = {
    "version": "test-1",
    "default_category": "Unclassified",
    "fallback": "none",
    "rules": [
        {"id": "payroll", "category": "Payroll", "priority": 100,
         "match": {"any_of": ["SALARY", "PAYROLL"], "direction": "credit",
                   "min_amount": 10000},
         "set": {"category_path": "Expenses > Payroll", "counterparty": "Staff"}},
        {"id": "food", "category": "Food", "priority": 100,
         "match": {"any_of": ["SWIGGY", "DOMINOS"], "none_of": ["REFUND"]}},
        # Higher priority than `food`, so an explicit transfer intent wins even
        # though the merchant also matches.
        {"id": "wallet-transfer", "category": "Transfer", "priority": 120,
         "match": {"all_of": ["TRANSFER"], "any_of": ["SWIGGY", "PAYTM"]}},
        {"id": "rides", "category": "Transport", "priority": 100,
         "match": {"any_of": ["NAMMAYATRI", "UBER"]}},
        {"id": "vendor-neft", "category": "Vendor Payment", "priority": 80,
         "match": {"regex": r"^NEFT-[A-Z]{4}\d{7}", "min_amount": 100000}},
        {"id": "bank-fees", "category": "Bank Charges", "priority": 90,
         "match": {"any_of": ["SERVICE CHARGE", "AMC"], "none_of": ["REVERSAL"],
                   "max_amount": 2000}},
        {"id": "never-fires", "category": "Nothing", "priority": 10,
         "match": {"any_of": ["ZZZ_NO_SUCH_MERCHANT_ZZZ"]}},
    ],
}


def _post(client, headers, rules=None, content=None, filename="statement.csv",
          ctype="text/csv", **form):
    body = _statement_csv() if content is None else content
    data = dict(form)
    if rules is not None:
        data["rules"] = rules if isinstance(rules, str) else json.dumps(rules)
    return client.post(
        "/v1/classify", headers=headers, data=data,
        files={"file": (filename, io.BytesIO(body), ctype)})


def _by_description(payload):
    """Map narration -> its row, for assertions that read like the statement."""
    return {row["description"]: row for row in payload["data"]["transactions"]}


# ------------------------------------------------------------------ the schema

def test_schema_is_public_and_self_describing(client):
    """An integrator must be able to read the contract without a key."""
    res = client.get("/v1/classify/schema")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["limits"]["max_rules"] == rulespec.MAX_RULES
    assert body["limits"]["max_regex_length"] == rulespec.MAX_REGEX_LENGTH
    assert set(body["top_level"]["fallback"]["values"]) == set(rulespec.FALLBACK_MODES)
    # The settable-field whitelist is the contract's most important guarantee,
    # so it has to be visible in the published schema.
    assert set(body["rule"]["set"]["settable_fields"]) == set(rulespec.SETTABLE_FIELDS)
    for money_field in ("amount", "debit", "credit", "balance", "date", "direction"):
        assert money_field not in body["rule"]["set"]["settable_fields"]


def test_schema_example_is_itself_valid(client):
    """The documented example must be accepted by the validator.

    A schema endpoint whose own example is rejected is worse than no example.
    """
    example = client.get("/v1/classify/schema").json()["example"]
    parsed = rulespec.parse_ruleset(example)
    assert len(parsed.rules) == len(example["rules"])


# ------------------------------------------------------------------------ auth

def test_requires_a_key(client):
    res = _post(client, {}, rules=_RULES)
    assert res.status_code == 401
    assert res.json()["error"]["code"] in ("MISSING_API_KEY", "INVALID_API_KEY")


def test_existing_analyze_key_is_accepted(client):
    """Keys issued before this endpoint existed carry analyze:write only."""
    _, secret = _make_client_and_key(scopes="analyze:write,analyze:read")
    res = _post(client, {"Authorization": f"Bearer {secret}"}, rules=_RULES)
    assert res.status_code == 200, res.text


def test_narrow_classify_only_key_is_accepted(client):
    """An operator can issue a key that may classify and nothing else."""
    _, secret = _make_client_and_key(scopes="classify:write")
    hdrs = {"Authorization": f"Bearer {secret}"}
    assert _post(client, hdrs, rules=_RULES).status_code == 200
    # ...and that key must NOT be able to reach the full analysis endpoint.
    res = client.post("/v1/analyze", headers=hdrs,
                      files={"file": ("s.csv", io.BytesIO(_statement_csv()),
                                      "text/csv")})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "INSUFFICIENT_SCOPE"


def test_read_only_key_is_refused(client):
    _, secret = _make_client_and_key(scopes="analyze:read")
    res = _post(client, {"Authorization": f"Bearer {secret}"}, rules=_RULES)
    assert res.status_code == 403
    body = res.json()["error"]
    assert body["code"] == "INSUFFICIENT_SCOPE"
    assert set(body["detail"]["required_any_of"]) == {"classify:write", "analyze:write"}


# --------------------------------------------------------------- happy path

def test_every_row_is_classified_as_the_rules_specify(client, headers):
    res = _post(client, headers, rules=_RULES)
    assert res.status_code == 200, res.text
    payload = res.json()
    rows = _by_description(payload)

    assert len(rows) == len(_ROWS)

    expected = {
        "SALARY CREDIT MARCH PAYROLL":     ("Payroll", "payroll"),
        "UPI-SWIGGY-ORDER-8821":           ("Food", "food"),
        # `wallet-transfer` at priority 120 outranks `food` at 100.
        "UPI TRANSFER TO SWIGGY WALLET":   ("Transfer", "wallet-transfer"),
        # Matched inside a concatenated token.
        "UPI-NAMMAYATRI-99112233":         ("Transport", "rides"),
        "NEFT-HDFC0001234-MEYER ORGANICS": ("Vendor Payment", "vendor-neft"),
        # `none_of: [REFUND]` vetoes `food`, so this falls to the default.
        "DOMINOS PIZZA REFUND CREDIT":     ("Unclassified", None),
        "SERVICE CHARGE AMC ANNUAL":       ("Bank Charges", "bank-fees"),
        "SOMETHING COMPLETELY UNKNOWN":    ("Unclassified", None),
    }
    for description, (category, rule_id) in expected.items():
        row = rows[description]
        assert row["category"] == category, f"{description} -> {row['category']}"
        assert row["classification"]["rule_id"] == rule_id, description


def test_summary_counts_are_correct(client, headers):
    payload = _post(client, headers, rules=_RULES).json()
    s = payload["summary"]

    assert s["transaction_count"] == 8
    # Six rows matched a rule; two fell through to default_category. A default
    # still counts as classified, because the row carries a category.
    assert s["classified"] == 8
    assert s["unclassified"] == 0
    assert s["by_method"] == {"rule": 6, "default": 2}
    assert s["by_category"]["Unclassified"] == 2
    assert s["by_category"]["Food"] == 1
    assert s["rule_count"] == 7
    assert s["rules_that_matched"] == 6
    assert s["rules_that_never_matched"] == ["never-fires"]


def test_rule_usage_reports_the_rules_that_never_fired(client, headers):
    """A rule matching nothing is usually a typo, and is invisible unless said."""
    payload = _post(client, headers, rules=_RULES).json()
    usage = {e["rule_id"]: e for e in payload["summary"]["rule_usage"]}
    assert set(usage) == {r["id"] for r in _RULES["rules"]}
    assert usage["never-fires"]["matched"] == 0
    assert usage["food"]["matched"] == 1
    assert usage["food"]["category"] == "Food"
    assert usage["food"]["priority"] == 100


def test_unmatched_samples_help_write_the_next_rule(client, headers):
    payload = _post(client, headers, rules=_RULES).json()
    samples = payload["summary"]["unmatched_samples"]
    assert "SOMETHING COMPLETELY UNKNOWN" in samples
    assert "DOMINOS PIZZA REFUND CREDIT" in samples
    # A classified row must never appear here.
    assert "UPI-SWIGGY-ORDER-8821" not in samples


def test_unmatched_samples_can_be_suppressed(client, headers):
    payload = _post(client, headers, rules=_RULES,
                    include_unmatched_samples="false").json()
    assert "unmatched_samples" not in payload["summary"]


def test_metadata_echoes_the_ruleset_version(client, headers):
    payload = _post(client, headers, rules=_RULES).json()
    meta = payload["metadata"]
    assert meta["ruleset_version"] == "test-1"
    assert meta["rule_count"] == 7
    assert meta["fallback"] == "none"
    assert meta["detected_format"] == "csv"
    assert meta["transaction_count"] == 8


def test_include_transactions_false_keeps_the_summary(client, headers):
    payload = _post(client, headers, rules=_RULES,
                    include_transactions="false").json()
    assert payload["data"]["transactions"] == []
    assert payload["data"]["transactions_omitted"] is True
    # The whole point: the aggregate is still computed over every row.
    assert payload["summary"]["transaction_count"] == 8
    assert payload["summary"]["by_method"] == {"rule": 6, "default": 2}


# ------------------------------------------------------- the two invariants

def test_rule_cannot_alter_money(client, headers):
    """A rule may label a row. It may never change what the statement said."""
    payload = _post(client, headers, rules=_RULES).json()
    rows = _by_description(payload)
    for date, narr, debit, credit in _ROWS:
        row = rows[narr]
        assert row["amount"] == float(debit or credit), narr
        assert row["type"] == ("DEBIT" if debit else "CREDIT"), narr
        assert row["date"] == date, narr


def test_set_block_rejects_any_attempt_to_write_money(client, headers):
    for forbidden in ({"amount": 1}, {"debit": 0}, {"balance": 5},
                      {"date": "2026-01-01"}, {"direction": "credit"},
                      {"debit_paise": 100}):
        rules = {"rules": [{"id": "x", "category": "C",
                            "match": {"any_of": ["SWIGGY"]},
                            "set": forbidden}]}
        res = _post(client, headers, rules=rules)
        assert res.status_code == 400, f"{forbidden} was not rejected"
        err = res.json()["error"]
        assert err["code"] == "INVALID_RULES"
        assert list(forbidden)[0] in err["detail"]["unknown_keys"]


def test_priority_beats_document_order(client, headers):
    """Ranked, not first-match: a later high-priority rule still wins."""
    rules = {"rules": [
        {"id": "first-but-weak", "category": "Weak", "priority": 10,
         "match": {"any_of": ["SWIGGY"]}},
        {"id": "last-but-strong", "category": "Strong", "priority": 200,
         "match": {"any_of": ["SWIGGY"]}},
    ], "default_category": "None"}
    payload = _post(client, headers, rules=rules).json()
    row = _by_description(payload)["UPI-SWIGGY-ORDER-8821"]
    assert row["category"] == "Strong"
    assert row["classification"]["rule_id"] == "last-but-strong"


def test_ties_break_on_position_and_are_reported_ambiguous(client, headers):
    """Two rules, same priority, different category = a ruleset fault to surface."""
    rules = {"rules": [
        {"id": "alpha", "category": "Alpha", "priority": 50,
         "match": {"any_of": ["SWIGGY"]}},
        {"id": "beta", "category": "Beta", "priority": 50,
         "match": {"any_of": ["SWIGGY"]}},
    ], "default_category": "None"}
    payload = _post(client, headers, rules=rules).json()
    row = _by_description(payload)["UPI-SWIGGY-ORDER-8821"]
    # Earlier position wins, deterministically.
    assert row["category"] == "Alpha"
    assert row["classification"]["ambiguous"] is True
    assert row["classification"]["runner_up_rule_id"] == "beta"
    assert payload["summary"]["ambiguous_count"] >= 1


def test_same_priority_same_category_is_not_ambiguous(client, headers):
    """Two rules agreeing is not a conflict, and must not be reported as one."""
    rules = {"rules": [
        {"id": "a", "category": "Food", "priority": 50,
         "match": {"any_of": ["SWIGGY"]}},
        {"id": "b", "category": "Food", "priority": 50,
         "match": {"any_of": ["ORDER"]}},
    ], "default_category": "None"}
    payload = _post(client, headers, rules=rules).json()
    row = _by_description(payload)["UPI-SWIGGY-ORDER-8821"]
    assert row["category"] == "Food"
    assert row["classification"].get("ambiguous") is not True
    assert payload["summary"]["ambiguous_count"] == 0


# --------------------------------------------------------------- clause types

def test_stop_gives_opt_in_first_match(client, headers):
    rules = {"rules": [
        {"id": "short-circuit", "category": "Stopped", "priority": 1,
         "stop": True, "match": {"any_of": ["SWIGGY"]}},
        {"id": "would-have-won", "category": "Loser", "priority": 999,
         "match": {"any_of": ["SWIGGY"]}},
    ], "default_category": "None"}
    payload = _post(client, headers, rules=rules).json()
    row = _by_description(payload)["UPI-SWIGGY-ORDER-8821"]
    assert row["category"] == "Stopped"
    assert "stopped here" in row["classification"]["explanation"]


def test_catch_all_requires_being_explicit(client, headers):
    """A rule with no conditions claims every row, so it must say so."""
    res = _post(client, headers, rules={"rules": [
        {"id": "oops", "category": "Everything"}]})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "INVALID_RULES"
    assert "catch_all" in res.json()["error"]["message"]

    ok = _post(client, headers, rules={"rules": [
        {"id": "deliberate", "category": "Everything", "priority": 0,
         "catch_all": True}]})
    assert ok.status_code == 200
    payload = ok.json()
    assert payload["summary"]["by_category"] == {"Everything": 8}


def test_none_of_alone_must_be_explicit(client, headers):
    """`none_of` with no positive condition is a catch-all in disguise."""
    res = _post(client, headers, rules={"rules": [
        {"id": "inverted", "category": "NotFood", "match": {"none_of": ["SWIGGY"]}}]})
    assert res.status_code == 400
    assert "catch_all" in res.json()["error"]["message"]


def test_amount_and_date_and_direction_gates(client, headers):
    rules = {"rules": [
        {"id": "big-debits", "category": "Large", "priority": 50,
         "match": {"direction": "debit", "min_amount": 1000}},
        {"id": "early-march", "category": "Early", "priority": 60,
         "match": {"max_date": "2026-03-02"}},
    ], "default_category": "Other"}
    payload = _post(client, headers, rules=rules).json()
    rows = _by_description(payload)
    # 1 Mar salary is a credit, and also within max_date -> Early wins on date.
    assert rows["SALARY CREDIT MARCH PAYROLL"]["category"] == "Early"
    # 2 Mar, 450 debit: within max_date, under min_amount -> Early.
    assert rows["UPI-SWIGGY-ORDER-8821"]["category"] == "Early"
    # 5 Mar, 250000 debit: past max_date, over min_amount -> Large.
    assert rows["NEFT-HDFC0001234-MEYER ORGANICS"]["category"] == "Large"
    # 8 Mar, 42 debit: neither.
    assert rows["SOMETHING COMPLETELY UNKNOWN"]["category"] == "Other"


def test_min_amount_zero_is_honoured_not_dropped(client, headers):
    """0 is a real bound. The internal money helper maps 0 to None, and using it
    here would have turned `min_amount: 0` into "no bound at all"."""
    parsed = rulespec.parse_ruleset({"rules": [
        {"id": "z", "category": "C", "match": {"min_amount": 0, "direction": "debit"}}]})
    assert parsed.rules[0].match.min_amount_paise == 0


def test_set_block_stamps_descriptive_fields(client, headers):
    rules = {"rules": [
        {"id": "food", "category": "Food", "match": {"any_of": ["SWIGGY"]},
         "set": {"category_path": "Food > Delivery > Swiggy",
                 "counterparty": "Swiggy", "merchant": "Swiggy",
                 "flow_type": "OUTFLOW", "tags": ["discretionary", "food"]}}],
        "default_category": "Other"}
    payload = _post(client, headers, rules=rules).json()
    row = _by_description(payload)["UPI-SWIGGY-ORDER-8821"]
    assert row["category_path"] == "Food > Delivery > Swiggy"
    assert row["counterparty"] == "Swiggy"
    assert row["merchant"] == "Swiggy"
    assert row["flow_type"] == "OUTFLOW"
    assert row["tags"] == ["discretionary", "food"]


def test_category_path_defaults_to_the_category(client, headers):
    """A caller grouping on category_path should not get nulls for half its rows."""
    rules = {"rules": [{"id": "food", "category": "Food",
                        "match": {"any_of": ["SWIGGY"]}}],
             "default_category": "Other"}
    payload = _post(client, headers, rules=rules).json()
    assert _by_description(payload)["UPI-SWIGGY-ORDER-8821"]["category_path"] == "Food"


def test_categories_are_opaque_and_not_forced_onto_our_taxonomy(client, headers):
    """The calling product has its own vocabulary; we must not validate it."""
    rules = {"rules": [
        {"id": "weird", "category": "GL-4100-COGS-RAW-MATERIAL",
         "match": {"any_of": ["SWIGGY"]}}], "default_category": "GL-9999"}
    payload = _post(client, headers, rules=rules).json()
    rows = _by_description(payload)
    assert rows["UPI-SWIGGY-ORDER-8821"]["category"] == "GL-4100-COGS-RAW-MATERIAL"
    assert rows["SOMETHING COMPLETELY UNKNOWN"]["category"] == "GL-9999"


def test_no_default_leaves_category_null_and_flags_review(client, headers):
    rules = {"rules": [{"id": "food", "category": "Food",
                        "match": {"any_of": ["SWIGGY"]}}]}
    payload = _post(client, headers, rules=rules).json()
    row = _by_description(payload)["SOMETHING COMPLETELY UNKNOWN"]
    assert row["category"] is None
    assert row["requires_review"] is True
    assert row["classification"]["method"] == "none"
    # `food` matches BOTH SWIGGY rows (the order and the wallet transfer), so
    # two rows are classified and the remaining six are not.
    assert payload["summary"]["unclassified"] == 6
    assert payload["summary"]["classified"] == 2


def test_no_stale_confidence_from_the_internal_classifier(client, headers):
    """A caller-rule category must not carry the LEGACY engine's confidence.

    The shared parser path runs the internal rule/ML engine on the way through
    and leaves its `final_confidence` on the row
    (app/b2b/parsers/base.py). `to_api()` publishes that field, so before this
    was fixed a row classified by a caller rule came back as
    `{"category": "Food & Beverage", "category_confidence": 0.97}` — a
    confidence in a category the rule had already replaced. A number that looks
    computed but describes something discarded is worse than no number.
    """
    payload = _post(client, headers, rules=_RULES).json()
    for row in payload["data"]["transactions"]:
        method = row["classification"]["method"]
        if method in ("rule", "default", "none"):
            assert row["category_confidence"] is None, (
                f"{row['description']} ({method}) carries a stale confidence "
                f"{row['category_confidence']!r}")
            assert "confidence" not in row["classification"]


def test_builtin_fallback_does_report_a_real_confidence(client, headers):
    """The one case where a probability genuinely exists, it is passed through."""
    rules = {"rules": [{"id": "food", "category": "MyFood",
                        "match": {"any_of": ["SWIGGY"]}}],
             "fallback": "builtin"}
    payload = _post(client, headers, rules=rules).json()
    builtin = [r for r in payload["data"]["transactions"]
               if r["classification"]["method"] == "builtin"]
    assert builtin, "expected at least one built-in decision"
    for row in builtin:
        conf = row["classification"]["confidence"]
        assert 0.0 < conf <= 1.0
        assert row["category_confidence"] == conf



# ------------------------------------------------------------------- fallback

def test_builtin_fallback_classifies_what_the_rules_missed(client, headers):
    """With fallback=builtin our own engine fills the gaps, clearly labelled."""
    rules = {"rules": [{"id": "food", "category": "MyFood",
                        "match": {"any_of": ["SWIGGY"]}}],
             "fallback": "builtin"}
    payload = _post(client, headers, rules=rules).json()
    rows = _by_description(payload)

    assert rows["UPI-SWIGGY-ORDER-8821"]["classification"]["method"] == "rule"
    assert rows["UPI-SWIGGY-ORDER-8821"]["category"] == "MyFood"

    methods = payload["summary"]["by_method"]
    assert methods.get("builtin", 0) >= 1, methods
    builtin_rows = [r for r in payload["data"]["transactions"]
                    if r["classification"]["method"] == "builtin"]
    # The vocabulary switch has to be visible, not left to be noticed.
    assert all("built-in taxonomy" in r["classification"]["explanation"]
               for r in builtin_rows)


def test_empty_ruleset_is_allowed_only_with_builtin_fallback(client, headers):
    res = _post(client, headers, rules={"rules": []})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "INVALID_RULES"
    assert "fallback" in res.json()["error"]["message"]

    ok = _post(client, headers, rules={"rules": [], "fallback": "builtin"})
    assert ok.status_code == 200
    assert ok.json()["summary"]["rule_count"] == 0


# ----------------------------------------------------------------- validation

def test_rules_are_required(client, headers):
    res = _post(client, headers, rules=None)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "MISSING_RULES"


def test_malformed_json_is_reported_as_such(client, headers):
    res = _post(client, headers, rules="{not json")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "INVALID_RULES"
    assert "not valid JSON" in res.json()["error"]["message"]


def test_a_bare_array_of_rules_is_accepted(client, headers):
    """`[{...}]` is the obvious thing to send; refusing it would buy nothing."""
    res = _post(client, headers, rules=[{"id": "food", "category": "Food",
                                         "match": {"any_of": ["SWIGGY"]}}])
    assert res.status_code == 200, res.text
    # Two rows carry SWIGGY, so the single rule matches both.
    assert res.json()["summary"]["by_category"]["Food"] == 2


@pytest.mark.parametrize("bad,expect_in_message", [
    ({"rules": [], "fallbcak": "builtin"}, "unknown top-level key"),
    ({"rules": [{"id": "a", "category": "C", "match": {"any_off": ["X"]}}]},
     "unknown key(s) in 'match'"),
    ({"rules": [{"id": "a", "category": "C", "matchh": {"any_of": ["X"]}}]},
     "unknown key(s) on rule"),
])
def test_unknown_keys_are_rejected_not_ignored(client, headers, bad,
                                               expect_in_message):
    """A typo'd key under a lenient parser yields a rule that looks specific and
    matches far more than intended — the worst failure a classifier can have."""
    res = _post(client, headers, rules=bad)
    assert res.status_code == 400, res.text
    assert expect_in_message in res.json()["error"]["message"]


def test_duplicate_rule_ids_are_rejected(client, headers):
    res = _post(client, headers, rules={"rules": [
        {"id": "dup", "category": "A", "match": {"any_of": ["X"]}},
        {"id": "dup", "category": "B", "match": {"any_of": ["Y"]}},
    ]})
    assert res.status_code == 400
    msg = res.json()["error"]["message"]
    assert "duplicate rule id 'dup'" in msg
    assert "rules[1]" in msg


def test_invalid_regex_names_the_problem(client, headers):
    res = _post(client, headers, rules={"rules": [
        {"id": "r", "category": "C", "match": {"regex": "([unclosed"}}]})
    assert res.status_code == 400
    err = res.json()["error"]
    assert err["code"] == "INVALID_RULES"
    assert "not a valid regular expression" in err["message"]
    assert err["detail"]["rule"] == "rules[0]"


def test_impossible_bounds_are_rejected_rather_than_silently_never_matching(
        client, headers):
    for bad, needle in (
        ({"min_amount": 500, "max_amount": 100}, "can never match"),
        ({"min_date": "2026-06-01", "max_date": "2026-01-01"}, "can never match"),
    ):
        res = _post(client, headers, rules={"rules": [
            {"id": "r", "category": "C", "match": bad}]})
        assert res.status_code == 400, bad
        assert needle in res.json()["error"]["message"]


def test_empty_term_is_rejected(client, headers):
    """An empty term matches everything and silently widens the rule."""
    res = _post(client, headers, rules={"rules": [
        {"id": "r", "category": "C", "match": {"any_of": ["SWIGGY", ""]}}]})
    assert res.status_code == 400
    assert "empty term" in res.json()["error"]["message"]


def test_errors_point_at_the_offending_rule(client, headers):
    """A ruleset is often machine-generated; 'it is invalid' is unactionable."""
    res = _post(client, headers, rules={"rules": [
        {"id": "fine", "category": "A", "match": {"any_of": ["X"]}},
        {"id": "fine2", "category": "B", "match": {"any_of": ["Y"]}},
        {"id": "broken", "category": "C", "match": {"direction": "sideways"}},
    ]})
    assert res.status_code == 400
    detail = res.json()["error"]["detail"]
    assert detail["rule"] == "rules[2]"
    assert detail["field"] == "direction"


def test_too_many_rules_is_refused(client, headers):
    many = {"rules": [{"id": f"r{i}", "category": "C",
                       "match": {"any_of": [f"TERM{i}"]}}
                      for i in range(rulespec.MAX_RULES + 1)]}
    res = _post(client, headers, rules=many)
    assert res.status_code == 400
    assert res.json()["error"]["detail"]["rule_count"] == rulespec.MAX_RULES + 1


def test_oversized_ruleset_is_refused_with_413(client, headers):
    """Our own limit must be the one that fires, with an actionable code.

    It has to sit below Starlette's 1 MB per-part multipart cap, or Starlette
    refuses the field first and the caller gets a bare 400 that does not say the
    ruleset was the problem. This test is what holds that ordering.
    """
    padding = "P" * (rulespec.MAX_RULES_JSON_BYTES + 1000)
    res = _post(client, headers,
                rules=json.dumps({"rules": [
                    {"id": "r", "category": "C",
                     "match": {"any_of": ["X"]}, "set": {"counterparty": padding}}]}))
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "RULES_TOO_LARGE"


def test_overlong_regex_is_refused(client, headers):
    res = _post(client, headers, rules={"rules": [
        {"id": "r", "category": "C",
         "match": {"regex": "a" * (rulespec.MAX_REGEX_LENGTH + 1)}}]})
    assert res.status_code == 400
    assert res.json()["error"]["detail"]["field"] == "regex"


def test_ruleset_is_validated_before_the_file_is_read(client, headers):
    """A bad ruleset must not require streaming the upload to disk first."""
    res = _post(client, headers, rules={"rules": [
        {"id": "r", "category": "C", "match": {"direction": "sideways"}}]},
        content=b"this is not a parseable statement at all")
    # The ruleset fault is reported, not a parse failure.
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "INVALID_RULES"


# ------------------------------------------------------------- file handling

def test_missing_file_is_reported(client, headers):
    res = client.post("/v1/classify", headers=headers,
                      data={"rules": json.dumps(_RULES)})
    assert res.status_code in (400, 422)


def test_unsupported_format_is_415(client, headers):
    res = _post(client, headers, rules=_RULES,
                content=b"PK\x03\x04fake zip payload", filename="s.zip",
                ctype="application/zip")
    assert res.status_code == 415
    assert res.json()["error"]["code"] == "UNSUPPORTED_FILE_FORMAT"


def test_file_with_no_transactions_is_422(client, headers):
    res = _post(client, headers, rules=_RULES,
                content=b"Date,Narration,Debit,Credit,Balance\n")
    assert res.status_code == 422
    assert res.json()["error"]["code"] in ("NO_TRANSACTIONS_FOUND", "PARSE_FAILED")


def test_json_statement_is_accepted(client, headers):
    """Format-agnostic: the same ruleset must work on a JSON statement."""
    body = json.dumps({"transactions": [
        {"date": "2026-03-02", "description": "UPI-SWIGGY-ORDER-1", "debit": 450},
        {"date": "2026-03-03", "description": "SALARY CREDIT", "credit": 50000},
    ]}).encode()
    res = _post(client, headers, rules=_RULES, content=body,
                filename="s.json", ctype="application/json")
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["metadata"]["detected_format"] == "json"
    rows = _by_description(payload)
    assert rows["UPI-SWIGGY-ORDER-1"]["category"] == "Food"
    assert rows["SALARY CREDIT"]["category"] == "Payroll"


# ---------------------------------------------------------------- statelessness

def test_nothing_is_written_to_the_ledger(client, headers):
    """The service processes and returns; it must not accumulate client data."""
    from app.models.transaction import Transaction
    from app.models.statement import Statement

    db = TestingSessionLocal()
    try:
        before = (db.query(Transaction).count(), db.query(Statement).count())
    finally:
        db.close()

    assert _post(client, headers, rules=_RULES).status_code == 200

    db = TestingSessionLocal()
    try:
        after = (db.query(Transaction).count(), db.query(Statement).count())
    finally:
        db.close()
    assert before == after


def test_usage_is_metered(client, headers):
    res = _post(client, headers, rules=_RULES)
    assert res.status_code == 200
    rid = res.json()["request_id"]

    db = TestingSessionLocal()
    try:
        rec = db.query(UsageRecord).filter(UsageRecord.request_id == rid).first()
        assert rec is not None, "no usage record written for /v1/classify"
        assert rec.endpoint == "/v1/classify"
        assert rec.status_code == 200
        assert rec.transaction_count == 8
    finally:
        db.close()


def test_failures_are_metered_too(client, headers):
    res = _post(client, headers, rules=_RULES,
                content=b"Date,Narration,Debit,Credit,Balance\n")
    assert res.status_code == 422
    db = TestingSessionLocal()
    try:
        rec = (db.query(UsageRecord)
                 .filter(UsageRecord.endpoint == "/v1/classify",
                         UsageRecord.succeeded == False)  # noqa: E712
                 .order_by(UsageRecord.id.desc()).first())
        assert rec is not None
        assert rec.status_code == 422
    finally:
        db.close()


def test_identical_requests_give_identical_output(client, headers):
    """Reproducibility: same file, same rules, same answer every time."""
    first = _post(client, headers, rules=_RULES).json()
    second = _post(client, headers, rules=_RULES).json()
    assert first["data"] == second["data"]
    assert first["summary"] == second["summary"]


def test_temp_directory_is_not_leaked(client, headers):
    """Each request must leave nothing behind in the system temp directory.

    `ingest.save_upload` nests its working directory inside the one the endpoint
    creates, so cleaning only the inner one leaks an empty parent per request.
    """
    import glob
    import tempfile

    pattern = os.path.join(tempfile.gettempdir(), "b2b_cls_*")
    before = set(glob.glob(pattern))
    for _ in range(3):
        assert _post(client, headers, rules=_RULES).status_code == 200
    # Also on the failure path.
    _post(client, headers, rules=_RULES,
          content=b"Date,Narration,Debit,Credit,Balance\n")
    assert set(glob.glob(pattern)) == before


# ---------------------------------------------------------------- review fixes
#
# One test per bug found in the post-implementation code review. Each fails
# against the code as first written and passes after the fix.

def test_builtin_fallback_runs_even_when_default_category_is_set(client, headers):
    """fallback:builtin must be consulted BEFORE default_category, not skipped.

    Regression: classify_one applied default_category itself, so an unmatched
    row was stamped 'default' before the built-in classifier was ever tried, and
    a ruleset with both settings silently never used the built-in engine.
    """
    rules = {"rules": [{"id": "food", "category": "MyFood",
                        "match": {"any_of": ["SWIGGY"]}}],
             "fallback": "builtin",
             "default_category": "CatchAll"}
    payload = _post(client, headers, rules=rules).json()
    methods = payload["summary"]["by_method"]
    # The built-in classifier must have decided at least one row; without the
    # fix every unmatched row is 'default' and 'builtin' never appears.
    assert methods.get("builtin", 0) >= 1, methods
    # And default_category is still the last resort for rows the built-in also
    # abstained on — so both mechanisms coexist.
    builtin_rows = [r for r in payload["data"]["transactions"]
                    if r["classification"]["method"] == "builtin"]
    assert all("built-in taxonomy" in r["classification"]["explanation"]
               for r in builtin_rows)


def test_ambiguity_is_detected_regardless_of_rule_order(client, headers):
    """A same-priority different-category conflict is reported even when a
    same-priority same-category rule sits between winner and conflict.

    Regression: a single running 'runner_up' latched onto the first equal rule
    and a strict '>' comparison never let the later, different-category rule
    replace it, so the conflict went unreported for this ordering only.
    """
    rules = {"rules": [
        {"id": "a0", "category": "Alpha", "priority": 100, "match": {"any_of": ["SWIGGY"]}},
        {"id": "a1", "category": "Alpha", "priority": 100, "match": {"any_of": ["SWIGGY"]}},
        {"id": "b0", "category": "Beta", "priority": 100, "match": {"any_of": ["SWIGGY"]}},
    ], "default_category": "None"}
    payload = _post(client, headers, rules=rules).json()
    row = _by_description(payload)["UPI-SWIGGY-ORDER-8821"]
    assert row["category"] == "Alpha"          # earliest at top priority wins
    assert row["classification"]["ambiguous"] is True
    assert row["classification"]["runner_up_rule_id"] == "b0"
    assert payload["summary"]["ambiguous_count"] >= 1


def test_non_finite_numbers_are_rejected_as_400_not_500(client, headers):
    """Infinity/NaN (which json.loads accepts) must be INVALID_RULES, not a 500.

    Regression: these reached int()/Decimal comparisons outside the guarded
    blocks and raised unhandled exceptions, which the ApiError handler could not
    render — the caller got a bare 500 for a malformed ruleset.
    """
    for field_json in ('{"id":"x","category":"C","priority":Infinity,"match":{"any_of":["X"]}}',
                       '{"id":"x","category":"C","priority":NaN,"match":{"any_of":["X"]}}',
                       '{"id":"x","category":"C","match":{"min_amount":Infinity,"any_of":["X"]}}',
                       '{"id":"x","category":"C","match":{"max_amount":NaN,"any_of":["X"]}}'):
        res = _post(client, headers, rules='{"rules":[' + field_json + ']}')
        assert res.status_code == 400, f"{field_json} -> {res.status_code}"
        assert res.json()["error"]["code"] == "INVALID_RULES"


def test_event_type_is_returned_not_silently_dropped(client, headers):
    """event_type is advertised as settable, so it must appear in the response.

    Regression: CanonicalTxn.to_api() does not emit event_type, so a rule that
    set it validated, applied, and then vanished from the output.
    """
    rules = {"rules": [{"id": "sub", "category": "Software",
                        "match": {"any_of": ["SWIGGY"]},
                        "set": {"event_type": "SUBSCRIPTION",
                                "counterparty": "Swiggy"}}],
             "default_category": "Other"}
    payload = _post(client, headers, rules=rules).json()
    row = _by_description(payload)["UPI-SWIGGY-ORDER-8821"]
    assert row["event_type"] == "SUBSCRIPTION"
    assert row["counterparty"] == "Swiggy"      # a native field still works too


def test_regex_flags_do_not_disable_case_insensitivity(client, headers):
    """Supplying regex_flags must ADD to case-insensitivity, never replace it.

    Regression: regex_flags reset the flag set to 0, so a lowercase pattern with
    flags='s' silently stopped matching an uppercase narration.
    """
    rules = {"rules": [{"id": "r", "category": "Food",
                        "match": {"regex": "swiggy", "regex_flags": "s"}}],
             "default_category": "Other"}
    payload = _post(client, headers, rules=rules).json()
    # The narration is 'UPI-SWIGGY-ORDER-8821' (uppercase); the pattern is lower.
    assert _by_description(payload)["UPI-SWIGGY-ORDER-8821"]["category"] == "Food"
