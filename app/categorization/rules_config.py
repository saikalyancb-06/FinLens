"""Rule definitions for the deterministic transaction classifier.

This module is pure data. The matching and scoring logic lives in
`rule_engine.py`, so rules can be tuned, added or removed without touching
executable logic.

Priority tiers (highest first)
------------------------------
INTENT_PHRASE  (110) Explicit statements of what the transaction *is*, which
                     override merchant identity. "UPI TRANSFER TO SWIGGY" is a
                     transfer that happens to name a merchant, not a food order.
EXACT_MERCHANT (100) The narration names a specific merchant/entity.
STRONG_PHRASE   (80) Multi-word phrase that is unambiguous in context.
STRONG_KEYWORD  (60) Single word that strongly implies one category.
CONTEXTUAL      (40) Combination of weaker signals that together are meaningful.
WEAK_KEYWORD    (20) Suggestive only; never decides on its own.

A weak keyword can never override a contradictory strong signal — the engine
enforces this by ranking on score, not by first match.
"""

from __future__ import annotations

from app.categorization.taxonomy import (
    BANK_CHARGES,
    EDUCATION,
    ENTERTAINMENT,
    FOOD_DINING,
    GROCERIES,
    HEALTHCARE,
    INVESTMENTS,
    RENT_HOUSING,
    SALARY_INCOME,
    SHOPPING,
    TRANSFERS,
    TRANSPORTATION,
    TRAVEL,
    UTILITIES_BILLS,
)

# ---------------------------------------------------------------------------
# Tier scores. Configurable; the engine reads these rather than hardcoding.
# ---------------------------------------------------------------------------
SCORE_INTENT_PHRASE = 110
SCORE_EXACT_MERCHANT = 100
# Merchant name found concatenated inside a larger token ("UPI-NAMMAYATRI").
# Scored just below a clean word-boundary match: still strong evidence, but a
# substring hit is marginally more prone to coincidence.
SCORE_MERCHANT_IN_TOKEN = 90
SCORE_STRONG_PHRASE = 80
# Strong phrase found concatenated inside a token ("NEFT-TRANSACTIONCHARGE").
# Scored at full strong-phrase weight: the 8-character minimum on embedded
# matching means only long, specific phrases can qualify, so a hit is as
# trustworthy as the spaced form. Scoring it lower left these just under the
# accept threshold, where a confident-but-wrong model reading of the payment
# rail ("UPI") would override a correct rule match.
SCORE_PHRASE_IN_TOKEN = 80
SCORE_STRONG_KEYWORD = 60
SCORE_CONTEXTUAL = 40
SCORE_WEAK_KEYWORD = 20
SCORE_AMOUNT_SIGNAL = 10


# ---------------------------------------------------------------------------
# TIER 0 — Intent phrases
# ---------------------------------------------------------------------------
# These describe the nature of the money movement. They deliberately outrank
# merchant matches: "TRANSFER TO SWIGGY" is a transfer whose counterparty is a
# merchant, and booking it as Food & Dining would misstate the ledger.
#
# Note that a bare payment rail (UPI / NEFT) is NOT an intent phrase. "UPI
# SWIGGY" is a food purchase made over UPI, so the rail alone must not win.
INTENT_PHRASES = {
    TRANSFERS: [
        "TRANSFER TO SELF",
        "SELF TRANSFER",
        "TRANSFER TO OWN ACCOUNT",
        "OWN ACCOUNT TRANSFER",
        "UPI TRANSFER",
        "NEFT TRANSFER",
        "RTGS TRANSFER",
        "IMPS TRANSFER",
        "FUND TRANSFER",
        "FUNDS TRANSFER",
        "TRANSFER TO",
        "ACCOUNT TRANSFER",
    ],
}


# ---------------------------------------------------------------------------
# TIER 1 — Exact merchants
# ---------------------------------------------------------------------------
EXACT_MERCHANTS = {
    FOOD_DINING: [
        "SWIGGY", "ZOMATO", "MCDONALDS", "MC DONALDS", "KFC", "DOMINOS",
        "PIZZA HUT", "STARBUCKS", "SUBWAY", "BURGER KING", "DUNKIN",
        "BARBEQUE NATION", "HALDIRAM", "CAFE COFFEE DAY", "CCD", "CHAIPOINT",
        "FAASOS", "BEHROUZ", "OVENSTORY", "EATFIT", "BOX8",
    ],
    GROCERIES: [
        "BIGBASKET", "BIG BASKET", "BLINKIT", "ZEPTO", "DMART", "D MART",
        "RELIANCE FRESH", "RELIANCE SMART", "MORE SUPERMARKET", "JIOMART",
        "NATURES BASKET", "SPENCERS", "STAR BAZAAR", "GROFERS", "INSTAMART",
    ],
    TRANSPORTATION: [
        "UBER", "OLA", "RAPIDO", "NAMMA YATRI", "BMRC", "BANGALORE METRO",
        "IRCTC", "REDBUS", "BLUSMART", "BLU SMART", "DELHI METRO", "DMRC",
        "INDIAN OIL", "IOCL", "BHARAT PETROLEUM", "BPCL", "HINDUSTAN PETROLEUM",
        "HPCL", "SHELL", "FASTAG", "PAYTM FASTAG",
    ],
    SHOPPING: [
        "AMAZON", "FLIPKART", "MYNTRA", "AJIO", "MEESHO", "CROMA",
        "RELIANCE DIGITAL", "DECATHLON", "NYKAA", "TATA CLIQ", "SNAPDEAL",
        "LIFESTYLE", "PANTALOONS", "WESTSIDE", "SHOPPERS STOP", "IKEA",
        "H AND M", "ZARA", "UNIQLO",
    ],
    ENTERTAINMENT: [
        "NETFLIX", "SPOTIFY", "PRIME VIDEO", "AMAZON PRIME", "HOTSTAR",
        "DISNEY HOTSTAR", "YOUTUBE PREMIUM", "BOOKMYSHOW", "PVR", "INOX",
        "SONY LIV", "SONYLIV", "ZEE5", "JIOCINEMA", "JIO CINEMA", "GAANA",
        "WYNK", "APPLE MUSIC", "STEAM GAMES",
    ],
    UTILITIES_BILLS: [
        "JIO", "RELIANCE JIO", "AIRTEL", "BHARTI AIRTEL", "VODAFONE IDEA",
        "VODAFONE", "BSNL", "BESCOM", "BWSSB", "TATA POWER",
        "ADANI ELECTRICITY", "ACT BROADBAND", "ACT FIBERNET", "JIOFIBER",
        "JIO FIBER", "AIRTEL XSTREAM", "INDANE GAS", "HP GAS", "BHARAT GAS",
        "MSEB", "TNEB", "KSEB", "TORRENT POWER", "MAHANAGAR GAS",
    ],
    HEALTHCARE: [
        "APOLLO", "MANIPAL", "FORTIS", "TATA 1MG", "1MG", "PHARMEASY",
        "NETMEDS", "MEDPLUS", "PRACTO", "THYROCARE", "DR LAL PATHLABS",
        "MAX HEALTHCARE", "NARAYANA HEALTH", "CLOUDNINE",
    ],
    EDUCATION: [
        "COURSERA", "UDEMY", "UNACADEMY", "UPGRAD", "BYJUS", "VEDANTU",
        "SIMPLILEARN", "GREAT LEARNING", "SCALER", "EDX", "KHAN ACADEMY",
    ],
    TRAVEL: [
        "AIR INDIA", "INDIGO", "EMIRATES", "BOOKING.COM", "BOOKINGCOM",
        "AIRBNB", "OYO", "MAKEMYTRIP", "GOIBIBO", "CLEARTRIP", "YATRA",
        "VISTARA", "SPICEJET", "AKASA AIR", "TRIVAGO", "AGODA", "TAJ HOTELS",
        "MARRIOTT", "RADISSON",
    ],
    INVESTMENTS: [
        "ZERODHA", "GROWW", "UPSTOX", "ANGEL ONE", "ANGELONE", "KUVERA",
        "COIN ZERODHA", "SMALLCASE", "ICICI DIRECT", "HDFC SECURITIES",
        "KOTAK SECURITIES", "PAYTM MONEY", "NSE", "BSE",
    ],
}


# ---------------------------------------------------------------------------
# TIER 2 — Strong multi-word phrases
# ---------------------------------------------------------------------------
STRONG_PHRASES = {
    BANK_CHARGES: [
        # Bank Charges is intentionally phrase-only. A bare "CHARGE" token is
        # never sufficient, otherwise "SWIGGY CHARGE" books as a bank fee.
        "BANK CHARGES", "BANK CHARGE", "SERVICE CHARGE", "SERVICE CHARGES",
        "ANNUAL MAINTENANCE FEE", "AMC FEE", "ATM FEE", "ATM CHARGES",
        "CASH WITHDRAWAL FEE", "SMS CHARGES", "SMS CHARGE",
        "TRANSACTION FEE", "TRANSACTION CHARGE", "TRANSACTION CHARGES",
        "GST ON CHARGES",
        "PROCESSING FEE", "LATE PAYMENT FEE", "PENALTY CHARGES",
        "MIN BALANCE CHARGES", "MINIMUM BALANCE CHARGES", "CHEQUE RETURN CHARGES",
        "NON MAINTENANCE CHARGES", "DEBIT CARD FEE", "CARD ANNUAL FEE",
        "OVERDRAFT FEE", "IMPS CHARGES", "NEFT CHARGES", "RTGS CHARGES",
    ],
    SALARY_INCOME: [
        "SALARY CREDIT", "MONTHLY SALARY", "PAYROLL CREDIT", "PAYROLL",
        "WAGES CREDIT", "PERFORMANCE BONUS", "INTEREST CREDIT",
        "SALARY FOR", "SAL CREDIT", "STIPEND CREDIT", "PENSION CREDIT",
        "INT CR", "INTEREST EARNED", "DIVIDEND CREDIT", "ANNUAL BONUS",
    ],
    RENT_HOUSING: [
        "HOUSE RENT", "MONTHLY RENT", "RENT PAYMENT", "LANDLORD PAYMENT",
        "RENT TO OWNER", "APARTMENT MAINTENANCE", "FLAT MAINTENANCE",
        "SOCIETY MAINTENANCE", "HOME MAINTENANCE", "RENT FOR",
        "HOUSING SOCIETY", "MAINTENANCE CHARGES SOCIETY",
    ],
    INVESTMENTS: [
        "MUTUAL FUND", "MF INVESTMENT", "SIP INVESTMENT", "SIP DEBIT",
        "SYSTEMATIC INVESTMENT", "EQUITY PURCHASE", "STOCK PURCHASE",
        "DEMAT DEBIT", "NPS CONTRIBUTION", "PPF DEPOSIT", "RD INSTALLMENT",
        "FD BOOKING", "FIXED DEPOSIT",
    ],
    UTILITIES_BILLS: [
        "MOBILE RECHARGE", "ELECTRICITY BILL", "WATER BILL", "GAS BILL",
        "BROADBAND BILL", "POSTPAID BILL", "PREPAID RECHARGE",
        "DTH RECHARGE", "INTERNET BILL", "UTILITY PAYMENT",
    ],
    TRANSPORTATION: [
        "FASTAG RECHARGE", "TOLL PLAZA", "FUEL PURCHASE", "PETROL PUMP",
        "METRO RECHARGE", "CAB RIDE", "AUTO RIDE",
    ],
    HEALTHCARE: [
        "MEDICAL STORE", "HEALTH INSURANCE", "DIAGNOSTIC CENTRE",
        "DIAGNOSTIC CENTER", "PATH LAB", "MEDICAL BILL",
    ],
    EDUCATION: [
        "TUITION FEE", "TUITION FEES", "COLLEGE FEE", "SCHOOL FEE",
        "EXAM FEE", "COURSE FEE", "SEMESTER FEE", "ADMISSION FEE",
    ],
    TRAVEL: [
        "FLIGHT BOOKING", "HOTEL BOOKING", "TRAVEL BOOKING", "AIR TICKET",
        "TICKET BOOKING",
    ],
    FOOD_DINING: [
        "FOOD ORDER", "FOOD DELIVERY", "ONLINE FOOD",
    ],
    GROCERIES: [
        "GROCERY STORE", "GROCERY SHOPPING", "SUPER MARKET",
    ],
}


# ---------------------------------------------------------------------------
# TIER 3 — Strong single keywords
# ---------------------------------------------------------------------------
STRONG_KEYWORDS = {
    FOOD_DINING: [
        "RESTAURANT", "CAFE", "COFFEE", "DINING", "BAKERY", "BIRYANI",
        "PIZZA", "BURGER", "CANTEEN", "DHABA", "EATERY", "BISTRO",
    ],
    GROCERIES: [
        "GROCERY", "GROCERIES", "SUPERMARKET", "VEGETABLES", "PROVISION",
        "KIRANA",
    ],
    TRANSPORTATION: [
        "TAXI", "METRO", "TOLL", "FASTAG", "PETROL", "DIESEL", "PARKING",
        "CAB",
    ],
    SHOPPING: [
        "ELECTRONICS", "CLOTHING", "APPAREL", "FASHION", "FOOTWEAR",
        "FURNITURE",
    ],
    ENTERTAINMENT: [
        "MOVIE", "CINEMA", "MULTIPLEX", "GAMING", "ENTERTAINMENT",
    ],
    UTILITIES_BILLS: [
        "ELECTRICITY", "BROADBAND", "POSTPAID", "PREPAID", "RECHARGE",
        "DTH", "LANDLINE",
    ],
    HEALTHCARE: [
        "HOSPITAL", "DOCTOR", "CLINIC", "PHARMACY", "MEDICINE", "MEDICAL",
        "DIAGNOSTIC", "PATHOLOGY", "DENTAL",
    ],
    EDUCATION: [
        "COLLEGE", "UNIVERSITY", "TUITION", "SEMESTER", "COACHING",
    ],
    TRAVEL: [
        "FLIGHT", "AIRLINE", "RESORT", "AIRPORT", "AIRWAYS",
    ],
    RENT_HOUSING: [
        "LANDLORD", "TENANT",
    ],
    INVESTMENTS: [
        "ZERODHA", "BROKERAGE", "EQUITY", "DEMAT", "MUTUALFUND",
    ],
}


# ---------------------------------------------------------------------------
# TIER 4 — Contextual combinations
# ---------------------------------------------------------------------------
# Each entry: (category, [all_of_these_tokens]). Meaningful only together.
CONTEXTUAL_COMBINATIONS = [
    (RENT_HOUSING, ["RENT"]),
    (SALARY_INCOME, ["SALARY"]),
    (SALARY_INCOME, ["BONUS", "CREDIT"]),
    (SALARY_INCOME, ["INTEREST", "CREDIT"]),
    (INVESTMENTS, ["SIP"]),
    (INVESTMENTS, ["STOCK"]),
    (UTILITIES_BILLS, ["BILL", "PAYMENT"]),
    (UTILITIES_BILLS, ["GAS"]),
    (UTILITIES_BILLS, ["WATER"]),
    (TRANSPORTATION, ["FUEL"]),
    (TRANSPORTATION, ["BUS"]),
    (TRANSPORTATION, ["TRAIN"]),
    (EDUCATION, ["COURSE"]),
    (EDUCATION, ["EXAM"]),
    (EDUCATION, ["SCHOOL"]),
    (TRAVEL, ["HOTEL"]),
    (TRAVEL, ["TRAVEL"]),
    (TRAVEL, ["BOOKING"]),
    (SHOPPING, ["SHOPPING"]),
    (SHOPPING, ["SHOES"]),
    (ENTERTAINMENT, ["SUBSCRIPTION"]),
    (ENTERTAINMENT, ["MUSIC"]),
    (HEALTHCARE, ["HEALTH"]),
    (HEALTHCARE, ["LAB"]),
    (FOOD_DINING, ["FOOD"]),
    (FOOD_DINING, ["MESS"]),
    (GROCERIES, ["FRUITS"]),
    (GROCERIES, ["MILK"]),
]


# ---------------------------------------------------------------------------
# TIER 5 — Weak keywords
# ---------------------------------------------------------------------------
# Suggestive only. Cannot reach the auto-accept threshold alone, by design.
# "Other" deliberately has no rules at any tier: it must never be *asserted* by
# the rule engine, only reached when nothing else applies.
WEAK_KEYWORDS = {
    TRANSFERS: ["NEFT", "RTGS", "IMPS", "UPI", "ACH", "ECS"],
    SHOPPING: ["STORE", "MART", "RETAIL", "MALL"],
    FOOD_DINING: ["KITCHEN", "JUICE", "SNACK"],
}


# ---------------------------------------------------------------------------
# Amount / direction supporting signals
# ---------------------------------------------------------------------------
# Small nudges applied only when a category is already in play. A CREDIT of a
# large amount supports Salary/Income; it never creates the candidate itself.
AMOUNT_SIGNALS = {
    SALARY_INCOME: {"direction": "CREDIT", "min_amount": 5000},
}
