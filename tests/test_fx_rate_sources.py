"""The rate scraper, the API adapter, and the gates that decide what is stored.

No test here touches the network. The scraper is exercised against saved HTML
and the API adapter against response bodies captured verbatim from the live
service, so a change in either parser fails here rather than on someone's
Transactions tab.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.currency.refresher import RefreshReport, store_quotes
from app.currency.service import seed_currencies
from app.currency.sources.base import (
    MAX_STEP_CHANGE, RateQuote, RateSourceError, parse_rate, validate,
)
from app.currency.sources.frankfurter import (
    FbilRateSource, FrankfurterRateSource, _extract_by_date,
)
from app.models.currency import CurrencyRate
from tests.conftest import TestingSessionLocal

# --- Fixtures: real page shapes ------------------------------------------

# Captured verbatim from api.frankfurter.dev/v2 on 2026-08-18.
FRANKFURTER_LATEST = [
    {"date": "2026-08-18", "base": "INR", "quote": "SGD", "rate": 0.01338},
    {"date": "2026-08-18", "base": "INR", "quote": "USD", "rate": 0.01047},
]
FRANKFURTER_SERIES = [
    {"date": "2026-08-10", "base": "INR", "quote": "USD", "rate": 0.0105},
    {"date": "2026-08-11", "base": "INR", "quote": "USD", "rate": 0.01049},
    {"date": "2026-08-14", "base": "INR", "quote": "USD", "rate": 0.01048},
]


# --- Publisher routing ---------------------------------------------------

def test_fbil_is_asked_for_indias_official_rate_and_the_aggregate_for_the_rest():
    """Ordering is provenance, not reliability.

    FBIL has published India's official reference rate since July 2018 - RBI's
    own page republishes it. Taking a market rate for a currency FBIL covers
    would silently downgrade a number that may end up in a filing.
    """
    from app.currency.refresher import build_sources

    fbil, general = build_sources()
    assert fbil.name == "fbil" and fbil.providers == "FBIL"
    assert set(fbil.supported) == {"USD", "EUR", "GBP", "JPY", "AED"}

    assert general.name == "frankfurter" and general.providers is None
    # The seven FBIL does not publish must still be reachable.
    assert {"SGD", "AUD", "CAD", "CHF", "HKD", "SAR", "SEK"} <= set(general.supported)


def test_the_publisher_is_named_in_the_request():
    assert FbilRateSource()._params(["USD"])["providers"] == "FBIL"
    assert "providers" not in FrankfurterRateSource()._params(["SGD"])


def test_provenance_follows_the_publisher():
    """A stored rate has to say which benchmark produced it, all the way to the UI."""
    payload = [{"date": "2026-08-10", "base": "INR", "quote": "USD", "rate": 0.0105}]
    official = FbilRateSource()
    quote = official._to_quotes(_extract_by_date(payload), ["USD"], date(2026, 8, 10),
                                source=official.name, note=official.note)[0]
    assert quote.source == "fbil"
    assert "FBIL" in quote.note


# --- Rate API adapter -----------------------------------------------------

def test_the_live_array_response_is_inverted_to_inr_per_unit():
    quotes = {q.code: q.inr_per_unit for q in FrankfurterRateSource._to_quotes(
        _extract_by_date(FRANKFURTER_LATEST), ["USD", "SGD"], date(2026, 8, 18))}
    # The API says 1 INR = 0.01047 USD; the ledger stores 1 USD = 95.51 INR.
    assert quotes["USD"] == pytest.approx(Decimal(1) / Decimal("0.01047"))
    assert quotes["SGD"] == pytest.approx(Decimal(1) / Decimal("0.01338"))


def test_a_time_series_keeps_each_records_own_date():
    quotes = FrankfurterRateSource._to_quotes(
        _extract_by_date(FRANKFURTER_SERIES), ["USD"], date(2026, 8, 14))
    assert {q.as_of for q in quotes} == {
        date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 14)}


def test_the_legacy_nested_shape_is_still_accepted():
    legacy = {"date": "2026-08-18", "rates": {"CHF": 0.00849}}
    q = FrankfurterRateSource._to_quotes(_extract_by_date(legacy), ["CHF"],
                                         date(2026, 8, 18))
    assert q[0].inr_per_unit == pytest.approx(Decimal(1) / Decimal("0.00849"))


@pytest.mark.parametrize("payload", [[], "nope", {"error": "x"}, 42])
def test_an_unusable_body_raises(payload):
    with pytest.raises(RateSourceError):
        _extract_by_date(payload)


# --- Cell parsing ---------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("88.1234", Decimal("88.1234")),
    ("1,234.50", Decimal("1234.50")),
    ("88.12\xa0", Decimal("88.12")),
    ("-", None), ("", None), ("N/A", None), ("0", None), ("-5", None),
    ("not a number", None), (None, None),
])
def test_cell_parsing(raw, expected):
    assert parse_rate(raw) == expected


# --- Sanity gates ---------------------------------------------------------

def _quote(code="USD", value="90", day=None, source="rbi"):
    return RateQuote(code, day or date(2026, 8, 18), Decimal(value), source)


def test_a_rate_outside_the_plausible_band_is_refused():
    # The classic scraper regression: a column shift picks up a figure 100x out.
    assert validate(_quote(value="9125")) is not None
    assert validate(_quote(value="0.00001")) is not None
    assert validate(_quote(value="90")) is None


def test_a_large_step_from_a_trusted_rate_is_refused():
    assert validate(_quote(value="120"), previous=Decimal("88")) is not None
    assert validate(_quote(value="92"), previous=Decimal("88")) is None


def test_the_step_gate_can_be_widened_for_a_backfill():
    """A multi-year backfill legitimately spans moves a live poll never would."""
    assert validate(_quote(value="120"), previous=Decimal("88"),
                    max_step=Decimal("1.0")) is None


# --- Storage --------------------------------------------------------------

#: Currencies these storage tests write to. Rates are shared reference data
#: with a unique (code, as_of) key, so a rate another test file left behind for
#: the same day is a duplicate-key error here, not an independent test.
_TOUCHED = ("USD", "SEK", "SGD", "XYZ")


@pytest.fixture
def db():
    """A session whose rate table looks like a fresh install for these codes.

    Clearing and re-seeding rather than assuming an empty table: the suite shares
    one database, and `currency_rates` is reference data that other test files
    legitimately write to.
    """
    session = TestingSessionLocal()
    session.query(CurrencyRate).filter(CurrencyRate.code.in_(_TOUCHED)).delete(
        synchronize_session=False)
    session.commit()
    seed_currencies(session)          # restores the placeholder rows only
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.query(CurrencyRate).filter(CurrencyRate.code.in_(_TOUCHED)).delete(
            synchronize_session=False)
        session.commit()
        seed_currencies(session)
        session.commit()
        session.close()


def _report():
    return RefreshReport(started_at=datetime.now(timezone.utc))


def test_the_first_real_rate_is_not_blocked_by_the_seed_placeholder(db):
    """The bug this exempted: six of twelve seeds sit >10% from the market.

    Left unexempted, the first refresh after install would reject SEK, AUD, CHF,
    EUR, GBP and SGD, and the feature would look broken out of the box.
    """
    report = _report()
    store_quotes(db, [_quote("SEK", "10.0472", source="frankfurter")], report)   # seed is 8.40
    assert report.updated and not report.rejected


def test_the_gate_re_arms_once_a_trusted_rate_exists(db):
    store_quotes(db, [_quote("SEK", "10.0472", source="frankfurter")], _report())
    db.flush()
    report = _report()
    store_quotes(db, [_quote("SEK", "14.00", day=date(2026, 8, 19),
                             source="frankfurter")], report)
    assert report.rejected and not report.updated


def test_a_manually_entered_rate_is_never_overwritten_by_a_fetch(db):
    db.add(CurrencyRate(code="USD", as_of=date(2026, 8, 18),
                        inr_per_unit=Decimal("95.00"), source="manual"))
    db.flush()

    report = _report()
    store_quotes(db, [_quote("USD", "95.40")], report)

    # Asserted on the stored row rather than on log wording: what matters is
    # that the number and its provenance survived, not how it was phrased.
    assert not report.updated
    stored = db.query(CurrencyRate).filter_by(code="USD", as_of=date(2026, 8, 18)).one()
    assert stored.inr_per_unit == Decimal("95.00")
    assert stored.source == "manual"
    assert report.conflicts and report.conflicts[0]["code"] == "USD"


def test_a_currency_that_is_not_configured_is_refused(db):
    report = _report()
    store_quotes(db, [_quote("XYZ", "50")], report)
    assert report.rejected and not report.updated


def test_re_fetching_the_same_day_corrects_rather_than_duplicates(db):
    store_quotes(db, [_quote("USD", "95.40")], _report())
    db.flush()
    store_quotes(db, [_quote("USD", "95.60")], _report())
    db.flush()

    rows = db.query(CurrencyRate).filter_by(code="USD", as_of=date(2026, 8, 18)).all()
    assert len(rows) == 1
    assert rows[0].inr_per_unit == Decimal("95.60")


def test_provenance_is_recorded_so_an_rbi_figure_stays_distinguishable(db):
    store_quotes(db, [
        _quote("USD", "95.40", source="rbi"),
        _quote("SGD", "74.73", source="frankfurter"),
    ], _report())
    db.flush()

    by_code = {r.code: r.source for r in db.query(CurrencyRate).filter(
        CurrencyRate.as_of == date(2026, 8, 18)).all()}
    assert by_code["USD"] == "rbi"
    assert by_code["SGD"] == "frankfurter"


# --- Manual-vs-fetched conflict resolution --------------------------------

def test_a_differing_fetch_is_reported_not_applied(db):
    """The default must never silently replace a hand-entered rate."""
    db.add(CurrencyRate(code="USD", as_of=date(2026, 8, 18),
                        inr_per_unit=Decimal("95.00"), source="manual"))
    db.flush()

    report = _report()
    store_quotes(db, [_quote("USD", "91.20", source="rbi")], report)

    assert not report.updated
    assert len(report.conflicts) == 1
    conflict = report.conflicts[0]
    assert conflict["code"] == "USD"
    assert conflict["manual_rate"] == 95.0
    assert conflict["fetched_rate"] == 91.2
    assert conflict["fetched_source"] == "rbi"
    assert conflict["difference_pct"] == pytest.approx(-4.0)

    stored = db.query(CurrencyRate).filter_by(code="USD", as_of=date(2026, 8, 18)).one()
    assert stored.inr_per_unit == Decimal("95.00")


def test_the_users_decision_replaces_their_rate_and_its_provenance(db):
    db.add(CurrencyRate(code="USD", as_of=date(2026, 8, 18),
                        inr_per_unit=Decimal("95.00"), source="manual"))
    db.flush()

    report = _report()
    store_quotes(db, [_quote("USD", "91.20", source="rbi")], report,
                 overwrite_manual=["USD"])

    assert report.updated and not report.conflicts
    stored = db.query(CurrencyRate).filter_by(code="USD", as_of=date(2026, 8, 18)).one()
    assert stored.inr_per_unit == Decimal("91.20")
    # Provenance follows the value: this row is now the RBI rate, not a manual one.
    assert stored.source == "rbi"


def test_a_decision_applies_only_to_the_currency_it_named(db):
    for code, rate in (("USD", "95.00"), ("SGD", "70.00")):
        db.add(CurrencyRate(code=code, as_of=date(2026, 8, 18),
                            inr_per_unit=Decimal(rate), source="manual"))
    db.flush()

    report = _report()
    store_quotes(db, [
        _quote("USD", "91.20", source="rbi"),
        _quote("SGD", "74.73", source="frankfurter"),
    ], report, overwrite_manual=["USD"])

    usd = db.query(CurrencyRate).filter_by(code="USD", as_of=date(2026, 8, 18)).one()
    sgd = db.query(CurrencyRate).filter_by(code="SGD", as_of=date(2026, 8, 18)).one()
    assert usd.inr_per_unit == Decimal("91.20") and usd.source == "rbi"
    assert sgd.inr_per_unit == Decimal("70.00") and sgd.source == "manual"
    assert [c["code"] for c in report.conflicts] == ["SGD"]


def test_a_manual_rate_that_already_agrees_raises_no_conflict(db):
    db.add(CurrencyRate(code="USD", as_of=date(2026, 8, 18),
                        inr_per_unit=Decimal("91.20"), source="manual"))
    db.flush()

    report = _report()
    store_quotes(db, [_quote("USD", "91.20", source="rbi")], report)

    assert not report.conflicts and not report.updated
    assert any("already matches" in line for line in report.unchanged)


def test_a_manual_rate_far_from_market_surfaces_as_a_conflict_not_a_rejection(db):
    """The bug this pins.

    The step gate used to run against the manual rate, so a hand-entered value
    more than 10% from market was rejected before the conflict path was reached.
    The user was never asked, and "Use fetched" had nothing to apply — the rate
    was simply stuck. The gate now compares source-to-source.
    """
    db.add(CurrencyRate(code="USD", as_of=date(2026, 8, 18),
                        inr_per_unit=Decimal("70.00"), source="manual"))
    db.flush()

    report = _report()
    store_quotes(db, [_quote("USD", "95.51", source="frankfurter")], report)

    assert not report.rejected, report.rejected
    assert len(report.conflicts) == 1
    assert report.conflicts[0]["difference_pct"] == pytest.approx(36.44, abs=0.1)

    # And the decision can actually be applied.
    applied = _report()
    store_quotes(db, [_quote("USD", "95.51", source="frankfurter")], applied,
                 overwrite_manual=["USD"])
    assert applied.updated and not applied.rejected
    stored = db.query(CurrencyRate).filter_by(code="USD", as_of=date(2026, 8, 18)).one()
    assert stored.inr_per_unit == Decimal("95.51")


def test_the_gate_still_catches_a_regressed_source(db):
    """Excluding manual/seed baselines must not disarm the gate itself."""
    store_quotes(db, [_quote("USD", "95.51", source="frankfurter")], _report())
    db.flush()

    report = _report()
    store_quotes(db, [_quote("USD", "9551.0", day=date(2026, 8, 19),
                             source="frankfurter")], report)
    assert report.rejected and not report.updated


# --- Back-off for a source that keeps refusing ----------------------------

def test_a_refusing_source_is_left_alone_after_repeated_failures(monkeypatch):
    """RBI answers HTTP 418 to every automated request, including robots.txt.

    Retrying that every 30 minutes forever is pointless traffic aimed at
    somebody else's server, and it buries real errors under the same one. A
    refusal counts toward a cooldown; a timeout does not, because a timeout may
    genuinely succeed next time.
    """
    from app.config import settings as app_settings
    from app.currency import refresher

    refresher._FAILURES.clear()
    refresher._COOLDOWN_UNTIL.clear()

    for _ in range(app_settings.FX_SOURCE_FAILURE_LIMIT):
        refresher._note_failure("rbi", retryable=False)

    assert refresher._in_cooldown("rbi") is not None
    assert refresher.source_health()["rbi"]["skipped_until"] is not None

    # A success clears it immediately — the block may have been temporary.
    refresher._note_success("rbi")
    assert refresher._in_cooldown("rbi") is None


def test_timeouts_do_not_trip_the_back_off():
    from app.config import settings as app_settings
    from app.currency import refresher

    refresher._FAILURES.clear()
    refresher._COOLDOWN_UNTIL.clear()

    for _ in range(app_settings.FX_SOURCE_FAILURE_LIMIT + 2):
        refresher._note_failure("frankfurter", retryable=True)

    assert refresher._in_cooldown("frankfurter") is None


def test_a_market_rate_never_displaces_an_official_one_for_the_same_day(db):
    """FBIL lags by days, so the aggregate covers dates it has not reached yet.

    Without a rank, a later poll covering an already-official date would swap
    the badge from `fbil` to `frankfurter` and nothing would look wrong — but a
    figure someone cited as India's official rate would no longer be it.
    """
    store_quotes(db, [_quote("USD", "95.2381", source="fbil")], _report())
    db.flush()

    report = _report()
    store_quotes(db, [_quote("USD", "95.5110", source="frankfurter")], report)

    stored = db.query(CurrencyRate).filter_by(code="USD", as_of=date(2026, 8, 18)).one()
    assert stored.source == "fbil"
    assert stored.inr_per_unit == Decimal("95.2381")
    assert any("rather than downgrade" in line for line in report.unchanged)


def test_an_official_rate_may_correct_a_market_one(db):
    """The rank is one-way: FBIL catching up on a date must be allowed to land."""
    store_quotes(db, [_quote("USD", "95.5110", source="frankfurter")], _report())
    db.flush()

    store_quotes(db, [_quote("USD", "95.2381", source="fbil")], _report())
    db.flush()

    stored = db.query(CurrencyRate).filter_by(code="USD", as_of=date(2026, 8, 18)).one()
    assert stored.source == "fbil"
