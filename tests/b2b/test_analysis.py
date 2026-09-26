"""Tests for the B2B analysis layer.

Every fixture is built in code and every expected figure is worked out by hand
in a comment beside the assertion. That is the point of this file: a test that
asserts `total_credits > 0` would pass against an implementation that summed the
wrong column, and a test that asserts against a value the code itself produced
would pass against any implementation at all.

The base fixture is six months of a plausible salaried account:

    day  1   +78,000.00   SALARY CREDIT ACME TECHNOLOGIES PVT LTD
    day  5   -12,500.00   EMI HDFC HOME LOAN 5001234
    day  7   -22,000.00   NEFT RENT PAYMENT LANDLORD
    day 12    -6,000.00   UPI/BIGBASKET GROCERIES
    day 20    -5,000.00   ATM CASH WDL BRANCH MUMBAI

    per month   in  78,000.00  out  45,500.00  net  32,500.00
    six months  in 468,000.00  out 273,000.00  net 195,000.00
"""
import datetime

import pytest

from app.b2b.analysis import NOT_VERIFIABLE, PASSED, analyze
from app.b2b.analysis.affordability import emi_for
from app.b2b.canonical import CanonicalTxn

# ---------------------------------------------------------------- the fixture

OPENING_PAISE = 100_000_00

SALARY_PAISE = 78_000_00
EMI_PAISE = 12_500_00
RENT_PAISE = 22_000_00
GROCERIES_PAISE = 6_000_00
ATM_PAISE = 5_000_00

MONTHLY_ROWS = [
    # (narration, day of month, direction, paise)
    ("SALARY CREDIT ACME TECHNOLOGIES PVT LTD", 1, "credit", SALARY_PAISE),
    ("EMI HDFC HOME LOAN 5001234", 5, "debit", EMI_PAISE),
    ("NEFT RENT PAYMENT LANDLORD", 7, "debit", RENT_PAISE),
    ("UPI/BIGBASKET GROCERIES", 12, "debit", GROCERIES_PAISE),
    ("ATM CASH WDL BRANCH MUMBAI", 20, "debit", ATM_PAISE),
]

MONTHS = 6
MONTHLY_IN_PAISE = SALARY_PAISE
MONTHLY_OUT_PAISE = EMI_PAISE + RENT_PAISE + GROCERIES_PAISE + ATM_PAISE  # 45,500.00
TOTAL_IN_PAISE = MONTHLY_IN_PAISE * MONTHS                                # 468,000.00
TOTAL_OUT_PAISE = MONTHLY_OUT_PAISE * MONTHS                              # 273,000.00
NET_PAISE = TOTAL_IN_PAISE - TOTAL_OUT_PAISE                              # 195,000.00


def build_statement(with_balance=True, extra_rows=None):
    """Six months, January to June 2025, in statement order."""
    txns, balance, index = [], OPENING_PAISE, 0
    for month in range(1, MONTHS + 1):
        for narration, day, direction, amount in MONTHLY_ROWS:
            balance += amount if direction == "credit" else -amount
            txns.append(CanonicalTxn(
                txn_date=datetime.date(2025, month, day),
                direction=direction,
                credit_paise=amount if direction == "credit" else None,
                debit_paise=amount if direction == "debit" else None,
                balance_paise=balance if with_balance else None,
                narration_raw=narration,
                narration_clean=narration,
                row_index=index,
                source_format="csv",
            ))
            index += 1
    for row in (extra_rows or []):
        row.row_index = index
        index += 1
        txns.append(row)
    return txns


@pytest.fixture(scope="module")
def analysis():
    return analyze(
        build_statement(),
        statement_meta={"bank": "HDFC Bank", "account_number_masked": "XXXX1234"},
        parse_quality={"confidence": 1.0},
        loan={"loan_amount": 500000, "interest_rate": 12.0, "tenure_months": 60},
    )


def metric(result, block, key):
    return result.data[block][key]


# ------------------------------------------------------------------- totals

def test_total_credits_is_the_exact_expected_paise(analysis):
    # 78,000.00 x 6 = 468,000.00 = 46,800,000 paise
    assert analysis.raw["income"]["total_credits_paise"] == 46_800_000
    assert analysis.raw["income"]["total_credits_paise"] == TOTAL_IN_PAISE
    assert metric(analysis, "income", "total_credits")["value"] == 468_000.00
    assert metric(analysis, "income", "total_credits")["source"] == "CALCULATED"


def test_total_debits_is_the_exact_expected_paise(analysis):
    # (12,500 + 22,000 + 6,000 + 5,000) x 6 = 273,000.00 = 27,300,000 paise
    assert analysis.raw["expenses"]["total_debits_paise"] == 27_300_000
    assert analysis.raw["expenses"]["total_debits_paise"] == TOTAL_OUT_PAISE
    assert metric(analysis, "expenses", "total_debits")["value"] == 273_000.00


def test_monthly_income_divides_by_the_six_calendar_months_covered(analysis):
    assert analysis.raw["income"]["months"] == 6
    assert analysis.raw["income"]["monthly_income_paise"] == 7_800_000
    assert metric(analysis, "income", "monthly_income")["value"] == 78_000.00


def test_monthly_series_has_one_entry_per_calendar_month(analysis):
    series = metric(analysis, "cashflow", "monthly_series")["value"]
    assert [row["month"] for row in series] == [
        "2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
    for row in series:
        assert row["inflow"] == 78_000.00
        assert row["outflow"] == 45_500.00
        assert row["net"] == 32_500.00
        assert row["count"] == 5


# ------------------------------------------------------------------- salary

def test_salary_is_detected_at_78000_with_real_confidence(analysis):
    salary = metric(analysis, "income", "salary")
    assert salary["source"] == "INFERRED"
    assert salary["value"] == 78_000.00
    assert salary["confidence"] > 0.7
    assert salary["confidence"] < 1.0

    evidence = salary["evidence"]
    assert evidence["series_name"] == "Salary Credit Acme Technologies"
    assert evidence["cadence"] == "monthly"
    assert evidence["occurrences"] == 6
    # A fixed credit on a fixed day: zero spread on both axes.
    assert evidence["amount_cv"] == 0.0
    assert evidence["day_of_month_stdev"] == 0.0
    assert "payroll_narration" in evidence["matched_evidence"]
    assert "category_tree_income_salary" in evidence["matched_evidence"]


def test_income_from_a_flat_series_is_perfectly_stable(analysis):
    # Every month is 78,000.00, so the coefficient of variation is 0 and
    # stability = 1 / (1 + 0) = 1.0. This is CALCULATED, not INFERRED, so 1.0
    # is a legitimate answer here.
    assert metric(analysis, "income", "income_volatility")["value"] == 0.0
    stability = metric(analysis, "income", "income_stability")
    assert stability["value"] == 1.0
    assert stability["source"] == "CALCULATED"


def test_one_distinct_income_source(analysis):
    sources = metric(analysis, "income", "income_sources")
    assert sources["value"] == 1
    assert sources["source"] == "INFERRED"


# ---------------------------------------------------------------------- debt

def test_the_emi_is_detected_at_12500_and_nothing_else_is(analysis):
    emis = metric(analysis, "debt", "detected_emis")["value"]
    assert len(emis) == 1
    emi = emis[0]
    assert emi["monthly_amount"] == 12_500.00
    assert emi["name"] == "Emi Hdfc Home Loan"
    assert emi["admitted_by"] == "named"
    assert "lender_or_emi_narration" in emi["evidence"]
    assert 0.7 < emi["confidence"] < 1.0

    assert analysis.raw["debt"]["total_monthly_emi_paise"] == 1_250_000
    assert metric(analysis, "debt", "total_monthly_emi")["value"] == 12_500.00
    assert metric(analysis, "debt", "emi_count")["value"] == 1


def test_rent_groceries_and_atm_are_not_mistaken_for_emis(analysis):
    """All three are fixed monthly outflows; only the loan is debt service.

    This is the guard that keeps every DTI in the response honest — without it a
    fixed monthly rent makes the borrower look three times as indebted.
    """
    names = {e["name"] for e in metric(analysis, "debt", "detected_emis")["value"]}
    assert "Rent Payment Landlord" not in names
    assert "Bigbasket Groceries" not in names
    assert "Atm Cash Wdl Branch" not in names


# ------------------------------------------------------------------ expenses

def test_essential_discretionary_split_follows_the_documented_mapping(analysis):
    # essential:      EMI 12,500 (Loans & Credit) + rent 22,000 (Housing)
    #                 + groceries 6,000 (Food & Dining > Groceries override)
    #                 = 40,500.00 a month -> 243,000.00 over six months
    # discretionary:  nothing in this fixture
    # unattributed:   the ATM withdrawal, 5,000.00 a month -> 30,000.00
    split = analysis.raw["expenses"]["split_paise"]
    assert split["essential"] == 24_300_000
    assert split["discretionary"] == 0
    assert split["unattributed"] == 3_000_000
    assert sum(split.values()) == TOTAL_OUT_PAISE

    assert metric(analysis, "expenses", "essential_expenses")["value"] == 243_000.00
    assert metric(analysis, "expenses", "essential_expenses")["source"] == "INFERRED"
    assert metric(analysis, "expenses", "discretionary_expenses")["value"] == 0.0
    assert metric(analysis, "expenses", "unattributed_expenses")["value"] == 30_000.00


def test_the_essentiality_mapping_is_returned_so_it_can_be_audited(analysis):
    mapping = metric(analysis, "expenses", "essentiality_mapping")["value"]
    assert mapping["by_root"]["Housing"] == "essential"
    assert mapping["by_root"]["Entertainment"] == "discretionary"
    assert mapping["by_root"]["Cash"] == "unattributed"
    assert mapping["by_path"]["Food & Dining > Groceries"] == "essential"


def test_cash_withdrawals_are_identified_from_narration_and_rail(analysis):
    cash = metric(analysis, "expenses", "cash_withdrawals")
    assert cash["value"] == 30_000.00          # 5,000.00 x 6
    assert cash["source"] == "INFERRED"
    assert metric(analysis, "expenses", "cash_withdrawal_count")["value"] == 6


def test_recurring_expenses_come_from_the_series_detector(analysis):
    names = {s["name"] for s in
             metric(analysis, "expenses", "recurring_expenses")["value"]}
    assert names == {"Emi Hdfc Home Loan", "Rent Payment Landlord",
                     "Bigbasket Groceries", "Atm Cash Wdl Branch"}


# ------------------------------------------------------------------ cashflow

def test_savings_rate_matches_the_hand_computed_figure(analysis):
    # net 195,000.00 / income 468,000.00 = 0.4166666... -> 0.4167 at 4dp
    assert analysis.raw["cashflow"]["net_flow_paise"] == 19_500_000
    assert analysis.raw["cashflow"]["savings_rate"] == pytest.approx(
        19_500_000 / 46_800_000, abs=1e-12)
    assert metric(analysis, "cashflow", "savings_rate")["value"] == 0.4167
    assert metric(analysis, "cashflow", "monthly_surplus")["value"] == 32_500.00


def test_cashflow_stability_is_one_for_an_identical_net_every_month(analysis):
    assert metric(analysis, "cashflow", "cashflow_stability")["value"] == 1.0


# ------------------------------------------------------------------ balances

def test_opening_and_closing_reconcile_with_the_movement(analysis):
    raw = analysis.raw["balances"]
    # Opening is reconstructed by reversing the in-period movement out of the
    # first stated balance, so it must come back as the 100,000.00 the fixture
    # started from.
    assert raw["opening_paise"] == 10_000_000
    assert raw["basis"] == "reconstructed"
    # 100,000.00 + 195,000.00 = 295,000.00
    assert raw["closing_paise"] == 29_500_000
    assert raw["opening_paise"] + raw["net_movement_paise"] == raw["closing_paise"]
    assert metric(analysis, "balances", "closing_balance")["value"] == 295_000.00
    assert metric(analysis, "balances", "closing_balance")["source"] == "EXTRACTED"


def test_average_daily_balance_is_time_weighted_not_row_weighted():
    """Ten days, three rows, worked out by hand.

        opening                       4,900.00   (5,000.00 - the 100.00 credit)
        Mar 1  +100.00  -> balance    5,000.00
        Mar 6  -200.00  -> balance    4,800.00
        Mar 10 +500.00  -> balance    5,300.00

        Mar  1- 5   5,000.00 x 5 =  25,000.00
        Mar  6- 9   4,800.00 x 4 =  19,200.00
        Mar 10      5,300.00 x 1 =   5,300.00
                                    ----------
                    over 10 days =  49,500.00  ->  ADB 4,950.00

    The row-weighted mean of the same balance column is
    (5,000 + 4,800 + 5,300) / 3 = 5,033.33, which is the number a naive
    implementation returns. The two must not be equal.
    """
    rows = [
        CanonicalTxn(txn_date=datetime.date(2025, 3, 1), direction="credit",
                     credit_paise=100_00, balance_paise=5_000_00,
                     narration_raw="NEFT INWARD ALPHA",
                     narration_clean="NEFT INWARD ALPHA", row_index=0),
        CanonicalTxn(txn_date=datetime.date(2025, 3, 6), direction="debit",
                     debit_paise=200_00, balance_paise=4_800_00,
                     narration_raw="UPI/KIRANA STORE",
                     narration_clean="UPI/KIRANA STORE", row_index=1),
        CanonicalTxn(txn_date=datetime.date(2025, 3, 10), direction="credit",
                     credit_paise=500_00, balance_paise=5_300_00,
                     narration_raw="NEFT INWARD BETA",
                     narration_clean="NEFT INWARD BETA", row_index=2),
    ]
    result = analyze(rows, parse_quality={"confidence": 1.0})
    raw = result.raw["balances"]

    assert raw["days_in_period"] == 10
    assert raw["opening_paise"] == 4_900_00
    assert raw["average_daily_paise"] == 495_000          # 4,950.00
    assert result.data["balances"]["average_daily_balance"]["value"] == 4_950.00
    # Row-weighted, for contrast: 5,033.33, and deliberately different.
    assert raw["average_paise"] == 503_333
    assert raw["average_daily_paise"] != raw["average_paise"]
    # Every balance step equals the transaction between the rows.
    assert result.quality["reconciliation_status"] == PASSED


def test_min_and_max_balance_are_read_off_the_column(analysis):
    lows = metric(analysis, "balances", "min_balance")
    assert lows["source"] == "EXTRACTED"
    # The lowest point is right after the first month's ATM withdrawal:
    # 100,000 + 78,000 - 12,500 - 22,000 - 6,000 - 5,000 = 132,500.00
    assert lows["value"] == 132_500.00
    # The highest is right after the last salary credit, before June's four
    # debits take it back down to the 295,000.00 close:
    # 295,000 + 45,500 = 340,500.00
    assert metric(analysis, "balances", "max_balance")["value"] == 340_500.00


# ------------------------------------------------------- no balance column

def test_a_statement_with_no_balance_column_is_not_verifiable():
    result = analyze(build_statement(with_balance=False),
                     parse_quality={"confidence": 1.0})

    assert result.quality["reconciliation_status"] == NOT_VERIFIABLE
    codes = {w["code"] for w in result.warnings}
    assert "NO_BALANCE_COLUMN" in codes
    assert "CONTINUITY_UNVERIFIABLE" in codes


def test_missing_balances_return_unavailable_and_never_zero():
    result = analyze(build_statement(with_balance=False),
                     parse_quality={"confidence": 1.0})
    balances = result.data["balances"]

    for key in ("opening_balance", "closing_balance", "min_balance",
                "max_balance", "average_balance", "average_daily_balance"):
        assert balances[key]["value"] is None, key
        assert balances[key]["value"] != 0
        assert balances[key]["note"], f"{key} must say why it is unavailable"

    # Net movement IS computable without a balance column, and is not a balance.
    assert balances["net_movement"]["value"] == 195_000.00

    # Everything that hangs off a balance follows it into unavailable rather
    # than quietly reporting a figure derived from an assumed zero.
    assert result.data["cashflow"]["forecast_30_60_90"]["value"] is None
    assert result.data["risk"]["negative_balance_days"]["value"] is None


def test_an_unverifiable_statement_is_capped_at_0_75():
    """The continuity term is zeroed and the weights are NOT renormalised.

    parse 1.0 x 0.50 + continuity 0.0 x 0.25 + coverage 1.0 x 0.25 = 0.75.
    """
    result = analyze(build_statement(with_balance=False),
                     parse_quality={"confidence": 1.0})
    components = result.quality["confidence_components"]
    assert components["parse_confidence"] == 1.0
    assert components["balance_continuity"] == 0.0
    assert components["classification_coverage"] == 1.0
    assert result.quality["overall_confidence"] == 0.75


def test_overall_confidence_is_the_documented_weighted_sum(analysis):
    components = analysis.quality["confidence_components"]
    expected = (0.50 * components["parse_confidence"]
                + 0.25 * components["balance_continuity"]
                + 0.25 * components["classification_coverage"])
    assert analysis.quality["overall_confidence"] == pytest.approx(expected, abs=5e-5)
    assert analysis.quality["reconciliation_status"] == PASSED


def test_a_broken_balance_chain_fails_reconciliation():
    """Move one balance by 1,000.00 and the statement stops reconciling."""
    txns = build_statement()
    txns[10].balance_paise += 1_000_00
    result = analyze(txns, parse_quality={"confidence": 1.0})

    assert result.quality["reconciliation_status"] == "FAILED"
    assert "BALANCE_CONTINUITY_FAILED" in {w["code"] for w in result.warnings}
    assert result.quality["confidence_components"]["balance_continuity"] < 1.0
    assert result.quality["overall_confidence"] < 1.0


# ------------------------------------------------------------- affordability

def test_proposed_emi_matches_the_hand_computed_amortisation():
    """500,000 at 12% a year over 60 months.

        r = 0.12 / 12 = 0.01,  n = 60
        (1.01)^60 = 1.8166966986...
        EMI = 500000 x 0.01 x 1.8166967 / 0.8166967 = 11,122.22
    """
    assert emi_for(500000, 12.0, 60) == pytest.approx(11122.22, abs=0.005)
    # A zero-rate loan is principal over tenure, not a division by zero.
    assert emi_for(120000, 0.0, 12) == pytest.approx(10000.00, abs=0.005)
    assert emi_for(0, 12.0, 60) is None
    assert emi_for(500000, 12.0, 0) is None


def test_affordability_ratios_against_the_detected_income_and_emi(analysis):
    aff = analysis.raw["affordability"]
    assert aff["income_basis"] == "detected_salary"
    assert aff["income_paise"] == 7_800_000            # 78,000.00 a month
    assert aff["existing_emi_paise"] == 1_250_000      # 12,500.00 a month
    assert aff["proposed_emi_paise"] == 1_112_222      # 11,122.22

    # 12,500 / 78,000 = 0.160256...
    assert aff["existing_emi_to_income"] == pytest.approx(0.160256, abs=1e-6)
    # 11,122.22 / 78,000 = 0.142593...
    assert aff["proposed_dti"] == pytest.approx(0.142593, abs=1e-6)
    # (12,500 + 11,122.22) / 78,000 = 0.302849...
    assert aff["total_dti"] == pytest.approx(0.302849, abs=1e-6)
    # 78,000 income - 45,500 average outgoings = 32,500.00 disposable
    assert aff["disposable_income_paise"] == 3_250_000

    # 30.3% total DTI is inside the documented 40% comfortable band, and
    # 21,377.78 left over clears the 15,000.00 floor.
    assert aff["verdict"] == "AFFORDABLE"


def test_loan_block_echoes_the_request_and_totals_the_repayment(analysis):
    loan = analysis.data["loan"]
    assert loan["requested_amount"]["value"] == 500000.0
    assert loan["tenure_months"]["value"] == 60
    assert loan["proposed_emi"]["value"] == pytest.approx(11122.22, abs=0.005)
    # 11,122.22 x 60 = 667,333.20, of which 167,333.20 is interest.
    assert loan["total_repayable"]["value"] == pytest.approx(667333.20, abs=0.5)
    assert loan["total_interest"]["value"] == pytest.approx(167333.20, abs=0.5)


def test_without_loan_parameters_only_the_independent_ratios_are_returned():
    result = analyze(build_statement(), parse_quality={"confidence": 1.0})
    aff = result.data["affordability"]

    assert aff["existing_emi_to_income"]["value"] == pytest.approx(0.1603, abs=1e-4)
    assert aff["disposable_income"]["value"] == 32_500.00
    for key in ("proposed_emi", "proposed_dti", "total_dti", "verdict"):
        assert aff[key]["value"] is None
        assert "loan parameters" in aff[key]["note"]
    assert result.data["loan"] is None


# -------------------------------------------------------------------- risk

def test_penal_and_bounce_charges_are_picked_up():
    extra = [
        CanonicalTxn(txn_date=datetime.date(2025, 3, 22), direction="debit",
                     debit_paise=590_00, balance_paise=None,
                     narration_raw="PENAL CHARGES ECS RETURN MAR",
                     narration_clean="PENAL CHARGES ECS RETURN MAR"),
        CanonicalTxn(txn_date=datetime.date(2025, 4, 22), direction="debit",
                     debit_paise=413_00, balance_paise=None,
                     narration_raw="BOUNCE CHG CHEQUE RETURN APR",
                     narration_clean="BOUNCE CHG CHEQUE RETURN APR"),
    ]
    result = analyze(build_statement(with_balance=False, extra_rows=extra),
                     parse_quality={"confidence": 1.0})
    charges = result.data["risk"]["penalty_and_bounce_charges"]
    # 590.00 + 413.00 = 1,003.00
    assert charges["value"] == 1_003.00
    assert result.data["risk"]["penalty_charge_count"]["value"] == 2
    assert charges["source"] == "INFERRED"
    assert result.data["risk"]["financial_stress_score"]["value"] > 0


def test_a_clean_statement_scores_no_stress(analysis):
    components = analysis.raw["risk"]["stress_components"]
    assert components == {"penal_charges": 0.0, "negative_days": 0.0,
                          "min_balance_breaches": 0.0, "anomalies": 0.0}
    assert metric(analysis, "risk", "financial_stress_score")["value"] == 0.0


def test_negative_balances_are_counted_by_day_not_by_row():
    """Overdrawn on the 5th, back in credit on the 9th: four days negative."""
    rows = [
        CanonicalTxn(txn_date=datetime.date(2025, 5, 1), direction="credit",
                     credit_paise=1_000_00, balance_paise=1_000_00,
                     narration_raw="NEFT INWARD", narration_clean="NEFT INWARD",
                     row_index=0),
        CanonicalTxn(txn_date=datetime.date(2025, 5, 5), direction="debit",
                     debit_paise=1_500_00, balance_paise=-500_00,
                     narration_raw="UPI/VENDOR PAYMENT",
                     narration_clean="UPI/VENDOR PAYMENT", row_index=1),
        CanonicalTxn(txn_date=datetime.date(2025, 5, 9), direction="credit",
                     credit_paise=2_000_00, balance_paise=1_500_00,
                     narration_raw="NEFT INWARD", narration_clean="NEFT INWARD",
                     row_index=2),
    ]
    result = analyze(rows, parse_quality={"confidence": 1.0})
    # 5th, 6th, 7th, 8th are all overdrawn; the 9th is not.
    assert result.data["risk"]["negative_balance_days"]["value"] == 4
    assert result.data["risk"]["overdraft_usage"]["value"]["deepest_overdraft"] == 500.00
    # The 1st to the 4th sit at exactly 1,000.00, which is not *below* the
    # 1,000.00 default threshold, so only the four overdrawn days breach it.
    assert result.data["risk"]["min_balance_breaches"]["value"] == 4


def test_compliance_is_scored_over_applicable_checks_only(analysis):
    compliance = metric(analysis, "risk", "compliance")["value"]
    assert compliance["checks_applied"] > 0
    # Nothing in this fixture is anywhere near a 1,00,000+ corporate limit.
    assert compliance["checks_failed"] == 0
    assert compliance["pass_rate"] == 100.0
    # A rule that applied to nothing must not be scored at all.
    for rule in compliance["per_rule"]:
        if rule["applicable"] == 0:
            assert rule["pass_pct"] is None


# ----------------------------------------------------- the provenance rules

def _walk_metrics(node):
    """Yield every dict in the payload that looks like a rendered Metric."""
    if isinstance(node, dict):
        if "source" in node and "value" in node and isinstance(node["source"], str):
            yield node
        for value in node.values():
            yield from _walk_metrics(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_metrics(item)


def test_no_inferred_metric_ever_claims_certainty(analysis):
    inferred = [m for m in _walk_metrics(analysis.data)
                if m["source"] == "INFERRED"]
    assert inferred, "the fixture must exercise at least one inferred metric"
    for m in inferred:
        assert m.get("confidence") is not None, m
        assert m["confidence"] < 1.0, m
        assert m["confidence"] <= 0.97, m
        assert m.get("method"), m


def test_every_metric_declares_a_known_source(analysis):
    for m in _walk_metrics(analysis.data):
        assert m["source"] in {"EXTRACTED", "CALCULATED", "INFERRED"}


def test_all_required_blocks_are_present(analysis):
    for key in ("statement", "transactions", "income", "expenses", "balances",
                "cashflow", "debt", "loan", "affordability", "risk",
                "financial_metrics"):
        assert key in analysis.data, key
    assert analysis.data["statement"]["transaction_count"] == 30
    assert analysis.data["statement"]["months_covered"] == 6
    assert analysis.data["statement"]["days_covered"] == 171   # 1 Jan to 20 Jun
    assert len(analysis.data["transactions"]) == 30


def test_the_payload_is_json_serialisable(analysis):
    import json
    json.dumps(analysis.to_api())


def test_financial_metrics_are_the_same_objects_as_their_blocks(analysis):
    headline = analysis.data["financial_metrics"]
    assert headline["savings_rate"] == analysis.data["cashflow"]["savings_rate"]
    assert headline["salary"] == analysis.data["income"]["salary"]
    assert headline["average_daily_balance"] == \
        analysis.data["balances"]["average_daily_balance"]


# ----------------------------------------------------------------- degenerate

def test_an_empty_statement_says_so_instead_of_returning_zeros():
    result = analyze([], parse_quality={"confidence": 1.0})
    assert result.quality["reconciliation_status"] == NOT_VERIFIABLE
    assert result.quality["overall_confidence"] == 0.0
    assert "NO_TRANSACTIONS" in {w["code"] for w in result.warnings}
    assert result.data["transactions"] == []


def test_a_short_statement_is_flagged_rather_than_silently_averaged():
    rows = [
        CanonicalTxn(txn_date=datetime.date(2025, 7, 1), direction="credit",
                     credit_paise=50_000_00, balance_paise=50_000_00,
                     narration_raw="NEFT INWARD ONE OFF",
                     narration_clean="NEFT INWARD ONE OFF", row_index=0),
        CanonicalTxn(txn_date=datetime.date(2025, 7, 3), direction="debit",
                     debit_paise=1_000_00, balance_paise=49_000_00,
                     narration_raw="UPI/SHOP", narration_clean="UPI/SHOP",
                     row_index=1),
    ]
    result = analyze(rows, parse_quality={"confidence": 1.0})
    codes = {w["code"] for w in result.warnings}
    assert "SHORT_HISTORY" in codes
    assert "SPARSE_HISTORY" in codes
    # Too little history for a projection, and it says why rather than guessing.
    assert result.data["cashflow"]["forecast_30_60_90"]["value"] is None
    assert "days of history" in result.data["cashflow"]["forecast_30_60_90"]["note"]


def test_no_salary_series_is_reported_as_unavailable_not_as_zero():
    rows = [
        CanonicalTxn(txn_date=datetime.date(2025, m, 4), direction="credit",
                     credit_paise=9_000_00 + m * 1_000_00,
                     balance_paise=9_000_00 + m * 1_000_00,
                     narration_raw=f"UPI/CUSTOMER {m} INVOICE",
                     narration_clean=f"UPI/CUSTOMER {m} INVOICE", row_index=m)
        for m in range(1, 7)
    ]
    result = analyze(rows, parse_quality={"confidence": 1.0})
    salary = result.data["income"]["salary"]
    assert salary["value"] is None
    assert "NO_SALARY_DETECTED" in {w["code"] for w in result.warnings}
    # Affordability falls back to average credits and says which basis it used.
    assert result.raw["affordability"]["income_basis"] == "average_monthly_credits"
