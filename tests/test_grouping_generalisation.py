"""Does the grouping work on data it was not built against?

HONEST FRAMING OF WHAT THE OTHER TEST FILE PROVES. The five banks' narration
samples in test_review_grouping_coverage.py were written by hand alongside the
patterns that match them. A test built that way proves the regexes match strings
chosen to match the regexes; it is a useful REGRESSION fixture — it will catch a
future edit that breaks HDFC — but it is close to worthless as evidence that an
unseen bank will work.

This file tries to answer the harder question with checks that do not depend on
formats anyone hand-picked:

  1. ABLATION. Delete every bank-specific pattern and re-measure. This exposes
     which number is real: coverage survives untouched at ~99.9%, because the
     shape fallback catches whatever the patterns miss. So coverage is NOT
     evidence the patterns work — it would read 99.9% if every one of them were
     wrong. What collapses is the share understood as a real counterparty.

  2. GENERATED FORMATS. Narrations assembled from randomised rails, reference
     formats, separators and names that appear in no pattern in the module.

  3. THE HEALTH CHECK ITSELF. It has to fire on an unfamiliar format, or it is
     decoration. An earlier version keyed off a hardcoded list of rail names,
     which made it exactly as format-specific as the thing it audits — it passed
     a completely foreign format as healthy. That regression is pinned here.
"""

import csv
import pathlib
import random
import string

import pytest

import app.categorization.counterparty as cp_module
from app.categorization.counterparty import group_for, grouping_health

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "real_bank_narrations.csv"


def _real_narrations():
    with FIXTURE.open() as fh:
        return [r["narration"] for r in csv.DictReader(fh) if r["narration"].strip()]


@pytest.fixture
def without_channel_patterns():
    """Strip every bank-specific pattern, leaving only format-agnostic code."""
    saved = cp_module._CHANNEL_PATTERNS
    cp_module._CHANNEL_PATTERNS = []
    try:
        yield
    finally:
        cp_module._CHANNEL_PATTERNS = saved


# ---------------------------------------------------------------------------
# 1. Ablation — which number is actually load-bearing
# ---------------------------------------------------------------------------

def test_coverage_is_not_evidence_that_the_patterns_work(without_channel_patterns):
    """Coverage survives deleting every pattern, so it cannot vouch for them.

    This test exists to stop anyone (including me) citing "99.9% coverage" as
    proof that an unfamiliar bank is handled. It is not.
    """
    health = grouping_health(_real_narrations())
    assert health.coverage >= 0.95, (
        "with no channel patterns at all, coverage should still be high — "
        "that is the point being made"
    )


def test_what_actually_degrades_is_counterparty_recognition(without_channel_patterns):
    health = grouping_health(_real_narrations())
    assert health.counterparty_share < 0.55, (
        "removing every pattern should collapse how much is understood as a "
        "real party; if it does not, the patterns are not doing the work"
    )


def test_the_health_check_notices_when_the_patterns_are_gone(without_channel_patterns):
    """The whole point: silent degradation must stop being silent."""
    health = grouping_health(_real_narrations())
    assert health.is_healthy is False
    assert health.suspect_share > 0.15
    assert health.as_dict()["warning"]
    # And it must name what it is unhappy about, not just complain.
    assert health.suspect_keys


def test_the_real_statement_is_healthy_with_the_patterns_in_place():
    health = grouping_health(_real_narrations())
    assert health.is_healthy is True
    assert health.counterparty_share > 0.70


# ---------------------------------------------------------------------------
# 2. Generated formats — rails, separators and names from no pattern here
# ---------------------------------------------------------------------------

# None of these rail prefixes appear in _CHANNEL_PATTERNS.
UNSEEN_RAILS = ["TRF", "XFER", "PMT", "REMIT", "DIRECTPAY", "QUICKTRANSFER", "ZAP"]
UNSEEN_SEPARATORS = ["~", "|", "::", "#", "^"]
MADE_UP_PARTIES = [
    "ACME INDUSTRIES LLP", "ZENITH TRADING CO", "BLUEFIN LOGISTICS",
    "ORCHARD FOODS PRIVATE LIMITED", "VERTEX SUPPLIES", "NORTHWIND MILLS",
]


def _generated_statement(seed: int, rows: int = 120):
    rng = random.Random(seed)
    rail = rng.choice(UNSEEN_RAILS)
    sep = rng.choice(UNSEEN_SEPARATORS)
    out = []
    for i in range(rows):
        party = MADE_UP_PARTIES[i % len(MADE_UP_PARTIES)]
        ref = "".join(rng.choice(string.digits) for _ in range(rng.randint(8, 14)))
        out.append(f"{rail}{sep}{ref}{sep}{party}{sep}SETTLE")
    return out, rail, sep


@pytest.mark.parametrize("seed", range(6))
def test_an_unseen_format_is_still_fully_groupable(seed):
    """Coverage must not depend on recognising the rail.

    This is the genuine value of the shape fallback: a bank nobody wrote a
    pattern for still produces a reviewable queue rather than 1,000 loose rows.
    """
    narrations, _rail, _sep = _generated_statement(seed)
    health = grouping_health(narrations)
    assert health.coverage >= 0.95, f"{health.as_dict()}"


@pytest.mark.parametrize("seed", range(6))
def test_an_unseen_format_is_reported_as_not_understood(seed):
    """And it must be HONEST that it does not understand the format.

    The regression this pins: a version of the health check that keyed off a
    hardcoded rail list passed "TRF~000123~ACME INDUSTRIES LLP~SETTLE" as
    healthy, because "TRF" was not in its list. Not recognising a rail is
    precisely the condition it is supposed to detect.
    """
    narrations, rail, sep = _generated_statement(seed)
    health = grouping_health(narrations)
    assert health.is_healthy is False, (
        f"rail={rail!r} sep={sep!r} passed as understood while "
        f"{health.counterparty_share:.0%} of rows were recognised as parties"
    )
    assert health.counterparty_share < 0.5


@pytest.mark.parametrize("seed", range(6))
def test_an_unseen_format_still_collapses_to_few_decisions(seed):
    """Degraded is not the same as useless.

    Even ungrasped, the shape fallback should turn 120 rows into a handful of
    groups — one per party — rather than 120.
    """
    narrations, _rail, _sep = _generated_statement(seed)
    keys = {g.key for g in (group_for(n) for n in narrations) if g}
    assert len(keys) <= len(MADE_UP_PARTIES) + 2, sorted(keys)


# ---------------------------------------------------------------------------
# 3. The health check must not cry wolf
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("charges", [
    ["MONTHLY ACCOUNT SERVICE CHARGE 09/2025"] * 20,
    ["POSRENT_JUN25_TID_65100728"] * 20,
    ["74460500000024:Int.Coll:01-03-2026 to 31-03-2026"] * 20,
    ["LEDGER FOLIO CHARGES - CC/OD"] * 20,
    ["MIN BAL CHARGES FOR SEP-2025"] * 20,
    ["SMS ALERT CHARGES Q2 2025"] * 20,
    ["CHQ RETURN CHARGES 12/09/2025"] * 20,
    ["CASH HANDLING CHGS AT OUTSTATION BRNCHS:01-09-2025"] * 20,
])
def test_a_statement_of_pure_bank_charges_is_not_flagged(charges):
    """Charges SHOULD group by shape — that is the design, not a failure.

    If fee lines tripped the warning, every statement would look broken and the
    signal would be ignored, which is worse than not having it.
    """
    health = grouping_health(charges)
    assert health.suspect_rows == 0, health.as_dict()["suspect_keys"]
    assert health.is_healthy is True


def test_a_mixed_statement_reports_both_populations():
    narrations = (
        ["NEFT-HDFCH25081234567-MEYER ORGANICS PVT LTD-HDFC BANK LTD."] * 30
        + ["MONTHLY ACCOUNT SERVICE CHARGE 09/2025"] * 10
    )
    health = grouping_health(narrations)
    assert health.by_counterparty == 30
    assert health.by_shape == 10
    assert health.is_healthy is True


def test_health_on_an_empty_batch_does_not_divide_by_zero():
    health = grouping_health([])
    assert health.total == 0
    assert health.coverage == 0.0
    assert health.counterparty_share == 0.0
