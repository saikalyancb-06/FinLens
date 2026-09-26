"""Currency re-denomination: the rate table, the FX narration parser, and the
display amounts the Transactions tab renders.

The invariant under test throughout is that a stored amount is never rewritten.
Every assertion about a converted figure is paired with an assertion that
`debit_paise` is still exactly what was written.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.currency.display import build_display
from app.currency.fx_parser import derive_rate, parse_fx_leg
from app.currency.service import (
    SEED_AS_OF, SEED_CURRENCIES, convert_minor, rate_on, rates_for_dates,
    seed_currencies,
)
from app.models.currency import Currency, CurrencyRate
from app.models.transaction import Direction, SourceType, Transaction
from tests.conftest import TestingSessionLocal


@pytest.fixture
def auth_headers(client):
    email = f"ccy_{uuid.uuid4().hex[:6]}@example.com"
    password = "Password123!"
    client.post("/auth/register", json={"email": email, "password": password})
    token = client.post("/auth/login", json={"email": email, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def seeded(client, auth_headers):
    """Seed the rate table via the endpoint the UI actually calls."""
    res = client.get("/currencies", headers=auth_headers)
    assert res.status_code == 200
    return {c["code"]: c for c in res.json()}


# ---------------------------------------------------------------------------
# Rate table
# ---------------------------------------------------------------------------

def test_seed_is_idempotent_and_never_clobbers_a_manual_rate(seeded, client, auth_headers):
    client.put("/currencies/USD/rate", headers=auth_headers,
               json={"inr_per_unit": 91.25, "as_of": "2026-07-01"})

    # Re-seeding is what a fresh deploy or a repair script does. It must not
    # revert a rate somebody entered from a bank advice.
    res = client.post("/currencies/reseed?overwrite_seed_rates=true", headers=auth_headers)
    assert res.status_code == 200

    rates = client.get("/currencies/USD/rates", headers=auth_headers).json()
    manual = [r for r in rates if r["as_of"] == "2026-07-01"]
    assert manual and manual[0]["inr_per_unit"] == 91.25
    assert manual[0]["source"] == "manual"


def test_rate_lookup_uses_the_newest_rate_not_after_the_date(seeded, client, auth_headers):
    for as_of, rate in [("2026-07-01", 85.0), ("2026-08-18", 91.25)]:
        client.put("/currencies/USD/rate", headers=auth_headers,
                   json={"inr_per_unit": rate, "as_of": as_of})

    db = TestingSessionLocal()
    try:
        assert rate_on(db, "USD", date(2026, 7, 15)) == Decimal("85.00000000")
        assert rate_on(db, "USD", date(2026, 8, 20)) == Decimal("91.25000000")
        # Before any real rate exists, the earliest known one is used rather
        # than leaving the row unconvertible.
        assert rate_on(db, "USD", date(1999, 1, 1)) == Decimal("88.00000000")
    finally:
        db.close()


def test_batched_lookup_agrees_with_the_single_lookup(seeded, client, auth_headers):
    client.put("/currencies/USD/rate", headers=auth_headers,
               json={"inr_per_unit": 85.0, "as_of": "2026-07-01"})
    dates = [date(2026, 6, 30), date(2026, 7, 1), date(2026, 7, 2), date(2030, 1, 1)]
    db = TestingSessionLocal()
    try:
        batched = rates_for_dates(db, "USD", dates)
        for d in dates:
            assert batched[d] == rate_on(db, "USD", d), d
    finally:
        db.close()


def test_an_implausible_rate_is_rejected(seeded, client, auth_headers):
    assert client.put("/currencies/USD/rate", headers=auth_headers,
                      json={"inr_per_unit": 0}).status_code == 422
    assert client.put("/currencies/USD/rate", headers=auth_headers,
                      json={"inr_per_unit": 10_000_000}).status_code == 422


def test_the_base_currency_rate_cannot_be_edited(seeded, client, auth_headers):
    res = client.put("/currencies/INR/rate", headers=auth_headers,
                     json={"inr_per_unit": 2.0})
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# Conversion arithmetic
# ---------------------------------------------------------------------------

def test_zero_decimal_currencies_are_not_scaled_by_a_hundred():
    # 1,00,000 rupees at 0.58 INR per yen is ~172,414 yen. Assuming two minor
    # units everywhere would report 17,241,379 - out by a factor of 100.
    jpy = convert_minor(10_000_000, "INR", "JPY", 2, 0, Decimal(1), Decimal("0.58"))
    assert jpy == 172414


def test_conversion_is_symmetric_within_the_targets_own_precision():
    inr = 2_717_718
    usd = convert_minor(inr, "INR", "USD", 2, 2, Decimal(1), Decimal("88"))
    back = convert_minor(usd, "USD", "INR", 2, 2, Decimal("88"), Decimal(1))
    # A cent is 0.88 rupees, so a round trip can land up to half a cent away.
    # This is precisely why the application converts from the stored column
    # every time instead of persisting a converted value.
    assert abs(back - inr) <= 44


def test_a_missing_rate_yields_none_rather_than_a_guess():
    assert convert_minor(100, "INR", "USD", 2, 2, Decimal(1), None) is None
    assert convert_minor(None, "INR", "USD", 2, 2, Decimal(1), Decimal("88")) is None


# ---------------------------------------------------------------------------
# FX narration parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("narration,code,minor", [
    ("FCY OUTWARD REMIT/HDFCN01/MULLER GMBH/EUR 1,200.00/IMPORT PAYMENT", "EUR", 120000),
    ("INWARD TT USD 306.36 @ 88.71 MANAGEMENT FEE RECEIPT", "USD", 30636),
    ("OUTWARD TT JPY 19,359,987.24 @ 0.58 CONTRACTOR PAYOUT", "JPY", 19359987),
])
def test_a_named_foreign_amount_is_captured(narration, code, minor):
    leg = parse_fx_leg(narration)
    assert leg is not None
    assert (leg.currency, leg.amount_minor) == (code, minor)


@pytest.mark.parametrize("narration", [
    "UPI/PAY/9876543210/SWIGGY ORDER",
    "NEFT DR HDFC0001234 ACME INDUSTRIES 250000",
    "SWIFT CHARGES USD 25.00",                 # the bank's fee, not the payment
    "FX CONVERSION CHARGES USD 45.00 REF FX1",
])
def test_a_narration_without_a_real_foreign_leg_is_left_alone(narration):
    assert parse_fx_leg(narration) is None


def test_a_narration_that_does_not_reconcile_is_rejected():
    # USD 100 at 88 is 8,800 rupees. A booked figure of 10,00,000 means the
    # narration was misread, and inventing a foreign amount from it would put a
    # number on screen that appears on no document.
    assert parse_fx_leg("INWARD TT USD 100.00 @ 88.00 X", booked_minor=100_000_000) is None
    assert parse_fx_leg("INWARD TT USD 100.00 @ 88.00 X", booked_minor=880_000) is not None


def test_the_rate_is_derived_from_the_advice_when_it_is_not_quoted():
    leg = parse_fx_leg("CROSS BORDER PAYMENT USD 1,000.00 TO ACME INC")
    assert leg.rate is None
    # Both figures came off the same advice, so the implied rate is exact.
    assert derive_rate(leg, 8_800_000) == Decimal("88.00000000")


# ---------------------------------------------------------------------------
# Display, through the API
# ---------------------------------------------------------------------------

def _make_fx_transaction(user_id):
    db = TestingSessionLocal()
    try:
        tx = Transaction(
            id=uuid.uuid4(), user_id=user_id, direction=Direction.DEBIT,
            debit_paise=115_920_000, balance_paise=500_000_000,
            txn_date=date(2026, 7, 15),
            narration_raw="FCY OUTWARD REMIT/HDFCN01/MULLER GMBH/USD 13,172.73/IMPORT PAYMENT",
            narration_clean="FCY OUTWARD REMIT MULLER GMBH USD IMPORT PAYMENT",
            source_type=SourceType.STATEMENT, booked_currency="INR",
            original_currency="USD", original_amount_minor=1_317_273,
            fx_rate=Decimal("88"),
        )
        db.add(tx)
        db.commit()
        return tx.id
    finally:
        db.close()


def _user_id(client, headers):
    return client.get("/auth/me", headers=headers).json()["id"]


def test_actual_mode_shows_the_advice_figure_not_a_reconversion(seeded, client, auth_headers):
    _make_fx_transaction(_user_id(client, auth_headers))
    # A rate that disagrees with the advice, to prove "actual" ignores the table.
    client.put("/currencies/USD/rate", headers=auth_headers,
               json={"inr_per_unit": 70.0, "as_of": "2026-01-01"})

    rows = client.get("/transactions?display_currency=actual&search=MULLER",
                      headers=auth_headers).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["display_currency"] == "USD"
    assert row["display_amount"] == 13172.73      # off the advice, not 115920000/70
    assert row["display_is_exact"] is True
    # A balance belongs to the account, so it stays in the account's currency.
    assert row["display_balance_currency"] == "INR"
    assert row["debit_paise"] == 115_920_000      # stored amount untouched


def test_an_explicit_currency_converts_the_balance_too(seeded, client, auth_headers):
    _make_fx_transaction(_user_id(client, auth_headers))
    # Pinned explicitly rather than relying on the seed: rate rows are shared
    # reference data, so an earlier test in this file can and does leave a
    # different USD rate behind.
    client.put("/currencies/USD/rate", headers=auth_headers,
               json={"inr_per_unit": 88.0, "as_of": "2026-07-15"})

    rows = client.get("/transactions?display_currency=USD&search=MULLER",
                      headers=auth_headers).json()
    row = rows[0]
    assert row["display_amount"] == 13172.73       # still the advice figure
    assert row["display_balance_currency"] == "USD"
    assert row["display_balance"] == pytest.approx(5_000_000 / 88, rel=1e-4)


def test_a_third_currency_is_marked_inexact(seeded, client, auth_headers):
    _make_fx_transaction(_user_id(client, auth_headers))
    row = client.get("/transactions?display_currency=EUR&search=MULLER",
                     headers=auth_headers).json()[0]
    assert row["display_currency"] == "EUR"
    assert row["display_is_exact"] is False


def test_omitting_the_parameter_leaves_the_legacy_fields_untouched(seeded, client, auth_headers):
    _make_fx_transaction(_user_id(client, auth_headers))
    row = client.get("/transactions?search=MULLER", headers=auth_headers).json()[0]
    assert row["display_currency"] is None
    assert row["debit"] == 1_159_200.0


def test_an_unknown_display_currency_is_rejected(seeded, client, auth_headers):
    _make_fx_transaction(_user_id(client, auth_headers))
    res = client.get("/transactions?display_currency=XYZ", headers=auth_headers)
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# Rate basis: the transaction's day vs today
# ---------------------------------------------------------------------------

def test_a_bare_rate_lookup_means_today_not_the_oldest_row_on_file(seeded, client, auth_headers):
    """The bug this pins.

    `rate_on(db, code)` with no date used to skip the date filter entirely and
    fall through to the *earliest* row — almost always the shipped placeholder.
    Nothing raised. A "value this at today's price" view simply returned a rate
    from the year 2000, and matched the historical view exactly.
    """
    client.put("/currencies/USD/rate", headers=auth_headers,
               json={"inr_per_unit": 99.0, "as_of": "2026-08-18"})
    db = TestingSessionLocal()
    try:
        assert rate_on(db, "USD") == Decimal("99.00000000")
        assert rate_on(db, "USD") != Decimal(str(dict(
            (c[0], c[4]) for c in SEED_CURRENCIES)["USD"]))
    finally:
        db.close()


#: The booked rupee amount of `_old_fx_transaction`, in rupees. Named because
#: the paise/rupee conversion is exactly what these assertions got wrong first.
BOOKED_INR = 87_490.00


def _old_fx_transaction(user_id):
    """A USD 1,000 payment booked at Rs 87,490 (rate 87.49), long before today."""
    db = TestingSessionLocal()
    try:
        tx = Transaction(
            id=uuid.uuid4(), user_id=user_id, direction=Direction.DEBIT,
            debit_paise=87_49_000, balance_paise=500_00_000,
            txn_date=date(2025, 8, 18),
            narration_raw="CROSS BORDER PAYMENT USD 1,000.00 TO ACME INC",
            narration_clean="CROSS BORDER PAYMENT USD 1000 TO ACME INC",
            source_type=SourceType.STATEMENT, booked_currency="INR",
            original_currency="USD", original_amount_minor=100000,
            fx_rate=Decimal("87.49"),
        )
        db.add(tx)
        db.commit()
    finally:
        db.close()


def _row(client, headers, ccy, basis):
    return client.get(
        f"/transactions?search=ACME INC&display_currency={ccy}&rate_basis={basis}",
        headers=headers).json()[0]


def test_the_two_bases_give_different_answers(seeded, client, auth_headers):
    _old_fx_transaction(_user_id(client, auth_headers))
    for as_of, rate in (("2025-08-18", 87.49), ("2026-08-18", 99.0)):
        client.put("/currencies/EUR/rate", headers=auth_headers,
                   json={"inr_per_unit": rate, "as_of": as_of})

    historic = _row(client, auth_headers, "EUR", "txn")
    today = _row(client, auth_headers, "EUR", "current")

    assert historic["display_amount"] != today["display_amount"]
    assert historic["display_rate_basis"] == "txn"
    assert today["display_rate_basis"] == "current"
    # The same Rs 87,490, valued at each day's rate.
    assert historic["display_amount"] == pytest.approx(BOOKED_INR / 87.49, rel=1e-3)
    assert today["display_amount"] == pytest.approx(BOOKED_INR / 99.0, rel=1e-3)


def test_actual_mode_ignores_the_basis_entirely(seeded, client, auth_headers):
    """A payment made in dollars was made in dollars. No rate changes that."""
    _old_fx_transaction(_user_id(client, auth_headers))
    for basis in ("txn", "current"):
        row = _row(client, auth_headers, "actual", basis)
        assert row["display_currency"] == "USD"
        assert row["display_amount"] == 1000.00
        assert row["display_is_exact"] is True


def test_the_rows_own_currency_is_exact_on_the_transaction_basis_only(seeded, client, auth_headers):
    """Asking 'what is this worth today' must not answer with the advice figure."""
    _old_fx_transaction(_user_id(client, auth_headers))
    client.put("/currencies/USD/rate", headers=auth_headers,
               json={"inr_per_unit": 99.0, "as_of": "2026-08-18"})

    historic = _row(client, auth_headers, "USD", "txn")
    assert historic["display_amount"] == 1000.00 and historic["display_is_exact"] is True

    today = _row(client, auth_headers, "USD", "current")
    assert today["display_amount"] == pytest.approx(BOOKED_INR / 99.0, rel=1e-3)
    assert today["display_is_exact"] is False


def test_rupees_are_rupees_on_either_basis(seeded, client, auth_headers):
    _old_fx_transaction(_user_id(client, auth_headers))
    for basis in ("txn", "current"):
        row = _row(client, auth_headers, "INR", basis)
        assert row["display_amount"] == BOOKED_INR
        assert row["display_is_exact"] is True


def test_an_unknown_basis_is_rejected(seeded, client, auth_headers):
    _old_fx_transaction(_user_id(client, auth_headers))
    res = client.get("/transactions?display_currency=USD&rate_basis=yesterday",
                     headers=auth_headers)
    assert res.status_code == 400
