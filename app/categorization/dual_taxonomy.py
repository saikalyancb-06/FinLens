"""Two-axis transaction labelling.

A transaction answers two independent questions, and forcing them into one field
is what produced a single `NEFT Transfer` bucket holding 853 transactions across
43 different counterparties — PhonePe settlements, supplier payments and payroll
all filed together because they shared a payment rail.

    PURPOSE     what the money was FOR      -> Sales Income, Salary, Bank Fees
    EVENT TYPE  what kind of business event -> Merchant Settlement, Payroll

PURPOSE is required and leads the dashboard and reports: it is the axis a P&L is
built from. EVENT TYPE is optional, because some rows genuinely have no
commercial event behind them — a bank fee has a purpose but no counterparty.

The payment rail (NEFT / IMPS / UPI / RTGS) is deliberately absent from both.
It already lives on Transaction.payment_method, and duplicating it as a category
is what created the 853-row bucket in the first place.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Axis 1 — PURPOSE (required). Plain English: what was the money for?
# ---------------------------------------------------------------------------
SALES_INCOME      = "Sales Income"
OTHER_INCOME      = "Other Income"
COST_OF_GOODS     = "Cost of Goods"
SALARY_WAGES      = "Salary & Wages"
RENT_PREMISES     = "Rent & Premises"
UTILITIES         = "Utilities & Bills"
BANK_FEES         = "Bank Fees"
FINANCE_COST      = "Interest & Finance Cost"
TAXES_STATUTORY   = "Taxes & Statutory"
PROFESSIONAL_FEES = "Professional Fees"
OWNER_FUNDING     = "Owner Funding"
LOANS             = "Loans & Borrowing"
INTERNAL_MOVEMENT = "Internal Movement"
FOOD_DINING       = "Food & Dining"
TRAVEL            = "Travel"
TRANSPORTATION    = "Transportation"
HEALTHCARE        = "Healthcare"
OTHER_PURPOSE     = "Other"

PURPOSES = [
    SALES_INCOME, OTHER_INCOME, COST_OF_GOODS, SALARY_WAGES, RENT_PREMISES,
    UTILITIES, BANK_FEES, FINANCE_COST, TAXES_STATUTORY, PROFESSIONAL_FEES,
    OWNER_FUNDING, LOANS, INTERNAL_MOVEMENT, FOOD_DINING, TRAVEL,
    TRANSPORTATION, HEALTHCARE, OTHER_PURPOSE,
]
PURPOSE_SET = set(PURPOSES)

# Which purposes are income vs expense vs neither. Reports need this so an
# internal transfer is never counted as revenue.
INCOME_PURPOSES = {SALES_INCOME, OTHER_INCOME, OWNER_FUNDING}
NEUTRAL_PURPOSES = {INTERNAL_MOVEMENT, LOANS}


# ---------------------------------------------------------------------------
# Axis 2 — EVENT TYPE (optional). What kind of business event was this?
# ---------------------------------------------------------------------------
MERCHANT_SETTLEMENT = "Merchant Settlement"
CUSTOMER_RECEIPT    = "Customer Receipt"
VENDOR_PAYMENT      = "Vendor Payment"
PAYROLL             = "Payroll"
STATUTORY_PAYMENT   = "Statutory Payment"
INTERNAL_TRANSFER   = "Internal Transfer"
BANK_CHARGE         = "Bank Charge"
OWNER_CONTRIBUTION  = "Owner Contribution"
LOAN_MOVEMENT       = "Loan Movement"
CASH_MOVEMENT       = "Cash Movement"

EVENT_TYPES = [
    MERCHANT_SETTLEMENT, CUSTOMER_RECEIPT, VENDOR_PAYMENT, PAYROLL,
    STATUTORY_PAYMENT, INTERNAL_TRANSFER, BANK_CHARGE, OWNER_CONTRIBUTION,
    LOAN_MOVEMENT, CASH_MOVEMENT,
]
EVENT_TYPE_SET = set(EVENT_TYPES)


# ---------------------------------------------------------------------------
# Migration map: every legacy category observed in the live database, mapped
# onto (purpose, event_type). event_type None means "no commercial event".
#
# Rail-named legacy categories (NEFT/IMPS/UPI/RTGS Transfer) cannot be resolved
# from the name alone — the rail says nothing about purpose — so they map to
# None and must be re-derived from the narration instead of guessed.
# ---------------------------------------------------------------------------
LEGACY_MAP: Dict[str, Tuple[Optional[str], Optional[str]]] = {
    # --- unambiguous business events -------------------------------------
    "Merchant Settlement":              (SALES_INCOME, MERCHANT_SETTLEMENT),
    "Customer Payment / NEFT Transfer": (SALES_INCOME, CUSTOMER_RECEIPT),
    "Vendor Payment":                   (COST_OF_GOODS, VENDOR_PAYMENT),
    "Salary Payment":                   (SALARY_WAGES, PAYROLL),
    "Salary / Income":                  (SALARY_WAGES, PAYROLL),
    "GST/Tax Payment":                  (TAXES_STATUTORY, STATUTORY_PAYMENT),
    "Government Fee":                   (TAXES_STATUTORY, STATUTORY_PAYMENT),
    "Bank Charges":                     (BANK_FEES, BANK_CHARGE),
    "Internal Fund Transfer":           (INTERNAL_MOVEMENT, INTERNAL_TRANSFER),
    "Transfers":                        (INTERNAL_MOVEMENT, INTERNAL_TRANSFER),
    "Cash Deposit":                     (INTERNAL_MOVEMENT, CASH_MOVEMENT),
    "Loan Disbursement":                (LOANS, LOAN_MOVEMENT),
    "Loan Repayment / EMI":             (LOANS, LOAN_MOVEMENT),
    "Insurance Premium":                (PROFESSIONAL_FEES, VENDOR_PAYMENT),
    "Interest Credit":                  (OTHER_INCOME, None),
    "Interest Debit":                   (FINANCE_COST, None),

    # --- purpose is clear, no commercial event ---------------------------
    "Utility Payment":                  (UTILITIES, VENDOR_PAYMENT),
    "Utilities & Bills":                (UTILITIES, VENDOR_PAYMENT),
    "Food & Dining":                    (FOOD_DINING, None),
    "Groceries":                        (COST_OF_GOODS, None),
    "Travel":                           (TRAVEL, None),
    "Transportation":                   (TRANSPORTATION, None),
    "Healthcare":                       (HEALTHCARE, None),
    "Education":                        (OTHER_PURPOSE, None),
    "Entertainment":                    (OTHER_PURPOSE, None),
    "Investments":                      (INTERNAL_MOVEMENT, None),
    "Professional Fees":                (PROFESSIONAL_FEES, VENDOR_PAYMENT),
    "Office Expenses":                  (COST_OF_GOODS, VENDOR_PAYMENT),
    "Software & Cloud":                 (PROFESSIONAL_FEES, VENDOR_PAYMENT),
    "Sales Income":                     (SALES_INCOME, CUSTOMER_RECEIPT),
    "Dividend":                         (OTHER_INCOME, None),
    "Refund":                           (OTHER_INCOME, None),
    "Reversal":                         (OTHER_INCOME, None),
    "Others":                           (OTHER_PURPOSE, None),
    "Other":                            (OTHER_PURPOSE, None),
    "ATM Withdrawal":                   (INTERNAL_MOVEMENT, CASH_MOVEMENT),
    "POS/Card Purchase":                (COST_OF_GOODS, VENDOR_PAYMENT),
    "Fuel":                             (TRANSPORTATION, None),

    # --- "Rent Payment" is a trap -----------------------------------------
    # Every row carrying it in the live data is a POS terminal rental
    # (P05RENT_MAR25_T1D_...), i.e. a cost of accepting card settlements, NOT
    # premises rent. Mapping it to Rent & Premises would carry the error
    # forward, so it is left unresolved for narration-based re-derivation.
    "Rent Payment": (None, None),

    # --- rail-named: purpose is NOT derivable from the label --------------
    "NEFT Transfer":  (None, None),
    "IMPS Transfer":  (None, None),
    "UPI Transfer":   (None, None),
    "RTGS Transfer":  (None, None),
}


def map_legacy(category_name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Map a legacy category onto (purpose, event_type).

    Returns (None, None) when the legacy label cannot honestly be resolved —
    the caller must then derive from the narration rather than guess.
    """
    if not category_name:
        return (None, None)
    return LEGACY_MAP.get(category_name.strip(), (None, None))


def is_valid_purpose(name: Optional[str]) -> bool:
    return name in PURPOSE_SET


def is_valid_event_type(name: Optional[str]) -> bool:
    return name is None or name in EVENT_TYPE_SET
