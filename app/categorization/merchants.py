"""Evidence patterns: what a narration has to contain before a path is earned.

Two kinds of evidence live here, and the difference between them is the whole
argument of this module.

CONCEPT PATTERNS say what the money was for, directly. `TOLL PLAZA`, `LIC
PREMIUM`, `GST PAYMENT`, `ATM WDL` — the narration names the purpose, so the
category follows from the words themselves and holds for any bank, any country
that writes in these terms, and any kind of account.

MERCHANT PATTERNS say who was paid. That is a weaker claim, and it is the one
that invites the mistake this module is built to avoid: a merchant is not a
category. Amazon sells laptops and lentils and washing machines. `AMAZON` in a
narration is excellent evidence of *who*, and no evidence at all of *what*, so
it resolves to `Shopping > Online Shopping > Amazon` and stops. It becomes
`Shopping > Electronics > Mobile` only when the narration itself also says
mobile — see PRODUCT_HINTS.

Where a merchant does imply a purpose, it is because the merchant only does one
thing. Zomato is food delivery; that is the entire company. Indian Oil sells
fuel. Those are safe. `BharatPe` is a payment aggregator that settles for
restaurants and pharmacies and hardware shops alike, so it implies a rail and a
settlement, not a category — and it is absent from the merchant table for that
reason.

None of this is a complete list of the world's merchants and it is not trying to
be. It is a floor: the transactions it recognises are classified deterministically
and cheaply, and everything else falls through to the upstream rule engine, the
model, and finally to an honest `Vendor / Business Transaction` with a low
confidence. Adding a merchant here is a one-line change and requires no retraining.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from app.categorization import hierarchy as H


@dataclass(frozen=True)
class Evidence:
    """One recognisable thing, and what it licenses us to say."""
    pattern: re.Pattern
    path: Tuple[str, ...]
    confidence: float
    merchant: Optional[str] = None
    # Some evidence only means what it means in one direction. `SALARY` on a
    # credit is income; on a debit it is payroll going out of a business account.
    direction: Optional[str] = None   # 'credit' | 'debit' | None
    note: str = ""


def _p(regex: str) -> re.Pattern:
    return re.compile(regex, re.I)


# ---------------------------------------------------------------------------
# CONCEPT PATTERNS — the narration states the purpose.
#
# Ordered: the first match wins, so anything that is a special case of something
# else has to come first. `CREDIT CARD PAYMENT` before `PAYMENT`; `FUEL SURCHARGE
# REVERSAL` before `FUEL`.
# ---------------------------------------------------------------------------
CONCEPT_EVIDENCE: Tuple[Evidence, ...] = (
    # -- Reversals and refunds, first, because they invert everything after ---
    Evidence(_p(r"\bCHARGEBACK\b"), (H.REFUNDS_REVERSALS, "Chargeback"), 0.93),
    Evidence(_p(r"\b(FAILED\s*(?:TXN|TRANSACTION|PAYMENT)|TXN\s*FAILED|AUTO\s*REVERSAL|RVSL|REVERSAL)\b"),
             (H.REFUNDS_REVERSALS, "Failed Transaction Reversal"), 0.88),
    Evidence(_p(r"\bREFUND\b"), (H.REFUNDS_REVERSALS, "Purchase Refund"), 0.80),

    # -- Merchant acquirers, on the CREDIT side only -------------------------
    #
    # BharatPe, Pine Labs, Razorpay, PhonePe and the rest are payment rails, and
    # `PASS_THROUGH_ENTITIES` below deliberately refuses to treat them as the
    # party — a settlement is not a purchase from BharatPe. But money coming IN
    # from an acquirer is not nothing: it is the day's card and UPI takings
    # being paid out. That is a fact about the direction, not a guess about the
    # business, and it is the difference between `Income > Business Revenue`
    # and a review question.
    #
    # `direction="credit"` is load-bearing. A DEBIT naming the same rail is a
    # payment made THROUGH it and says nothing about what was bought, so there
    # is deliberately no matching outbound rule — the row keeps going.
    #
    # On the statement that prompted this, BharatPe alone was 56 rows across
    # two spellings, and both were review questions.
    Evidence(_p(r"\b(BHARATPE|BHARAT\s*PE|PINELABS|PINE\s*LABS|MSWIPE|EZETAP|"
                r"WORLDLINE|INNOVITI|ATOM\s*TECH|PAYSWIFF)\b"),
             (H.INCOME, "Business Revenue"), 0.86, direction="credit",
             note="merchant acquirer settlement"),
    Evidence(_p(r"\b(RAZORPAY|CASHFREE|BILLDESK|CCAVENUE|PAYU|INSTAMOJO|JUSPAY|"
                r"PHONEPE|PAYTM|MOBIKWIK|FREECHARGE|STRIPE)\b"),
             (H.INCOME, "Business Revenue"), 0.82, direction="credit",
             note="payment gateway or wallet settlement"),

    # -- Cash ---------------------------------------------------------------
    Evidence(_p(r"\b(ATM\s*(?:WDL|WITHDRAWAL|CASH)?|NWD|CASH\s*WDL|CASH\s*WITHDRAWAL|AWB)\b"),
             (H.CASH, "ATM Withdrawal"), 0.93, direction="debit"),
    Evidence(_p(r"\b(CASH\s*DEP(?:OSIT)?|CDM|CASH\s*RECEIPT|BY\s*CASH)\b"),
             (H.CASH, "Cash Deposit"), 0.90, direction="credit"),

    # -- Bank charges and fees ----------------------------------------------
    Evidence(_p(r"\bATM\s*(?:CHG|CHARGE|FEE)"), (H.FINANCIAL, "ATM Charges"), 0.92),
    Evidence(_p(r"\b(PG\s*CHG|PAYMENT\s*GATEWAY\s*(?:CHG|CHARGE|FEE)|MDR)\b"),
             (H.FINANCIAL, "Payment Gateway Charges"), 0.90),
    Evidence(_p(r"\b(PROC(?:ESSING)?\s*(?:FEE|CHG|CHARGE))\b"), (H.FINANCIAL, "Processing Fees"), 0.88),
    Evidence(_p(r"\b(SMS\s*CHG|SMSCHG|AMB\s*CHG|MIN\s*BAL|NON\s*MAINT|ACH\s*RTN\s*CHG|"
                r"CHQ\s*RTN\s*CHG|RTN\s*CHG|BANK\s*CHARGE|SERVICE\s*CHARGE|SRV\s*CHG|"
                r"FOLIO\s*CHG|ANNUAL\s*MAINT|AMC\b|LOCKER\s*(?:RENT|CHG))\b"),
             (H.FINANCIAL, "Bank Charges"), 0.90),
    Evidence(_p(r"\b(LATE\s*(?:PAYMENT\s*)?FEE|LPF)\b"), (H.FEES_CHARGES, "Late Payment Fee"), 0.88),
    Evidence(_p(r"\b(PENALTY|PENAL\s*(?:CHG|CHARGE|INT))\b"), (H.FEES_CHARGES, "Penalty"), 0.85),
    Evidence(_p(r"\bCONVENIENCE\s*FEE\b"), (H.FEES_CHARGES, "Convenience Fee"), 0.88),

    # -- Interest -----------------------------------------------------------
    Evidence(_p(r"\b(INT(?:EREST)?\s*(?:CR|CREDIT|PAID|EARNED)|SB\s*INT|SAVINGS\s*INT|FD\s*INT)\b"),
             (H.INCOME, "Interest Income"), 0.90, direction="credit"),
    Evidence(_p(r"\b(INT(?:EREST)?\s*(?:DR|DEBIT|CHG|CHARGED)|LOAN\s*INT|OD\s*INT)\b"),
             (H.FINANCIAL, "Interest"), 0.88, direction="debit"),

    # -- Taxes and government ------------------------------------------------
    Evidence(_p(r"\b(GST|CGST|SGST|IGST|GSTN)\b"), (H.TAXES_GOVERNMENT, "GST"), 0.92),
    Evidence(_p(r"\bTDS\b"), (H.TAXES_GOVERNMENT, "TDS"), 0.92),
    Evidence(_p(r"\b(INCOME\s*TAX|ITNS|CBDT|ADVANCE\s*TAX|SELF\s*ASST\s*TAX)\b"),
             (H.TAXES_GOVERNMENT, "Income Tax"), 0.92),
    Evidence(_p(r"\bPROPERTY\s*TAX\b"), (H.TAXES_GOVERNMENT, "Property Tax"), 0.90),
    Evidence(_p(r"\b(CHALLAN|TRAFFIC\s*FINE|E-?CHALLAN)\b"), (H.TAXES_GOVERNMENT, "Fines / Penalties"), 0.85),
    Evidence(_p(r"\b(PF|EPF|EPFO|ESIC?|PROF(?:ESSIONAL)?\s*TAX|PTAX)\b"),
             (H.TAXES_GOVERNMENT, "Other Government Payment"), 0.80),

    # -- Loans and credit ----------------------------------------------------
    Evidence(_p(r"\bCREDIT\s*CARD\s*(?:PAYMENT|PMT|BILL|AUTOPAY)\b"),
             (H.LOANS_CREDIT, "Credit Card", "Payment"), 0.92),
    Evidence(_p(r"\b(CC\s*PAYMENT|CARD\s*PAYMENT\s*RECEIVED)\b"),
             (H.LOANS_CREDIT, "Credit Card", "Payment"), 0.82),
    Evidence(_p(r"\bLOAN\s*DISB(?:URSEMENT|URSAL)?\b"), (H.LOANS_CREDIT, "Loan Disbursement"), 0.93),
    Evidence(_p(r"\b(EMI|E\.M\.I|EQUATED\s*MONTHLY)\b"), (H.LOANS_CREDIT, "EMI"), 0.90),
    Evidence(_p(r"\b(LOAN\s*(?:REPAY(?:MENT)?|INSTAL?MENT|A/?C)|HOUSING\s*LOAN|"
                r"HOME\s*LOAN|CAR\s*LOAN|PERSONAL\s*LOAN|BUSINESS\s*LOAN|GOLD\s*LOAN)\b"),
             (H.LOANS_CREDIT, "Loan Repayment"), 0.88),

    # -- Insurance -----------------------------------------------------------
    Evidence(_p(r"\b(HEALTH\s*INS(?:URANCE)?|MEDICLAIM)\b"), (H.INSURANCE, "Health Insurance"), 0.90),
    Evidence(_p(r"\b(MOTOR\s*INS(?:URANCE)?|VEHICLE\s*INS(?:URANCE)?)\b"), (H.INSURANCE, "Motor Insurance"), 0.90),
    Evidence(_p(r"\b(LIFE\s*INS(?:URANCE)?|TERM\s*PLAN|ULIP)\b"), (H.INSURANCE, "Life Insurance"), 0.88),
    Evidence(_p(r"\b(INSURANCE|PREMIUM\s*PAY(?:MENT)?|POLICY\s*PREMIUM)\b"), (H.INSURANCE, "Other Insurance"), 0.75),

    # -- Investments ---------------------------------------------------------
    Evidence(_p(r"\bSIP\b"), (H.INVESTMENTS, "Mutual Funds", "SIP"), 0.90),
    Evidence(_p(r"\b(MUTUAL\s*FUND|MF\s*PURCHASE|FOLIO)\b"), (H.INVESTMENTS, "Mutual Funds"), 0.82),
    Evidence(_p(r"\b(FIXED\s*DEPOSIT|FD\s*(?:BOOKING|RENEWAL|CLOSURE)|TERM\s*DEPOSIT)\b"),
             (H.INVESTMENTS, "Fixed Deposit"), 0.88),
    Evidence(_p(r"\b(RECURRING\s*DEPOSIT|RD\s*INSTAL?MENT)\b"), (H.INVESTMENTS, "Recurring Deposit"), 0.88),
    Evidence(_p(r"\bPPF\b"), (H.INVESTMENTS, "PPF"), 0.92),
    Evidence(_p(r"\b(NPS|NATIONAL\s*PENSION)\b"), (H.INVESTMENTS, "NPS"), 0.90),
    Evidence(_p(r"\b(DEMAT|BROKERAGE|EQUITY\s*(?:PURCHASE|BUY))\b"), (H.INVESTMENTS, "Stocks"), 0.78),

    # -- Income --------------------------------------------------------------
    Evidence(_p(r"\b(SALARY|SAL\s*CREDIT|SALCR|PAYROLL|WAGES|STIPEND)\b"),
             (H.INCOME, "Salary"), 0.92, direction="credit"),
    Evidence(_p(r"\b(SALARY|PAYROLL|WAGES)\b"), (H.BUSINESS, "Salaries & Wages"), 0.88, direction="debit"),
    Evidence(_p(r"\b(PENSION)\b"), (H.INCOME, "Pension"), 0.90, direction="credit"),
    Evidence(_p(r"\b(DIVIDEND|DIV\s*CR)\b"), (H.INCOME, "Dividend"), 0.90, direction="credit"),
    Evidence(_p(r"\b(RENT\s*(?:RECEIVED|CR)|RENTAL\s*INCOME)\b"), (H.INCOME, "Rental Income"), 0.85, direction="credit"),
    Evidence(_p(r"\b(SUBSIDY|SCHOLARSHIP|DBT|PMKISAN|PM\s*KISAN|LPG\s*SUBSIDY)\b"),
             (H.INCOME, "Government Benefit"), 0.85, direction="credit"),

    # -- Housing -------------------------------------------------------------
    # Guarded: "RENT" alone is a trap. POS terminal rental (POSRENT, P05RENT)
    # is a cost of accepting cards, not premises rent, and this system has
    # already mis-filed a whole statement on exactly that.
    # No trailing \b: the real narrations are `P05RENT_MAR25_T1D_...`, and an
    # underscore is a word character, so a boundary after RENT never matches.
    Evidence(_p(r"\b(?:POS|P05)\s*RENT"), (H.FINANCIAL, "Payment Gateway Charges"), 0.85),
    Evidence(_p(r"\b(HOUSE\s*RENT|RENT\s*PAID|MONTHLY\s*RENT|LANDLORD)\b"), (H.HOUSING, "Rent"), 0.85, direction="debit"),
    Evidence(_p(r"\b(SOCIETY\s*(?:MAINT|CHARGES|FEE)|MAINTENANCE\s*CHARGES|RWA)\b"),
             (H.HOUSING, "Society & Association Fees"), 0.82),

    # -- Bills and utilities -------------------------------------------------
    Evidence(_p(r"\b(ELECTRICITY|ELEC\s*BILL|POWER\s*BILL|BESCOM|MSEB|TNEB|BSES|TATA\s*POWER|"
                r"ADANI\s*ELECTRICITY|TORRENT\s*POWER|KSEB|APSPDCL|TSSPDCL|PSPCL|UPPCL|DHBVN|MSEDCL)\b"),
             (H.BILLS_UTILITIES, "Electricity"), 0.90),
    Evidence(_p(r"\b(WATER\s*BILL|JAL\s*BOARD|WATER\s*SUPPLY)\b"), (H.BILLS_UTILITIES, "Water"), 0.88),
    Evidence(_p(r"\b(GAS\s*BILL|LPG|INDANE|HP\s*GAS|BHARATGAS|GAIL|MAHANAGAR\s*GAS|IGL)\b"),
             (H.BILLS_UTILITIES, "Gas"), 0.85),
    Evidence(_p(r"\b(BROADBAND|INTERNET\s*BILL|FIBERNET|FIBRENET|WIFI\s*BILL|LEASED\s*LINE)\b"),
             (H.BILLS_UTILITIES, "Internet"), 0.88),
    Evidence(_p(r"\b(MOBILE\s*(?:BILL|RECHARGE)|PREPAID\s*RECHARGE|POSTPAID|TOPUP|TOP-UP)\b"),
             (H.BILLS_UTILITIES, "Mobile"), 0.85),
    Evidence(_p(r"\b(DTH|TATA\s*SKY|TATAPLAY|DISH\s*TV|SUN\s*DIRECT|D2H)\b"),
             (H.BILLS_UTILITIES, "DTH"), 0.88),

    # -- Transportation ------------------------------------------------------
    Evidence(_p(r"\b(EV\s*CHARG|CHARGING\s*STATION)\b"), (H.TRANSPORTATION, "Fuel", "EV Charging"), 0.85),
    Evidence(_p(r"\b(PETROL|PETROL\s*PUMP)\b"), (H.TRANSPORTATION, "Fuel", "Petrol"), 0.88),
    Evidence(_p(r"\bDIESEL\b"), (H.TRANSPORTATION, "Fuel", "Diesel"), 0.88),
    Evidence(_p(r"\bCNG\b"), (H.TRANSPORTATION, "Fuel", "CNG"), 0.88),
    Evidence(_p(r"\b(FUEL|FILLING\s*STATION)\b"), (H.TRANSPORTATION, "Fuel"), 0.82),
    Evidence(_p(r"\b(FASTAG|TOLL|NHAI|TOLL\s*PLAZA)\b"), (H.TRANSPORTATION, "Vehicle Expenses", "Toll"), 0.90),
    Evidence(_p(r"\bPARKING\b"), (H.TRANSPORTATION, "Vehicle Expenses", "Parking"), 0.85),
    Evidence(_p(r"\b(SERVICING|VEHICLE\s*SERVICE|CAR\s*SERVICE)\b"),
             (H.TRANSPORTATION, "Vehicle Expenses", "Servicing"), 0.80),
    Evidence(_p(r"\b(METRO|DMRC|BMRCL|MMRDA)\b"), (H.TRANSPORTATION, "Public Transport", "Metro"), 0.85),
    Evidence(_p(r"\b(IRCTC|RAILWAY|INDIAN\s*RAIL)\b"), (H.TRAVEL, "Trains"), 0.88),

    # -- Healthcare ----------------------------------------------------------
    Evidence(_p(r"\b(PHARMACY|MEDICAL\s*STORE|CHEMIST|MEDICOS|DRUG\s*HOUSE)\b"), (H.HEALTHCARE, "Pharmacy"), 0.85),
    Evidence(_p(r"\b(HOSPITAL|NURSING\s*HOME|CLINIC)\b"), (H.HEALTHCARE, "Hospital"), 0.82),
    Evidence(_p(r"\b(DIAGNOSTIC|PATHOLOG|LAB\s*TEST|SCAN\s*CENTRE|RADIOLOG)\b"), (H.HEALTHCARE, "Diagnostics"), 0.85),
    Evidence(_p(r"\b(DENTAL|DENTIST)\b"), (H.HEALTHCARE, "Dental"), 0.88),
    Evidence(_p(r"\b(OPTICAL|OPTICIAN|EYE\s*CARE)\b"), (H.HEALTHCARE, "Optical"), 0.85),

    # -- Education -----------------------------------------------------------
    Evidence(_p(r"\b(TUITION|SCHOOL\s*FEE|COLLEGE\s*FEE|ADMISSION\s*FEE|SEMESTER\s*FEE|HOSTEL\s*FEE)\b"),
             (H.EDUCATION, "Tuition & Fees"), 0.88),
    Evidence(_p(r"\b(EXAM\s*FEE|EXAMINATION\s*FEE)\b"), (H.EDUCATION, "Exam Fees"), 0.88),
    Evidence(_p(r"\b(COACHING|TRAINING\s*FEE|COURSE\s*FEE|CERTIFICATION)\b"),
             (H.EDUCATION, "Courses & Training"), 0.80),

    # -- Donations -----------------------------------------------------------
    Evidence(_p(r"\b(DONATION|CHARITABLE|TRUST\s*DONATION|NGO)\b"), (H.DONATIONS, "Charity"), 0.85),
    Evidence(_p(r"\b(TEMPLE|CHURCH|MOSQUE|GURUDWARA|DEVASTHANAM|SEVA)\b"), (H.DONATIONS, "Religious"), 0.80),

    # -- Food, as a concept rather than a merchant ---------------------------
    Evidence(_p(r"\b(RESTAURANT|DHABA|BHOJANALAYA|EATERY|DINER)\b"), (H.FOOD_DINING, "Restaurants"), 0.85),
    Evidence(_p(r"\b(BAKERY|CONFECTION)\b"), (H.FOOD_DINING, "Cafes & Beverages", "Bakery"), 0.85),
    Evidence(_p(r"\b(CAFE|COFFEE\s*(?:HOUSE|SHOP|DAY))\b"), (H.FOOD_DINING, "Cafes & Beverages", "Cafe"), 0.82),
    Evidence(_p(r"\b(SUPERMARKET|HYPERMARKET|KIRANA|GENERAL\s*STORE|PROVISION\s*STORE|GROCER(?:Y|IES))\b"),
             (H.FOOD_DINING, "Groceries"), 0.82),

    # -- Transfers -----------------------------------------------------------
    Evidence(_p(r"\b(SELF|OWN\s*A/?C|TO\s*SELF|FROM\s*SELF|INTERNAL\s*TRANSFER|SWEEP\s*(?:IN|OUT))\b"),
             (H.TRANSFERS, "Own Account Transfer"), 0.88),
)


# ---------------------------------------------------------------------------
# MERCHANT PATTERNS — the narration names who was paid.
#
# Only merchants whose business IS the category. A marketplace, an aggregator or
# a bank belongs in the marketplace table below instead, or nowhere at all.
# ---------------------------------------------------------------------------
MERCHANT_EVIDENCE: Tuple[Evidence, ...] = (
    # -- Food delivery -------------------------------------------------------
    Evidence(_p(r"\bZOMATO\b"),   (H.FOOD_DINING, "Food Delivery", "Zomato"),   0.95, merchant="Zomato"),
    Evidence(_p(r"\bSWIGGY\b"),   (H.FOOD_DINING, "Food Delivery", "Swiggy"),   0.95, merchant="Swiggy"),
    Evidence(_p(r"\b(UBER\s*EATS|UBEREATS)\b"), (H.FOOD_DINING, "Food Delivery", "Uber Eats"), 0.95, merchant="Uber Eats"),
    Evidence(_p(r"\b(DOORDASH|DELIVEROO|JUST\s*EAT|GRUBHUB|FOODPANDA)\b"),
             (H.FOOD_DINING, "Food Delivery", "Online Delivery"), 0.90),
    Evidence(_p(r"\b(EATFIT|FAASOS|BEHROUZ|OVENSTORY|BOX8|FRESHMENU)\b"),
             (H.FOOD_DINING, "Food Delivery", "Cloud Kitchen"), 0.88),

    # -- Restaurants and cafes ----------------------------------------------
    Evidence(_p(r"\b(MCDONALD|MC\s*DONALD|BURGER\s*KING|KFC|SUBWAY|DOMINO|PIZZA\s*HUT|"
                r"WENDY|TACO\s*BELL|POPEYES|JOLLIBEE)\b"),
             (H.FOOD_DINING, "Restaurants", "Fast Food"), 0.92),
    Evidence(_p(r"\b(STARBUCKS|CAFE\s*COFFEE\s*DAY|CCD|COSTA\s*COFFEE|BARISTA|"
                r"BLUE\s*TOKAI|THIRD\s*WAVE|CHAAYOS|CHAI\s*POINT|DUNKIN)\b"),
             (H.FOOD_DINING, "Cafes & Beverages", "Coffee"), 0.92),
    Evidence(_p(r"\b(BASKIN|NATURALS\s*ICE|IBACO|HAAGEN)\b"),
             (H.FOOD_DINING, "Cafes & Beverages", "Desserts"), 0.90),

    # -- Groceries -----------------------------------------------------------
    Evidence(_p(r"\b(BIGBASKET|BIG\s*BASKET|BLINKIT|GROFERS|ZEPTO|INSTAMART|"
                r"JIOMART|DUNZO\s*DAILY|SWIGGY\s*INSTAMART)\b"),
             (H.FOOD_DINING, "Groceries", "Online Grocery"), 0.92),
    Evidence(_p(r"\b(DMART|D-MART|RELIANCE\s*FRESH|RELIANCE\s*SMART|MORE\s*RETAIL|"
                r"SPENCER|STAR\s*BAZAAR|NATURE'?S\s*BASKET|WALMART|TESCO|CARREFOUR|LULU)\b"),
             (H.FOOD_DINING, "Groceries", "Supermarket"), 0.92),

    # -- Fuel ----------------------------------------------------------------
    Evidence(_p(r"\b(INDIAN\s*OIL|INDIANOIL|IOCL|BHARAT\s*PETROLEUM|BPCL|HINDUSTAN\s*PETRO|"
                r"HPCL|RELIANCE\s*PETRO|NAYARA|SHELL\s*(?:INDIA|PETROL)?)\b"),
             (H.TRANSPORTATION, "Fuel"), 0.88),

    # -- Ride hailing --------------------------------------------------------
    Evidence(_p(r"\b(OLA\s*(?:CABS|MONEY)?|UBER(?!\s*EATS))\b"),
             (H.TRANSPORTATION, "Ride Hailing", "Cab"), 0.88),
    Evidence(_p(r"\b(RAPIDO|BOUNCE|YULU)\b"), (H.TRANSPORTATION, "Ride Hailing", "Bike Taxi"), 0.88),

    # -- Travel --------------------------------------------------------------
    Evidence(_p(r"\b(INDIGO|SPICEJET|AIR\s*INDIA|VISTARA|AKASA|GO\s*FIRST|GOAIR|"
                r"EMIRATES|LUFTHANSA|QATAR\s*AIRWAYS|SINGAPORE\s*AIRLINES)\b"),
             (H.TRAVEL, "Flights"), 0.90),
    Evidence(_p(r"\b(MAKEMYTRIP|MMT|GOIBIBO|CLEARTRIP|YATRA|EASEMYTRIP|IXIGO|"
                r"EXPEDIA|BOOKING\.?COM|AGODA)\b"),
             (H.TRAVEL, "Travel Agencies"), 0.90),
    Evidence(_p(r"\b(OYO|TAJ\s*HOTEL|MARRIOTT|HYATT|RADISSON|LEMON\s*TREE|ITC\s*HOTEL|AIRBNB)\b"),
             (H.TRAVEL, "Hotels"), 0.90),
    Evidence(_p(r"\b(REDBUS|ABHIBUS)\b"), (H.TRAVEL, "Buses"), 0.90),

    # -- Entertainment -------------------------------------------------------
    Evidence(_p(r"\b(NETFLIX|PRIME\s*VIDEO|HOTSTAR|DISNEY\+?|SONYLIV|SONY\s*LIV|ZEE5|"
                r"VOOT|JIOCINEMA|AHA\s*VIDEO|APPLE\s*TV)\b"),
             (H.ENTERTAINMENT, "OTT / Streaming"), 0.93),
    Evidence(_p(r"\b(SPOTIFY|GAANA|WYNK|JIOSAAVN|APPLE\s*MUSIC|YOUTUBE\s*(?:MUSIC|PREMIUM))\b"),
             (H.ENTERTAINMENT, "Music"), 0.93),
    Evidence(_p(r"\b(BOOKMYSHOW|BMS\s*TICKET|PVR|INOX|CINEPOLIS|CARNIVAL\s*CINEMA)\b"),
             (H.ENTERTAINMENT, "Movies"), 0.90),
    Evidence(_p(r"\b(STEAM\s*GAMES|PLAYSTATION|XBOX|NINTENDO|EPIC\s*GAMES|GOOGLE\s*PLAY\s*GAMES)\b"),
             (H.ENTERTAINMENT, "Gaming"), 0.88),

    # -- Healthcare ----------------------------------------------------------
    Evidence(_p(r"\b(APOLLO\s*PHARMACY|MEDPLUS|NETMEDS|PHARMEASY|TATA\s*1MG|1MG|WELLNESS\s*FOREVER)\b"),
             (H.HEALTHCARE, "Pharmacy"), 0.93),
    Evidence(_p(r"\b(APOLLO\s*HOSPITAL|FORTIS|MAX\s*HEALTHCARE|MANIPAL\s*HOSPITAL|NARAYANA\s*HEALTH|AIIMS)\b"),
             (H.HEALTHCARE, "Hospital"), 0.92),
    Evidence(_p(r"\b(DR\s*LAL\s*PATH|SRL\s*DIAGNOST|METROPOLIS|THYROCARE|REDCLIFFE)\b"),
             (H.HEALTHCARE, "Diagnostics"), 0.92),

    # -- Education -----------------------------------------------------------
    Evidence(_p(r"\b(BYJU|UNACADEMY|VEDANTU|COURSERA|UDEMY|UPGRAD|SIMPLILEARN|"
                r"GREAT\s*LEARNING|WHITEHAT)\b"),
             (H.EDUCATION, "Courses & Training"), 0.90),

    # -- Shopping, category-specific ----------------------------------------
    Evidence(_p(r"\b(MYNTRA|AJIO|NYKAA\s*FASHION|H\s*&\s*M|ZARA|UNIQLO|LIFESTYLE\s*STORE|"
                r"PANTALOONS|WESTSIDE|MAX\s*FASHION|SHOPPERS\s*STOP)\b"),
             (H.SHOPPING, "Clothing"), 0.90),
    Evidence(_p(r"\b(CROMA|RELIANCE\s*DIGITAL|VIJAY\s*SALES|APPLE\s*STORE|SAMSUNG\s*(?:STORE|INDIA))\b"),
             (H.SHOPPING, "Electronics"), 0.88),
    Evidence(_p(r"\b(NYKAA(?!\s*FASHION)|PURPLLE|SEPHORA|BODY\s*SHOP)\b"),
             (H.SHOPPING, "Personal Care"), 0.88),
    Evidence(_p(r"\b(IKEA|PEPPERFRY|URBAN\s*LADDER|HOME\s*CENTRE|WAKEFIT)\b"),
             (H.SHOPPING, "Home & Furniture"), 0.90),
    Evidence(_p(r"\b(TANISHQ|KALYAN\s*JEWEL|MALABAR\s*GOLD|PC\s*JEWELLER|CARATLANE|BLUESTONE)\b"),
             (H.SHOPPING, "Jewelry"), 0.92),

    # -- Business software and services --------------------------------------
    Evidence(_p(r"\b(AWS|AMAZON\s*WEB\s*SERVICES|MICROSOFT\s*AZURE|GOOGLE\s*CLOUD|GCP|"
                r"DIGITALOCEAN|HEROKU|CLOUDFLARE|GODADDY|HOSTINGER|ATLASSIAN|"
                r"SLACK|ZOOM\.?US|ADOBE|AUTODESK|SALESFORCE|HUBSPOT|ZOHO|FRESHWORKS)\b"),
             (H.BUSINESS, "Professional Services", "IT Services"), 0.88),
    Evidence(_p(r"\b(GOOGLE\s*ADS|FACEBOOK\s*ADS|META\s*PLATFORMS|LINKEDIN\s*ADS|"
                r"ADWORDS|INSTAGRAM\s*ADS)\b"),
             (H.BUSINESS, "Marketing & Advertising"), 0.90),
)


# ---------------------------------------------------------------------------
# MARKETPLACES — merchants that sell across categories.
#
# The point of naming these separately is to stop at the merchant. A path
# through here ends at `Shopping > Online Shopping > <name>` unless the
# narration independently says what was bought.
# ---------------------------------------------------------------------------
MARKETPLACE_EVIDENCE: Tuple[Evidence, ...] = (
    Evidence(_p(r"\bAMAZON(?!\s*WEB)\b"), (H.SHOPPING, "Online Shopping", "Amazon"), 0.90, merchant="Amazon"),
    Evidence(_p(r"\bFLIPKART\b"),  (H.SHOPPING, "Online Shopping", "Flipkart"),  0.90, merchant="Flipkart"),
    Evidence(_p(r"\bMEESHO\b"),    (H.SHOPPING, "Online Shopping", "Meesho"),    0.90, merchant="Meesho"),
    Evidence(_p(r"\bSNAPDEAL\b"),  (H.SHOPPING, "Online Shopping", "Snapdeal"),  0.90, merchant="Snapdeal"),
    Evidence(_p(r"\bTATA\s*CLIQ\b"), (H.SHOPPING, "Online Shopping", "Tata CLiQ"), 0.90, merchant="Tata CLiQ"),
    Evidence(_p(r"\b(EBAY|ALIEXPRESS|SHOPIFY\s*STORE|ETSY)\b"), (H.SHOPPING, "Online Shopping"), 0.85),
)

# Product words that let a marketplace transaction go deeper — and only these.
# `AMAZON` gives Online Shopping; `AMAZON MOBILE` gives Electronics > Mobile,
# because the narration itself named the product.
PRODUCT_HINTS: Tuple[Tuple[re.Pattern, Tuple[str, ...]], ...] = (
    (_p(r"\b(MOBILE|SMARTPHONE|IPHONE|HANDSET)\b"),      (H.SHOPPING, "Electronics", "Mobile")),
    (_p(r"\b(LAPTOP|NOTEBOOK|MACBOOK|ULTRABOOK)\b"),     (H.SHOPPING, "Electronics", "Laptop")),
    (_p(r"\b(HEADPHONE|EARBUD|CHARGER|CABLE|ADAPTER|MOUSE|KEYBOARD)\b"),
                                                          (H.SHOPPING, "Electronics", "Accessories")),
    (_p(r"\b(REFRIGERATOR|FRIDGE|WASHING\s*MACHINE|MICROWAVE|AIR\s*CONDITIONER|\bAC\b|TELEVISION)\b"),
                                                          (H.SHOPPING, "Electronics", "Appliances")),
    (_p(r"\b(BOOKS?|KINDLE|PAPERBACK)\b"),               (H.SHOPPING, "Books")),
    (_p(r"\b(FURNITURE|SOFA|MATTRESS|WARDROBE)\b"),      (H.SHOPPING, "Home & Furniture")),
    (_p(r"\b(GROCER(?:Y|IES)|FRESH|PANTRY)\b"),          (H.FOOD_DINING, "Groceries", "Online Grocery")),
)


# ---------------------------------------------------------------------------
# Aggregators and rails that carry other people's money.
#
# Recognised so they can be explicitly REFUSED as category evidence. A PhonePe
# settlement tells you the rail and the collector, and nothing about what was
# sold. Left out of the tables above, they would still be matched by a
# counterparty extractor and end up looking like a merchant.
# ---------------------------------------------------------------------------
PASS_THROUGH_ENTITIES: re.Pattern = _p(
    r"\b(PHONEPE|PAYTM|GOOGLEPAY|GOOGLE\s*PAY|GPAY|BHARATPE|BHIM|MOBIKWIK|FREECHARGE|"
    r"RAZORPAY|PAYU|CCAVENUE|BILLDESK|CASHFREE|INSTAMOJO|PINELABS|PINE\s*LABS|EZETAP|"
    r"WORLDLINE|MSWIPE|STRIPE|PAYPAL|AMAZON\s*PAY|CRED|SLICE|JUSPAY)\b"
)


def is_pass_through(text: Optional[str]) -> bool:
    """Is this name a payment rail rather than a party with a business?"""
    return bool(text) and bool(PASS_THROUGH_ENTITIES.search(str(text)))


def _first_match(table: Sequence[Evidence], text: str,
                 direction: Optional[str]) -> Optional[Tuple[Evidence, str]]:
    dir_norm = (direction or "").strip().lower() or None
    if dir_norm in {"dr", "d"}:
        dir_norm = "debit"
    elif dir_norm in {"cr", "c"}:
        dir_norm = "credit"

    for ev in table:
        if ev.direction and dir_norm and ev.direction != dir_norm:
            continue
        m = ev.pattern.search(text)
        if m:
            return ev, m.group(0).strip()
    return None


def match_concept(text: str, direction: Optional[str] = None):
    return _first_match(CONCEPT_EVIDENCE, text, direction)


def match_merchant(text: str, direction: Optional[str] = None):
    return _first_match(MERCHANT_EVIDENCE, text, direction)


def match_marketplace(text: str, direction: Optional[str] = None):
    return _first_match(MARKETPLACE_EVIDENCE, text, direction)


def match_product_hint(text: str) -> Optional[Tuple[Tuple[str, ...], str]]:
    for pattern, path in PRODUCT_HINTS:
        m = pattern.search(text)
        if m:
            return path, m.group(0).strip()
    return None


def _self_check() -> None:
    """Every path in the tables above must exist in the tree.

    A typo here would otherwise surface as a category that renders in the API,
    has no node behind it, and cannot be drilled into — which is exactly the
    class of bug where a prediction claims a category the database has no row
    for. Runs at import; the cost is microseconds and the alternative is finding
    out in production.
    """
    bad = []
    for table in (CONCEPT_EVIDENCE, MERCHANT_EVIDENCE, MARKETPLACE_EVIDENCE):
        for ev in table:
            if not H.is_valid_path(ev.path):
                bad.append(" > ".join(ev.path))
    for _pattern, path in PRODUCT_HINTS:
        if not H.is_valid_path(path):
            bad.append(" > ".join(path))
    if bad:
        raise RuntimeError(
            "merchants.py references paths that are not in the category tree: "
            + "; ".join(sorted(set(bad)))
        )


_self_check()


__all__ = [
    "Evidence", "CONCEPT_EVIDENCE", "MERCHANT_EVIDENCE", "MARKETPLACE_EVIDENCE",
    "PRODUCT_HINTS", "PASS_THROUGH_ENTITIES", "is_pass_through",
    "match_concept", "match_merchant", "match_marketplace", "match_product_hint",
]
