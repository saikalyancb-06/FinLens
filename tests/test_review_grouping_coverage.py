"""Every transaction must be reviewable in a GROUP, not one at a time.

The requirement: at least 95% of any uploaded statement is reviewable through a
group, so a user never faces a list of a thousand individual rows.

Two grouping strategies, in order:

  1. counterparty — the row names someone the user trades with (KUMAR FISH,
     MEYER ORGANICS). One decision settles every transaction with that party,
     past and future.
  2. narration shape — the row names nobody, because the money did not go to a
     trading partner at all: a bank charge, POS terminal rent, interest
     collected, a cash deposit. Reference numbers and dates are stripped and
     what remains is the KIND of charge, which is what the user decides about.
     273 identical "Charges for PORD Customer Payment" rows become ONE decision.

The 95% floor is asserted here against the real statement this was built from,
and against narration styles from other Indian banks as a regression fixture.

Be careful what the second set proves: those samples were hand-written next to
the patterns that match them. Evidence about UNSEEN formats lives in
test_grouping_generalisation.py, which deletes every bank-specific pattern and
re-measures, and generates narrations from rails this module has never heard of.
"""

import csv
import pathlib

import pytest

from app.categorization.counterparty import extract, group_for, shape_key

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "real_bank_narrations.csv"

COVERAGE_FLOOR = 0.95


def _coverage(narrations):
    grouped = [n for n in narrations if group_for(n)]
    return len(grouped) / len(narrations), [n for n in narrations if not group_for(n)]


# ---------------------------------------------------------------------------
# The real statement
# ---------------------------------------------------------------------------

def _real_narrations():
    with FIXTURE.open() as fh:
        return [r["narration"] for r in csv.DictReader(fh) if r["narration"].strip()]


def test_real_statement_is_at_least_95_percent_groupable():
    narrations = _real_narrations()
    assert len(narrations) > 1500, "fixture looks truncated"

    coverage, ungrouped = _coverage(narrations)
    assert coverage >= COVERAGE_FLOOR, (
        f"only {coverage:.1%} of {len(narrations)} rows are groupable; "
        f"{len(ungrouped)} would fall to one-by-one review. "
        f"Examples: {ungrouped[:5]}"
    )


def test_the_rows_that_cannot_be_grouped_are_genuinely_contentless():
    """What is left must be reference numbers, not real transactions.

    If this starts failing with recognisable narrations in it, the grouping
    rules have a gap — that is the signal to widen them, not to lower the floor.
    """
    _coverage_pct, ungrouped = _coverage(_real_narrations())
    for n in ungrouped:
        stripped = "".join(ch for ch in n if ch.isalpha())
        assert len(stripped) <= 2, (
            f"{n!r} carries real content but is not groupable"
        )


def test_grouping_collapses_the_statement_into_a_reviewable_number_of_decisions():
    """Coverage is worthless if it produces one group per row."""
    narrations = _real_narrations()
    keys = {g.key for g in (group_for(n) for n in narrations) if g}
    assert len(keys) < len(narrations) / 5, (
        f"{len(keys)} groups for {len(narrations)} rows is barely a reduction"
    )


# ---------------------------------------------------------------------------
# Other banks' narration styles
#
# WHAT THESE DO AND DO NOT PROVE. These samples were written by hand alongside
# the patterns that match them, so a pass here shows the regexes match strings
# chosen to match the regexes. That makes them a genuine REGRESSION fixture —
# they will catch a future edit that breaks the HDFC or SBI form — but NOT
# evidence that a bank nobody has seen will work.
#
# The harder question is answered in test_grouping_generalisation.py, by
# ablation (delete every pattern and re-measure) and by generated formats using
# rails and separators that appear in no pattern in the module.
# ---------------------------------------------------------------------------

HDFC = [
    "UPI-SWIGGY-SWIGGY@YBL-YESB0000262-412345678901-PAYMENT",
    "NEFT DR-HDFC0000123-MEYER ORGANICS PVT LTD-NETBANK",
    "IMPS-412345678901-RAMESH KUMAR-SBIN-XXXXXX1234",
    "ACH D- INDIAN CLEARING CORP-12345678",
    "POS 4321XXXXXXXX9876 RELIANCE RETAIL",
    "ATW-4321XXXXXXXX9876-S1ACHM01-MUMBAI",
    "MONTHLY ACCOUNT SERVICE CHARGE 09/2025",
    "INT PD:01-07-2025 to 30-09-2025",
]

ICICI = [
    "UPI/412345678901/Payment from Ph/SUNIL TRADERS/UTIB/9876543210",
    "NEFT-ICIC0000456-BHARAT SILICA WORKS-PAYMENT",
    "MMT/IMPS/523412345678/RENT JULY/SHOBHA G",
    "BIL/ONL/000123456/TATA POWER/ELECTRICITY",
    "VAT/EAZYDINER PVT/412345678901",
    "SERVICE CHARGES FOR SEP 2025",
    "DEBIT CARD ANNUAL FEE 2025-26",
]

SBI = [
    "TO TRANSFER-INB SUSHMITHA H SHETTY--",
    "BY TRANSFER-NEFT*SBIN0001234*KUMAR FISH--",
    "TO TRANSFER-UPI/DR/412345678901/SHIVKUMAR/YESB--",
    "BY CASH DEPOSIT SELF",
    "DEBIT INTEREST FOR THE QUARTER ENDED 30/09/2025",
    "SMS CHARGES FOR THE QTR 07/2025 TO 09/2025",
]

AXIS = [
    "NEFT/AXISP00123456/NARASIMHAIAH CHIKEN/CANARA BANK",
    "IMPS/P2A/524516911143/MARUTHI PRO STORE/GROCERY",
    "UPI/P2M/412345678901/ZOMATO LTD/PAYTM",
    "CONS CHRGS SEP25 + GST",
    "ACCOUNT MAINTENANCE CHARGE OCT 2025",
]

KOTAK = [
    "MB:UPI/412345678901/SURAJ KUMAR/KKBK",
    "NEFT-KKBK0000789-TOPSACK PACKAGING PRIVATE LIMITED",
    "RTGS-KKBKR52025091234-EMMVEE ENERGY PRIVATE LIMITED",
    "MIN BAL CHARGES FOR SEP-2025",
    "CHQ RETURN CHARGES 12/09/2025",
]


@pytest.mark.parametrize("bank,narrations", [
    ("HDFC", HDFC), ("ICICI", ICICI), ("SBI", SBI),
    ("AXIS", AXIS), ("KOTAK", KOTAK),
])
def test_other_banks_narration_styles_are_at_least_95_percent_groupable(bank, narrations):
    coverage, ungrouped = _coverage(narrations)
    assert coverage >= COVERAGE_FLOOR, (
        f"{bank}: only {coverage:.0%} groupable; ungrouped: {ungrouped}"
    )


def test_named_parties_group_as_counterparties_not_as_shapes():
    """Coverage must not be met by dumping everything into shape buckets.

    A row naming a supplier has to be recognised AS that supplier — otherwise
    two different suppliers reached through the same rail land in one group and
    a single decision mis-books both.
    """
    for narration, expected in [
        ("NEFT DR-HDFC0000123-MEYER ORGANICS PVT LTD-NETBANK", "MEYER ORGANICS"),
        ("NEFT/AXISP00123456/NARASIMHAIAH CHIKEN/CANARA BANK", "NARASIMHAIAH CHIKEN"),
        ("RTGS-KKBKR52025091234-EMMVEE ENERGY PRIVATE LIMITED", "EMMVEE ENERGY"),
        ("BT26022051849666/ 6051910375/79707/BOBCARD LIMITE", "BOBCARD"),
    ]:
        g = group_for(narration)
        assert g is not None and g.kind == "counterparty", narration
        assert g.key == expected, f"{narration} -> {g.key}, expected {expected}"


def test_two_different_suppliers_never_share_a_group():
    a = group_for("NEFT-HDFCH1-MEYER ORGANICS PVT LTD-HDFC BANK LTD.")
    b = group_for("NEFT-HDFCH2-TOPSACK PACKAGING PRIVATE LIMITED-HDFC BANK LTD.")
    assert a.key != b.key


# ---------------------------------------------------------------------------
# Shape keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    # Same charge, different month and reference
    ("POSRENT_JUN25_TID_65100728", "POSRENT_FEB26_65100729"),
    ("Charges for PORD Customer Payment :003551752858",
     "Charges for PORD Customer Payment :003551748418"),
    ("74460500000024:Int.Coll:01-03-2026 to 31-03-2026",
     "74460500000011:Int.Coll:01-11-2025 to 30-11-2025"),
    ("MONTHLY ACCOUNT SERVICE CHARGE 09/2025",
     "MONTHLY ACCOUNT SERVICE CHARGE 12/2025"),
])
def test_the_same_charge_written_differently_lands_in_one_group(a, b):
    assert shape_key(a) is not None
    assert shape_key(a) == shape_key(b)


@pytest.mark.parametrize("a,b", [
    ("SERVICE CHARGE FOR JUNE 2026", "SERVICE TAX PAYMENT 4402"),
    ("LEDGER FOLIO CHARGES - CC/OD", "POSRENT_JUN25_TID_65100728"),
    ("CASH HANDLING CHARGES 01-09-2025", "CHQ RETURN CHARGES 12/09/2025"),
])
def test_different_charges_stay_in_different_groups(a, b):
    assert shape_key(a) != shape_key(b)


@pytest.mark.parametrize("narration", ["A00021802260044311", "G000213112514553", "1234567890"])
def test_a_bare_reference_number_produces_no_shape(narration):
    """Better to review three rows individually than to bucket them wrongly."""
    assert shape_key(narration) is None
    assert group_for(narration) is None
