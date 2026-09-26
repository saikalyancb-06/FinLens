"""Trade names: when a counterparty's name says what they sell.

THE DISTINCTION THIS MODULE IS BUILT ON.

    NEFT-CITIN2508-ABC PVT LTD        the name is just a name
    NEFT-HDFCH25-KUMAR FISH-HDFC      the name states a trade

The first is genuinely unreadable and must go to a human. The second is not: a
business called "Kumar Fish" sells fish, and a debit to them from a restaurant's
account is a food purchase. Sending that to a review queue asks a person to
re-type what the narration already said.

That distinction is the whole point. The rest of this system is careful never to
invent a purpose from a counterparty name — `ABC PVT LTD` must not become "Raw
Materials". This module is not an exception to that rule, it is the other half
of it: a name that NAMES A TRADE is evidence, and a name that is only a name is
not. `MARUTHI MOTORS` tells you about vehicles for the same reason `KUMAR FISH`
tells you about food, and neither tells you anything about `SRI VENKATESWARA
ENTERPRISES`.

WHY THE COUNT MATTERS. On a real 1,823-row restaurant statement the review queue
asked about 73 counterparties. A person answering 73 questions to file one
month's statement is a worse deal than doing it by hand, and the feature exists
to remove that work, not to relocate it. Most of those 73 are trades: fish,
vegetables, chicken, gas, packaging, a garage. Reading the trade out of the name
is the difference between asking 73 questions and asking the handful that are
genuinely unreadable.

DIRECTION DECIDES WHICH SIDE OF THE TRADE YOU ARE ON. Money going out to a fish
supplier is a purchase; money coming in from them is a sale to them. Same
keyword, opposite books, and the direction column settles it without guessing.

WHAT THIS DOES NOT DO. It never fires on a name that is only a name, it never
overrides a rule that matched the narration itself, and every answer records the
word that produced it — so a wrong one is visible as "matched FISH" rather than
appearing from nowhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.categorization import hierarchy as H
from app.categorization.purpose_rules import ocr_correct
from app.categorization.dual_taxonomy import (
    COST_OF_GOODS, CUSTOMER_RECEIPT, OTHER_PURPOSE, PROFESSIONAL_FEES,
    SALES_INCOME, TRANSPORTATION, UTILITIES, VENDOR_PAYMENT,
)


@dataclass(frozen=True)
class Trade:
    """A trade vocabulary, and where a payment to that trade belongs."""
    pattern: re.Pattern
    label: str                      # "a fish or meat supplier"
    flat_purpose: str               # the dual-taxonomy axis, for reports
    path: Tuple[str, ...]           # tree path on a personal account
    business_path: Tuple[str, ...]  # tree path on a current/business account


@dataclass(frozen=True)
class TradeMatch:
    trade: Trade
    matched_word: str
    flat_purpose: str
    event_type: Optional[str]
    path: Tuple[str, ...]

    @property
    def explanation(self) -> str:
        return (f'"{self.matched_word}" in the counterparty name identifies '
                f'{self.trade.label}')


def _p(regex: str) -> re.Pattern:
    return re.compile(regex, re.I)


BUSINESS = H.BUSINESS
_INVENTORY = (BUSINESS, "Inventory")
_RAW_MATERIALS = (BUSINESS, "Raw Materials")
_SUPPLIER = (BUSINESS, "Supplier Payment")

# Ordered; first match wins. Specific trades come before general ones, so
# `MEDICAL` beats `STORES` and `PETROL BUNK` beats `BUNK`.
TRADES: Tuple[Trade, ...] = (
    # ---- Food supply -------------------------------------------------------
    Trade(_p(r"\b(FISH|SEAFOOD|PRAWNS?|CRAB|MEAT|MUTTON|BEEF|PORK|"
             r"CHICKEN|CHIKEN|CHICKENS|POULTRY|EGGS?)\b"),
          "a fish, meat or poultry supplier", COST_OF_GOODS,
          (H.FOOD_DINING, "Groceries"), _INVENTORY),
    Trade(_p(r"\b(VEG|VEGS|VEGETABLES?|VEGETABLE|GREENS|FRUITS?|"
             r"SABZI|SUBZI|MANDI)\b"),
          "a vegetable or fruit supplier", COST_OF_GOODS,
          (H.FOOD_DINING, "Groceries"), _INVENTORY),
    Trade(_p(r"\b(MILK|DAIRY|DAIRIES|CURD|PANEER|GHEE|AAVIN|AMUL|NANDINI|HERITAGE\s*FOODS)\b"),
          "a dairy supplier", COST_OF_GOODS,
          (H.FOOD_DINING, "Groceries"), _INVENTORY),
    Trade(_p(r"\b(RICE|WHEAT|ATTA|FLOUR|DAL|PULSES|GRAINS?|CEREALS?|"
             r"MASALA|SPICES?|OIL\s*MILLS?|EDIBLE\s*OIL)\b"),
          "a staples or spices supplier", COST_OF_GOODS,
          (H.FOOD_DINING, "Groceries"), _RAW_MATERIALS),
    Trade(_p(r"\b(BAKERY|BAKERS?|SWEETS?|SWEET\s*HOUSE|CONFECTION\w*|"
             r"NAMKEEN|SNACKS?)\b"),
          "a bakery or sweets supplier", COST_OF_GOODS,
          (H.FOOD_DINING, "Cafes & Beverages", "Bakery"), _INVENTORY),
    Trade(_p(r"\b(KIRANA|GROCER\w*|PROVISION\w*|SUPERMARKET|HYPERMARKET|"
             r"GENERAL\s*STORES?|DEPARTMENTAL)\b"),
          "a grocery or provisions store", COST_OF_GOODS,
          (H.FOOD_DINING, "Groceries"), _INVENTORY),
    Trade(_p(r"\b(HOTELS?|RESTAURANTS?|DHABA|MESS|TIFFIN|CANTEEN|CATERERS?|"
             r"CATERING|BHOJAN\w*|FOODS?|FOOD\s*COURT|CAFE|CAFETERIA)\b"),
          "a food business", COST_OF_GOODS,
          (H.FOOD_DINING, "Restaurants"), _INVENTORY),
    Trade(_p(r"\b(BEVERAGES?|BREWER\w*|DISTILLER\w*|WINES?|LIQUOR|"
             r"SOFT\s*DRINKS?|AERATED)\b"),
          "a beverages supplier", COST_OF_GOODS,
          (H.FOOD_DINING, "Cafes & Beverages"), _INVENTORY),

    # ---- Vehicles and fuel -------------------------------------------------
    Trade(_p(r"\b(PETROL\s*BUNK|PETROLEUM|FUEL\s*S(TATION|TN)|FILLING\s*STATION|"
             r"HP\s*PETRO|INDIAN\s*OIL|IOCL|BPCL|HPCL|NAYARA|GAS\s*AGENC\w*)\b"),
          "a fuel supplier", TRANSPORTATION,
          (H.TRANSPORTATION, "Fuel"), (H.TRANSPORTATION, "Fuel")),
    Trade(_p(r"\b(MARUTHI|MARUTI|HYUNDAI|TOYOTA|MAHINDRA|TATA\s*MOTORS|"
             r"HONDA|BAJAJ|TVS|YAMAHA|SUZUKI|MOTORS?|AUTOMOBILES?|AUTOMOTIVE|"
             r"GARAGE|TYRES?|TYRE|SPARES?|SPARE\s*PARTS)\b"),
          "a vehicle dealer, garage or parts supplier", TRANSPORTATION,
          (H.TRANSPORTATION, "Vehicle Expenses"), (H.TRANSPORTATION, "Vehicle Expenses")),
    Trade(_p(r"\b(TRANSPORTS?|LOGISTICS|ROADLINES|ROADWAYS|CARRIERS?|CARGO|"
             r"COURIERS?|FREIGHT|PACKERS?\s*(AND|&)?\s*MOVERS?)\b"),
          "a transport or logistics provider", COST_OF_GOODS,
          (H.TRANSPORTATION, "Other Transportation"), _SUPPLIER),
    Trade(_p(r"\b(TRAVELS?|TOURS?\s*(AND|&)?\s*TRAVELS?|TRAVEL\s*AGENC\w*)\b"),
          "a travel agent", OTHER_PURPOSE,
          (H.TRAVEL, "Travel Agencies"), (H.BUSINESS, "Business Travel")),

    # ---- Health ------------------------------------------------------------
    Trade(_p(r"\b(PHARMAC\w*|MEDICALS?|MEDICOS|CHEMISTS?|DRUGS?\s*(HOUSE|STORE)?|"
             r"HOSPITALS?|CLINICS?|NURSING\s*HOME|DIAGNOSTICS?|LABS?|"
             r"POLYCLINIC|DENTAL|SCAN\s*CENTRE)\b"),
          "a pharmacy, clinic or hospital", OTHER_PURPOSE,
          (H.HEALTHCARE, "Pharmacy"), (H.HEALTHCARE, "Pharmacy")),

    # ---- Construction and hardware ----------------------------------------
    Trade(_p(r"\b(CEMENTS?|STEELS?|IRON|TMT|HARDWARES?|TIMBERS?|PLYWOODS?|"
             r"SANITARY|TILES?|MARBLES?|GRANITES?|BRICKS?|PAINTS?|"
             r"BUILDERS?|CONSTRUCTIONS?|INFRA\w*|ENGINEERS?|ENGINEERING)\b"),
          "a building materials or engineering supplier", COST_OF_GOODS,
          (H.HOUSING, "Home Improvement"), _RAW_MATERIALS),

    # ---- Textiles, packaging, printing -------------------------------------
    Trade(_p(r"\b(TEXTILES?|GARMENTS?|FABRICS?|SILKS?|COTTONS?|SAREES?|"
             r"HOSIERY|APPARELS?|TAILORS?)\b"),
          "a textiles or garments supplier", COST_OF_GOODS,
          (H.SHOPPING, "Clothing"), _INVENTORY),
    Trade(_p(r"\b(PACKAGING|PACKS?|PACKERS|CARTONS?|CORRUGAT\w*|"
             r"PLASTICS?|POLYMERS?|POUCH\w*)\b"),
          "a packaging supplier", COST_OF_GOODS,
          (H.SHOPPING, "Other Shopping"), _RAW_MATERIALS),
    Trade(_p(r"\b(PRINTERS?|PRINTING|PRESS|STATIONERS?|STATIONERY|"
             r"XEROX|GRAPHICS?|SIGNAGES?|FLEX)\b"),
          "a printing or stationery supplier", COST_OF_GOODS,
          (H.SHOPPING, "Other Shopping"), (H.BUSINESS, "Office Expenses")),

    # ---- Electrical, electronics, utilities --------------------------------
    Trade(_p(r"\b(ELECTRICALS?|ELECTRONICS?|CABLES?|WIRES?|LIGHTINGS?|"
             r"APPLIANCES?|REFRIGERATIONS?|AIRCON\w*|HVAC)\b"),
          "an electrical or electronics supplier", COST_OF_GOODS,
          (H.SHOPPING, "Electronics"), _INVENTORY),
    Trade(_p(r"\b(POWER\s*(CORP|DISCOM|SUPPLY)|ELECTRICITY\s*BOARD|"
             r"WATER\s*(BOARD|SUPPLY)|MUNICIPAL\w*|CORPORATION\s*OF)\b"),
          "a utility provider", UTILITIES,
          (H.BILLS_UTILITIES,), (H.BILLS_UTILITIES,)),

    # ---- Professional services --------------------------------------------
    Trade(_p(r"\b(CHARTERED\s*ACCOUNTANTS?|ACCOUNTANTS?|AUDITORS?|"
             r"ADVOCATES?|LAWYERS?|LEGAL\s*ASSOCIATES?|LAW\s*(FIRM|OFFICE))\b"),
          "an accounting or legal practice", PROFESSIONAL_FEES,
          (H.BUSINESS, "Professional Services"), (H.BUSINESS, "Professional Services")),
    Trade(_p(r"\b(CONSULTANC\w*|CONSULTANTS?|ADVISORY|SOLUTIONS?|"
             r"TECHNOLOG\w*|SOFTWARES?|INFOTECH|IT\s*SERVICES?|SYSTEMS?|"
             r"DIGITAL|WEB\s*SERVICES?)\b"),
          "a consulting or technology provider", PROFESSIONAL_FEES,
          (H.BUSINESS, "Professional Services"), (H.BUSINESS, "Professional Services")),
    Trade(_p(r"\b(SECURIT(Y|IES)\s*SERVICES?|MANPOWER|FACILIT(Y|IES)\s*"
             r"(MANAGEMENT|SERVICES?)|HOUSEKEEPING|PEST\s*CONTROL|"
             r"LAUNDR\w*|CLEANING\s*SERVICES?)\b"),
          "a facilities or manpower provider", COST_OF_GOODS,
          (H.BUSINESS, "Office Expenses"), (H.BUSINESS, "Office Expenses")),
    Trade(_p(r"\b(ADVERTIS\w*|MARKETING|MEDIA|BRANDING|CREATIVES?|STUDIOS?)\b"),
          "an advertising or media agency", PROFESSIONAL_FEES,
          (H.BUSINESS, "Marketing & Advertising"), (H.BUSINESS, "Marketing & Advertising")),

    # ---- Education and agriculture ----------------------------------------
    Trade(_p(r"\b(SCHOOLS?|COLLEGES?|UNIVERSIT\w*|ACADEM\w*|INSTITUTES?|"
             r"VIDYALAYA|VIDHYALAYA|TUITIONS?|COACHING|EDUCATIONS?)\b"),
          "an educational institution", OTHER_PURPOSE,
          (H.EDUCATION, "Tuition & Fees"), (H.EDUCATION, "Tuition & Fees")),
    Trade(_p(r"\b(AGRO|AGRI\w*|SEEDS?|FERTILIZERS?|FERTILISERS?|PESTICIDES?|"
             r"NURSER(Y|IES)|FARMS?|PLANTATIONS?)\b"),
          "an agricultural supplier", COST_OF_GOODS,
          (H.FOOD_DINING, "Groceries"), _RAW_MATERIALS),
)


# Words that make a name look like a business without saying what business it
# is. Recognised so they never count as a trade — this is the `ABC PVT LTD`
# case, and it must keep going to a human.
GENERIC_ONLY = _p(
    r"^\W*(?:(?:M/?S|MR|MRS|SHRI|SRI|SMT)\W+)?"
    r"[\w\s.&'-]*?"
    r"\b(?:PVT|PRIVATE|LTD|LIMITED|LLP|INC|CORP|CO|COMPANY|ENTERPRISES?|"
    r"TRADERS?|TRADING|AGENC(?:Y|IES)|ASSOCIATES?|"
    r"INDUSTR(?:Y|IES)|DISTRIBUTORS?|SUPPLIERS?|MARKETING\s*CO)\b\W*$"
)


def match(text: Optional[str], direction: Optional[str] = None,
          account_type: Optional[str] = None) -> Optional[TradeMatch]:
    """What trade, if any, this text names.

    `text` is normally the whole narration — the counterparty's name is in
    there, and matching the whole string means this works whether or not the
    counterparty extractor recognised the bank's format.

    Returns None when nothing matches, which is the answer for a name that is
    only a name.
    """
    if not text:
        return None

    # Both the repaired and the raw text. The scanner turns `MARUTHI` into
    # `MARUTH1`, and a trade word nobody can spell is a trade word nobody can
    # match — that one supplier was 11 rows still waiting on a person.
    haystacks = (ocr_correct(str(text)), str(text))

    for trade in TRADES:
        m = None
        for haystack in haystacks:
            m = trade.pattern.search(haystack)
            if m:
                break
        if not m:
            continue

        # Which side of the trade are we on? Money out to a fish supplier is a
        # purchase; money in from them is a sale. One keyword, two books, and
        # the direction column settles it rather than a guess.
        dir_norm = (direction or "").strip().lower()
        is_credit = dir_norm in {"credit", "cr", "c", "inflow"}

        if is_credit:
            flat = SALES_INCOME
            event = CUSTOMER_RECEIPT
            path = (H.INCOME, "Business Revenue")
        else:
            flat = trade.flat_purpose
            event = VENDOR_PAYMENT
            business = str(account_type or "").strip().lower() in {
                "current", "business", "merchant", "corporate"}
            path = trade.business_path if business else trade.path

        return TradeMatch(
            trade=trade,
            matched_word=m.group(0).strip().upper(),
            flat_purpose=flat,
            event_type=event,
            path=path,
        )
    return None


def names_only_a_company(text: Optional[str]) -> bool:
    """True when the name is corporate boilerplate and says nothing else.

    `ABC PVT LTD`, `SRI VENKATESWARA ENTERPRISES`. Used to keep those going to a
    human instead of being swept into a default.
    """
    if not text:
        return False
    return bool(GENERIC_ONLY.match(str(text).strip()))


def _self_check() -> None:
    """Every path in the table must exist in the tree, checked at import."""
    bad: List[str] = []
    for trade in TRADES:
        for path in (trade.path, trade.business_path):
            if not H.is_valid_path(path):
                bad.append(" > ".join(path))
    if bad:
        raise RuntimeError(
            "trades.py references paths that are not in the category tree: "
            + "; ".join(sorted(set(bad)))
        )


_self_check()


__all__ = ["Trade", "TradeMatch", "TRADES", "match", "names_only_a_company"]
