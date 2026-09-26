"""Report period resolution.

`today` is injected throughout so these do not have to be re-dated every month —
a test that starts failing in April because the financial year rolled over is a
test nobody trusts.
"""

from datetime import date

import pytest

from app.api import treasury_periods as tp


TODAY = date(2026, 8, 18)      # a Tuesday in Q2 of FY 2026-27


@pytest.mark.parametrize("key,start,end,prev_start,prev_end", [
    (tp.CURRENT_MONTH, date(2026, 8, 1), date(2026, 8, 31), date(2026, 7, 1), date(2026, 7, 31)),
    (tp.LAST_MONTH,    date(2026, 7, 1), date(2026, 7, 31), date(2026, 6, 1), date(2026, 6, 30)),
    # Quarters count from April, not January: Q2 of an Indian FY is Jul-Sep.
    (tp.CURRENT_QUARTER, date(2026, 7, 1), date(2026, 9, 30), date(2026, 4, 1), date(2026, 6, 30)),
    (tp.LAST_QUARTER,    date(2026, 4, 1), date(2026, 6, 30), date(2026, 1, 1), date(2026, 3, 31)),
    (tp.FINANCIAL_YEAR,  date(2026, 4, 1), date(2027, 3, 31), date(2025, 4, 1), date(2026, 3, 31)),
    (tp.LAST_FINANCIAL_YEAR, date(2025, 4, 1), date(2026, 3, 31), date(2024, 4, 1), date(2025, 3, 31)),
])
def test_each_preset_resolves_to_the_right_bounds(key, start, end, prev_start, prev_end):
    p = tp.resolve(key, today=TODAY)
    assert (p.start, p.end) == (start, end)
    assert (p.prev_start, p.prev_end) == (prev_start, prev_end)


def test_the_financial_year_runs_april_to_march():
    """A calendar year would put Q4 in the wrong year for every Indian filing."""
    assert tp.resolve(tp.FINANCIAL_YEAR, today=date(2026, 3, 31)).start == date(2025, 4, 1)
    assert tp.resolve(tp.FINANCIAL_YEAR, today=date(2026, 4, 1)).start == date(2026, 4, 1)


def test_the_previous_period_is_the_same_kind_of_period_not_minus_thirty_days():
    """February against January is a comparison a finance team recognises."""
    p = tp.resolve(tp.LAST_MONTH, today=date(2026, 3, 15))   # February
    assert (p.start, p.end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert (p.prev_start, p.prev_end) == (date(2026, 1, 1), date(2026, 1, 31))
    assert (p.end - p.start).days != (p.prev_end - p.prev_start).days   # 28 vs 31


def test_a_year_boundary_does_not_break_the_previous_month():
    p = tp.resolve(tp.CURRENT_MONTH, today=date(2026, 1, 9))
    assert (p.start, p.end) == (date(2026, 1, 1), date(2026, 1, 31))
    assert (p.prev_start, p.prev_end) == (date(2025, 12, 1), date(2025, 12, 31))


def test_a_leap_year_february_is_twenty_nine_days():
    p = tp.resolve(tp.CURRENT_MONTH, today=date(2028, 2, 10))
    assert p.end == date(2028, 2, 29)


def test_a_custom_range_compares_against_an_equally_long_span_before_it():
    p = tp.resolve(tp.CUSTOM, "2026-03-01", "2026-03-31", today=TODAY)
    assert (p.start, p.end) == (date(2026, 3, 1), date(2026, 3, 31))
    assert p.prev_end == date(2026, 2, 28)
    assert (p.end - p.start).days == (p.prev_end - p.prev_start).days


def test_explicit_dates_win_over_the_preset():
    """Somebody who typed a range meant that range, whatever the dropdown says."""
    p = tp.resolve(tp.CURRENT_MONTH, "2024-01-01", "2024-01-31", today=TODAY)
    assert (p.start, p.end) == (date(2026, 8, 1), date(2026, 8, 31))

    p = tp.resolve(None, "2024-01-01", "2024-01-31", today=TODAY)
    assert (p.start, p.end) == (date(2024, 1, 1), date(2024, 1, 31))


def test_all_time_has_no_bounds_and_no_comparison():
    """There is nothing before all of time; a variance here would be invented."""
    p = tp.resolve(tp.ALL_TIME, today=TODAY)
    assert p.start is None and p.end is None
    assert p.prev_start is None and p.prev_end is None
    assert p.as_dict()["previous"] is None


def test_an_unknown_key_falls_back_to_the_current_month():
    p = tp.resolve("nonsense", today=TODAY)
    assert p.key == tp.CURRENT_MONTH


def test_the_endpoint_rejects_an_unknown_period(client):
    import uuid
    email = f"tp_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    tok = client.post("/auth/login",
                      json={"email": email, "password": "Password123!"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}

    assert client.get("/reports/treasury?period=next_tuesday", headers=h).status_code == 400
    ok = client.get("/reports/treasury?period=last_month", headers=h)
    assert ok.status_code == 200
    assert ok.json()["period"]["key"] == "last_month"
