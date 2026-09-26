"""Canonical transaction category taxonomy.

This module is the single source of truth for category names. Every other part
of the system — rule engine, ML label encoder, hybrid decision layer, API
responses, database seeding — must import from here rather than spelling
category names inline, which is how the codebase previously ended up with
"Food & Dining" and "Food" and "Uncategorized" and "Other" all in circulation.
"""

from __future__ import annotations

from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# The 15 canonical categories. Order is stable and used for report columns.
# ---------------------------------------------------------------------------
FOOD_DINING = "Food & Dining"
GROCERIES = "Groceries"
TRANSPORTATION = "Transportation"
SHOPPING = "Shopping"
ENTERTAINMENT = "Entertainment"
UTILITIES_BILLS = "Utilities & Bills"
HEALTHCARE = "Healthcare"
EDUCATION = "Education"
TRAVEL = "Travel"
RENT_HOUSING = "Rent & Housing"
BANK_CHARGES = "Bank Charges"
SALARY_INCOME = "Salary / Income"
TRANSFERS = "Transfers"
INVESTMENTS = "Investments"
OTHER = "Other"

CATEGORIES: List[str] = [
    FOOD_DINING,
    GROCERIES,
    TRANSPORTATION,
    SHOPPING,
    ENTERTAINMENT,
    UTILITIES_BILLS,
    HEALTHCARE,
    EDUCATION,
    TRAVEL,
    RENT_HOUSING,
    BANK_CHARGES,
    SALARY_INCOME,
    TRANSFERS,
    INVESTMENTS,
    OTHER,
]

CATEGORY_SET = set(CATEGORIES)

# Sentinel used when the system declines to assign a category. This is NOT a
# member of the taxonomy: it means "no decision", whereas OTHER means "decided,
# and none of the 14 specific categories apply".
UNCATEGORIZED = "Uncategorized"


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------
# Historical/variant spellings that exist in the database, in older rule files
# and in third-party data. Mapping them here lets us normalise legacy rows
# without adding alternate spellings to the taxonomy itself.
_ALIASES: Dict[str, str] = {
    # Food
    "food": FOOD_DINING,
    "food and dining": FOOD_DINING,
    "food & dining": FOOD_DINING,
    "dining": FOOD_DINING,
    "restaurants": FOOD_DINING,
    # Groceries
    "grocery": GROCERIES,
    "groceries": GROCERIES,
    "supermarket": GROCERIES,
    # Transport
    "transport": TRANSPORTATION,
    "travel & transport": TRANSPORTATION,
    "commute": TRANSPORTATION,
    "fuel": TRANSPORTATION,
    # Legacy label from the old rule file. A cash withdrawal is not transport and
    # not itself a bank charge (only the *fee* on it is), so it maps to Other.
    "atm withdrawal": OTHER,
    # Shopping
    "shopping": SHOPPING,
    "e-commerce": SHOPPING,
    "ecommerce": SHOPPING,
    "retail": SHOPPING,
    # Entertainment
    "entertainment": ENTERTAINMENT,
    "subscriptions": ENTERTAINMENT,
    "media": ENTERTAINMENT,
    # Utilities
    "utilities": UTILITIES_BILLS,
    "utilities and bills": UTILITIES_BILLS,
    "bills": UTILITIES_BILLS,
    "telecom": UTILITIES_BILLS,
    "electricity": UTILITIES_BILLS,
    # Health
    "health": HEALTHCARE,
    "healthcare": HEALTHCARE,
    "medical": HEALTHCARE,
    "pharmacy": HEALTHCARE,
    # Education
    "education": EDUCATION,
    "tuition": EDUCATION,
    # Travel
    "travel": TRAVEL,
    "trips": TRAVEL,
    # Rent
    "rent": RENT_HOUSING,
    "rent and housing": RENT_HOUSING,
    "housing": RENT_HOUSING,
    "maintenance": RENT_HOUSING,
    # Bank charges
    "bank charge": BANK_CHARGES,
    "bank charges": BANK_CHARGES,
    "bank fees": BANK_CHARGES,
    "charges": BANK_CHARGES,
    "fees": BANK_CHARGES,
    # Income
    "salary": SALARY_INCOME,
    "salary payment": SALARY_INCOME,
    "salary / income": SALARY_INCOME,
    "salary/income": SALARY_INCOME,
    "income": SALARY_INCOME,
    "direct income": SALARY_INCOME,
    "payroll": SALARY_INCOME,
    # Transfers
    "transfer": TRANSFERS,
    "transfers": TRANSFERS,
    "fund transfer": TRANSFERS,
    "self transfer": TRANSFERS,
    # Investments
    "investment": INVESTMENTS,
    "investments": INVESTMENTS,
    "mutual fund": INVESTMENTS,
    # Other
    "other": OTHER,
    "others": OTHER,
    "miscellaneous": OTHER,
    "misc": OTHER,
}


def normalize_category(name: Optional[str]) -> Optional[str]:
    """Map any known spelling of a category onto its canonical form.

    Returns None for values that are not recognised, so callers can decide
    whether to treat them as Uncategorized or raise. UNCATEGORIZED passes
    through unchanged because it is a valid state, just not a category.
    """
    if not name:
        return None

    raw = str(name).strip()
    if not raw:
        return None

    if raw in CATEGORY_SET:
        return raw

    if raw.lower() == UNCATEGORIZED.lower():
        return UNCATEGORIZED

    return _ALIASES.get(raw.lower())


def is_valid_category(name: Optional[str]) -> bool:
    """True when `name` is exactly one of the 15 canonical categories."""
    return name in CATEGORY_SET
