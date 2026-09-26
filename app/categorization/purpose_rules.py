"""Derive (purpose, event_type) from a narration when the legacy label can't say.

Used for the rail-named legacy categories — "NEFT Transfer" and friends — where
the stored label describes how the money moved and therefore cannot tell you what
it was for.

All matching runs on OCR-corrected, uppercased text: the live data contains
digit-for-letter substitution (POSRENT -> P05RENT, PHONEPE -> PH0NEPE, TID -> T1D)
and none of these patterns match without correcting it first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from app.categorization.dual_taxonomy import (
    BANK_CHARGE, BANK_FEES, COST_OF_GOODS, CUSTOMER_RECEIPT, FINANCE_COST,
    INTERNAL_MOVEMENT,
    INTERNAL_TRANSFER, MERCHANT_SETTLEMENT, OWNER_CONTRIBUTION, OWNER_FUNDING,
    PAYROLL, RENT_PREMISES, SALARY_WAGES, SALES_INCOME, STATUTORY_PAYMENT,
    TAXES_STATUTORY, VENDOR_PAYMENT,
)

# Digits that OCR substitutes for letters in this data set.
_OCR = {"0": "O", "1": "I", "5": "S", "8": "B"}

# The token-level repair below cannot save every word. `INT.COLL` arrives as
# `1NT.C011` — split on the dot, `C011` is one letter and three digits, which
# is indistinguishable from a reference number by shape alone. Correcting it
# would mean correcting reference numbers too.
#
# So the patterns themselves are written to tolerate the damage. Each letter
# that OCR confuses becomes a character class, and `1` stands for both I and L
# because the scanner uses it for both (`C011` is COLL, `MARUTH1` is MARUTHI).
_TOLERANT = {
    "O": "[O0]", "I": "[I1]", "S": "[S5]", "B": "[B8]", "L": "[L1]",
}


def tolerant(word: str) -> str:
    """A regex fragment matching `word` however OCR mangled it."""
    return "".join(_TOLERANT.get(ch, ch) for ch in word.upper())


def ocr_correct(text: str) -> str:
    """Repair digit-for-letter OCR damage without touching reference numbers.

    A token is only corrected when it looks like a damaged word — at least two
    letters, and digits making up no more than half of it. That leaves genuine
    identifiers such as AXNPN09200047279 and 5091357116 untouched.

    The threshold was three letters until a real statement showed the cost.
    Every row on it was scaffolded `EBANK:W1B/<ref>/<name>`, and `W1B` is two
    letters and a digit — never repaired, so the channel pattern (which expects
    letters there) never matched, and every one of those rows was grouped by
    narration shape instead of by counterparty. `1NT` for INT was lost the same
    way. Two letters still excludes identifiers: `BT25040124001012` has two
    letters and fourteen digits, so the digit ratio rejects it.
    """
    if not text:
        return ""

    def repl(m: re.Match) -> str:
        w = m.group(0)
        letters = sum(ch.isalpha() for ch in w)
        digits = sum(ch.isdigit() for ch in w)
        if letters >= 2 and 0 < digits <= letters / 2:
            return "".join(_OCR.get(ch, ch) if ch.isdigit() else ch for ch in w)
        return w

    return re.sub(r"[A-Za-z0-9]+", repl, text.upper())


# (pattern, purpose, event_type, note, certain) — ordered, first match wins.
# Every entry is grounded in an observed pattern in the live ledger.
#
# CERTAIN vs PROVISIONAL, and this distinction is what decides whether a human
# gets asked about the row.
#
#   certain=True   the narration states a FACT about the transaction, in the
#                  bank's own vocabulary. "CHARGES FOR ...", "LEDGER FOLIO
#                  CHARGES", "CBDT", "BY CASH", "SELF". There is nothing for a
#                  person to add, and asking them is how a review queue grows
#                  to 73 questions for one month's statement.
#
#   certain=False  the narration identifies a COUNTERPARTY or a rail, and the
#                  purpose is inferred from it. "PHONEPE" tells you a wallet
#                  aggregator settled money into the account; whether this
#                  particular row is revenue is a judgement, so it is written
#                  AND queued for confirmation.
#
# Everything that was provisional before this distinction existed stayed
# provisional; only the bank-fact rules moved.
DERIVATION_RULES = [
    # ---- Acquirers and aggregators, split by whether the name can mean
    # anything OTHER than a settlement.
    #
    # CERTAIN (True): a merchant never PAYS BOBCARD, BharatPe, Pine Labs or
    # BillDesk — those names appear on a statement only when money is being
    # settled to you. Measured on the 2,862 labelled rows in `data.csv`, these
    # rules were right 605 times out of 612 and every one of those 612 was
    # still being sent to a human to confirm. That is ~600 questions asked to
    # hear "yes" 99% of the time, and it is the single biggest source of review
    # work on a real statement.
    #
    # PROVISIONAL (False): PhonePe, Paytm, Swiggy and Zomato are ALSO consumer
    # rails. These rules are direction-blind, so promoting them would call a
    # Swiggy dinner "Sales Income" with confidence. They keep asking until the
    # rule carries a direction.
    (r"\b" + tolerant("PHONEPE") + r"\b",   SALES_INCOME, MERCHANT_SETTLEMENT, "wallet aggregator settlement", False),
    (r"\b" + tolerant("PAYTM") + r"\b",     SALES_INCOME, MERCHANT_SETTLEMENT, "wallet aggregator settlement", False),
    (r"\b" + tolerant("RAZORPAY") + r"\b",  SALES_INCOME, MERCHANT_SETTLEMENT, "payment gateway settlement", False),
    (r"\b" + tolerant("CASHFREE") + r"\b",  SALES_INCOME, MERCHANT_SETTLEMENT, "payment gateway settlement", False),
    (r"\b" + tolerant("PLUXEE") + r"\b",    SALES_INCOME, MERCHANT_SETTLEMENT, "meal-card settlement", True),
    (r"\b" + tolerant("SODEXO") + r"\b",    SALES_INCOME, MERCHANT_SETTLEMENT, "meal-card settlement", False),
    (r"\b" + tolerant("EAZYDINER") + r"\b", SALES_INCOME, MERCHANT_SETTLEMENT, "dining aggregator settlement", False),
    (r"\b" + tolerant("SWIGGY") + r"\b",    SALES_INCOME, MERCHANT_SETTLEMENT, "food aggregator settlement", False),
    (r"\b" + tolerant("ZOMATO") + r"\b",    SALES_INCOME, MERCHANT_SETTLEMENT, "food aggregator settlement", False),
    (r"\b" + tolerant("DINEOUT") + r"\b",   SALES_INCOME, MERCHANT_SETTLEMENT, "dining aggregator settlement", False),
    (r"\b" + tolerant("MAGICPIN") + r"\b",  SALES_INCOME, MERCHANT_SETTLEMENT, "dining aggregator settlement", False),
    (r"\b" + tolerant("BOBCARD") + r"\b",   SALES_INCOME, MERCHANT_SETTLEMENT, "card acquirer settlement", True),
    # BharatPe was simply missing, and on the labelled set that was 206 rows of
    # merchant settlement going to a human one at a time. The rest are the
    # acquirers that sit alongside it in `merchants.PASS_THROUGH_ENTITIES` —
    # if the name is a payment rail there, it settles money here.
    # No trailing \b on purpose: banks glue product codes straight onto the
    # brand — `BHARATPEPPG`, `BHARATPE.PAYOUT@YES` — and requiring a word
    # boundary after it silently drops those rows.
    (r"\b" + tolerant("BHARATPE"),          SALES_INCOME, MERCHANT_SETTLEMENT, "card acquirer settlement", True),
    (r"\bPINE\s*LABS\b|\b" + tolerant("PINELABS") + r"\b",
                                            SALES_INCOME, MERCHANT_SETTLEMENT, "card acquirer settlement", True),
    (r"\b" + tolerant("MSWIPE") + r"\b",    SALES_INCOME, MERCHANT_SETTLEMENT, "card acquirer settlement", True),
    (r"\b" + tolerant("EZETAP") + r"\b",    SALES_INCOME, MERCHANT_SETTLEMENT, "card acquirer settlement", True),
    (r"\b" + tolerant("WORLDLINE") + r"\b", SALES_INCOME, MERCHANT_SETTLEMENT, "card acquirer settlement", True),
    (r"\b" + tolerant("BILLDESK") + r"\b",  SALES_INCOME, MERCHANT_SETTLEMENT, "payment gateway settlement", True),
    (r"\b" + tolerant("CCAVENUE") + r"\b",  SALES_INCOME, MERCHANT_SETTLEMENT, "payment gateway settlement", True),
    (r"\b" + tolerant("INSTAMOJO") + r"\b", SALES_INCOME, MERCHANT_SETTLEMENT, "payment gateway settlement", True),
    (r"\b" + tolerant("JUSPAY") + r"\b",    SALES_INCOME, MERCHANT_SETTLEMENT, "payment gateway settlement", True),
    (r"\bBT\d*/",                          SALES_INCOME, MERCHANT_SETTLEMENT, "card batch settlement", False),

    # ---- CERTAIN from here down: the bank's own vocabulary, describing what
    # it did. A person confirming "LEDGER FOLIO CHARGES - CC/OD" is a bank
    # charge adds nothing.

    # POS terminal rental, not premises rent. 39 rows carried the legacy label
    # "Rent Payment", which is why real rent had nowhere to go. No trailing \b:
    # the live text is P05RENT_MAR25_T1D_34016239 and an underscore is a word
    # character, so a boundary never matches after the T.
    (r"\b" + tolerant("POSRENT"),            BANK_FEES,       BANK_CHARGE,        "POS terminal rental", True),
    (r"\b" + tolerant("POS") + r"\s*" + tolerant("RENT"), BANK_FEES, BANK_CHARGE, "POS terminal rental", True),

    # Statutory.
    (r"\b" + tolerant("CBDT") + r"\b|\b" + tolerant("TIN") + r"\d",
                                             TAXES_STATUTORY, STATUTORY_PAYMENT,  "direct tax challan", True),
    # Rent with GST on it is rent, not a tax payment. `5H0BHA G RENT GST` is a
    # landlord being paid; filing it under Taxes & Statutory would put premises
    # rent in the wrong line of the P&L. Provisional rather than certain,
    # because which of the two the row leans on is a judgement — and once the
    # user answers it for this landlord, the counterparty memory holds.
    (r"\b" + tolerant("RENT") + r"\b.*\b" + tolerant("GST") + r"\b|"
     r"\b" + tolerant("GST") + r"\b.*\b" + tolerant("RENT") + r"\b",
                                             RENT_PREMISES,   VENDOR_PAYMENT,     "rent with GST", False),
    (r"\b" + tolerant("GST") + r"\b",       TAXES_STATUTORY, STATUTORY_PAYMENT,  "GST payment", True),
    (r"\b" + tolerant("TDS") + r"\b",       TAXES_STATUTORY, STATUTORY_PAYMENT,  "TDS payment", True),
    (r"\bESIC?\b|\bEPFO?\b|\bPF\b",       TAXES_STATUTORY, STATUTORY_PAYMENT,  "statutory contribution", True),

    # Interest the bank charged or collected. Distinct from a fee: it is the
    # cost of borrowing, and it belongs in finance cost rather than bank
    # charges, because those two lines are read by different people.
    (r"\b" + tolerant("INT") + r"\.?\s*" + tolerant("COLL"),
                                             FINANCE_COST,    BANK_CHARGE,        "interest collected by bank", True),
    (r"\b" + tolerant("PENAL") + r"\s*(" + tolerant("CHARGE") + "|" + tolerant("INT") + ")",
                                             FINANCE_COST,    BANK_CHARGE,        "penal interest", True),
    (r"\b" + tolerant("INTEREST") + r"\s+(" + tolerant("COLL") + "|" + tolerant("DEBIT") + "|" + tolerant("CHARGED") + ")",
                                             FINANCE_COST,    BANK_CHARGE,        "interest charged", True),

    # The bank's own charges.
    (r"\b" + tolerant("CHARGES") + r"?\s+" + tolerant("FOR") + r"\b",
                                             BANK_FEES,       BANK_CHARGE,        "bank charge", True),
    (r"\b" + tolerant("PROCESSING") + r"\s*(" + tolerant("FEE") + "|" + tolerant("CHG") + ")",
                                             BANK_FEES,       BANK_CHARGE,        "bank charge", True),
    (r"\bNEFT\s*(" + tolerant("CHG") + "|" + tolerant("CHARGE") + ")",
                                             BANK_FEES,       BANK_CHARGE,        "transfer charge", True),
    (r"\bIMPS\s*(" + tolerant("CHG") + "|" + tolerant("CHARGE") + ")",
                                             BANK_FEES,       BANK_CHARGE,        "transfer charge", True),
    (r"\b" + tolerant("SMS") + r"\s*" + tolerant("CHARGE"),
                                             BANK_FEES,       BANK_CHARGE,        "bank charge", True),
    (r"\b" + tolerant("LEDGER") + r"\s*" + tolerant("FOLIO") + r"\b",
                                             BANK_FEES,       BANK_CHARGE,        "bank charge", True),
    (r"\b" + tolerant("CASH") + r"\s*" + tolerant("HANDLING") + r"\b",
                                             BANK_FEES,       BANK_CHARGE,        "bank charge", True),
    (r"\b" + tolerant("CHEQUE") + r"\s*" + tolerant("BOOK") + r"\b",
                                             BANK_FEES,       BANK_CHARGE,        "bank charge", True),

    # Cash over the counter. The narration says how it moved and that is the
    # whole fact — a cash deposit IS a cash deposit. What it was FOR is a
    # separate question the statement does not answer for anyone, so asking a
    # person to confirm the movement buys nothing.
    (r"^\s*" + tolerant("BY") + r"\s+" + tolerant("CASH") + r"\b",
                                             INTERNAL_MOVEMENT, INTERNAL_TRANSFER, "cash deposit", True),
    (r"^\s*" + tolerant("TO") + r"\s+" + tolerant("CASH") + r"\b",
                                             INTERNAL_MOVEMENT, INTERNAL_TRANSFER, "cash withdrawal", True),
    (r"\b" + tolerant("CASH") + r"\s*" + tolerant("DEP") + r"(" + tolerant("OSIT") + r")?\b",
                                             INTERNAL_MOVEMENT, INTERNAL_TRANSFER, "cash deposit", True),
    (r"\bATM\s*(" + tolerant("WDL") + "|" + tolerant("WITHDRAWAL") + ")",
                                             INTERNAL_MOVEMENT, INTERNAL_TRANSFER, "cash withdrawal", True),

    # Self / internal movement. `5ELF` in the live data.
    (r"\b" + tolerant("SELF") + r"\b",      INTERNAL_MOVEMENT, INTERNAL_TRANSFER, "self transfer", True),
]

_COMPILED = [(re.compile(p), pu, ev, note, certain)
             for p, pu, ev, note, certain in DERIVATION_RULES]


@dataclass(frozen=True)
class Derived:
    purpose: str
    event_type: Optional[str]
    note: str
    #: True when the narration states a fact rather than implying one. A
    #: certain answer is written and NOT queued; see the note on
    #: DERIVATION_RULES for why that distinction is the whole point.
    certain: bool


def derive_result(narration: str,
                  direction: Optional[str] = None) -> Optional[Derived]:
    """What the narration says about itself, or None if it says nothing."""
    if not narration:
        return None

    # Both forms, because the two repairs interfere. `PENA1` is corrected to
    # `PENAI` by the token pass — four letters and one digit look like a
    # damaged word — while the pattern for PENAL tolerates `1` as an L. Each
    # fix defeats the other on this one token. Searching the corrected text and
    # the raw text costs one extra regex pass and removes the whole class of
    # problem, instead of tuning one heuristic against the other forever.
    haystacks = (ocr_correct(narration), str(narration).upper())

    for pattern, purpose, event_type, note, certain in _COMPILED:
        if any(pattern.search(h) for h in haystacks):
            return Derived(purpose, event_type, note, certain)
    return None


def derive(narration: str, direction: Optional[str] = None) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (purpose, event_type, reason) or (None, None, None) if undecidable.

    Deliberately conservative: an unrecognised counterparty returns None rather
    than a guess, so the transaction reaches the review queue instead of being
    booked under an invented purpose.

    Kept as the three-value form for existing callers; `derive_result` adds
    whether the answer is certain.
    """
    got = derive_result(narration, direction=direction)
    if got is None:
        return (None, None, None)
    return (got.purpose, got.event_type, got.note)
