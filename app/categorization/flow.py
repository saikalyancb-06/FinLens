"""Flow type and payment method: the two facts a narration usually does state.

These are separate from the category on purpose, and separating them is what
stops the category from absorbing them. A taxonomy that contains `NEFT Transfer`
as a category has stopped describing what money was for and started describing
how it travelled — which is how a single bucket ended up holding 853
transactions across 43 unrelated counterparties in this system's own history.
The rail belongs in a field of its own, where it can be filtered on without
competing with the thing it is not.

FLOW TYPE answers: did money arrive, leave, move sideways, or come back?

    INFLOW    money entered, and it is somebody else's money becoming yours
    OUTFLOW   money left
    TRANSFER  money moved without changing hands — your account to your account
    REVERSAL  an earlier transaction being undone

The distinction that earns its keep is TRANSFER versus INFLOW. A credit is not
income. Moving 2 lakh from your savings account to your current account credits
the current account, and counting that as revenue overstates the business by
2 lakh. Same for a loan disbursement, which is a credit and a liability. The
direction column cannot tell you this; the narration sometimes can, and when it
cannot, saying INFLOW with low confidence is the honest answer.

TRANSACTION METHOD answers: which rail carried it? This is the most reliably
extractable field in a bank statement — rails announce themselves, because the
narration is assembled by the system that processed the payment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Flow types
# ---------------------------------------------------------------------------
INFLOW   = "INFLOW"
OUTFLOW  = "OUTFLOW"
TRANSFER = "TRANSFER"
REVERSAL = "REVERSAL"

FLOW_TYPES: Tuple[str, ...] = (INFLOW, OUTFLOW, TRANSFER, REVERSAL)


# ---------------------------------------------------------------------------
# Payment methods
# ---------------------------------------------------------------------------
UPI            = "UPI"
NEFT           = "NEFT"
IMPS           = "IMPS"
RTGS           = "RTGS"
ECS            = "ECS"
ACH            = "ACH"
CARD           = "Card"
ATM            = "ATM"
CASH           = "Cash"
CHEQUE         = "Cheque"
BANK_TRANSFER  = "Bank Transfer"
DIRECT_DEBIT   = "Direct Debit"
OTHER_METHOD   = "Other"

METHODS: Tuple[str, ...] = (
    UPI, NEFT, IMPS, RTGS, ECS, ACH, CARD, ATM, CASH, CHEQUE,
    BANK_TRANSFER, DIRECT_DEBIT, OTHER_METHOD,
)

# Ordered most-specific first. `NEFT` and `RTGS` are named rails; `TRANSFER` on
# its own is not, so it must not win over them — hence order, not a dict.
#
# Every pattern is anchored on a word boundary. Without that, `ATM` matches
# inside `PATMOS TRADERS` and a supplier payment becomes a cash withdrawal.
_METHOD_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    (UPI,           re.compile(r"\b(UPI|BHIM|VPA|@(?:OKAXIS|OKHDFCBANK|OKICICI|OKSBI|YBL|PAYTM|IBL|AXL|APL))\b", re.I)),
    (NEFT,          re.compile(r"\bNEFT\b", re.I)),
    (RTGS,          re.compile(r"\bRTGS\b", re.I)),
    (IMPS,          re.compile(r"\bIMPS\b", re.I)),
    (ATM,           re.compile(r"\b(ATM|ATW|NWD|CASH\s*WDL|CASH\s*WITHDRAWAL|AWB)\b", re.I)),
    (CHEQUE,        re.compile(r"\b(CHQ|CHEQUE|CTS|CLG|CLEARING|INWARD\s*CLG|OUTWARD\s*CLG)\b", re.I)),
    (ECS,           re.compile(r"\bECS\b", re.I)),
    (ACH,           re.compile(r"\b(ACH|NACH)\b", re.I)),
    (DIRECT_DEBIT,  re.compile(r"\b(SI|STANDING\s*INSTRUCTION|AUTO\s*DEBIT|AUTOPAY|E-?MANDATE|MANDATE)\b", re.I)),
    (CARD,          re.compile(r"\b(POS|CARD|DEBIT\s*CARD|CREDIT\s*CARD|VISA|MASTERCARD|RUPAY|ECOM|E-?COM|MERCHANT\s*ID|MID)\b", re.I)),
    (CASH,          re.compile(r"\b(CASH\s*DEP|CASH\s*DEPOSIT|CDM|BY\s*CASH|TO\s*CASH|CASH\s*RECEIPT|CASH\b)", re.I)),
    (BANK_TRANSFER, re.compile(r"\b(TRANSFER|TRF|XFER|FT|FUND\s*TRANSFER|EBANK|NETBANKING|INB|MB)\b", re.I)),
)

# How much a rail match is worth. A rail token is close to proof — the string
# was written by the system that moved the money — but `TRANSFER` and `CASH` are
# ordinary English words that appear in narrations for other reasons.
_METHOD_CONFIDENCE = {
    UPI: 0.97, NEFT: 0.97, RTGS: 0.97, IMPS: 0.97,
    ATM: 0.92, CHEQUE: 0.92, ECS: 0.93, ACH: 0.93,
    DIRECT_DEBIT: 0.80, CARD: 0.85, CASH: 0.75, BANK_TRANSFER: 0.70,
    OTHER_METHOD: 0.0,
}

# What the parser may already have written on the row. Accepted as a hint, not
# as truth: it comes from the same narration this module reads, via a different
# and older extractor.
_METHOD_ALIASES = {
    "upi": UPI, "neft": NEFT, "imps": IMPS, "rtgs": RTGS, "ecs": ECS,
    "ach": ACH, "nach": ACH, "card": CARD, "pos": CARD, "debit card": CARD,
    "credit card": CARD, "atm": ATM, "cash": CASH, "chq": CHEQUE,
    "cheque": CHEQUE, "check": CHEQUE, "transfer": BANK_TRANSFER,
    "bank transfer": BANK_TRANSFER, "net banking": BANK_TRANSFER,
    "netbanking": BANK_TRANSFER, "si": DIRECT_DEBIT,
    "standing instruction": DIRECT_DEBIT, "direct debit": DIRECT_DEBIT,
    "other": OTHER_METHOD,
}


def normalize_method(value: Optional[str]) -> Optional[str]:
    """Canonical method name for a value written by another part of the system."""
    if not value:
        return None
    raw = str(value).strip()
    if raw in METHODS:
        return raw
    return _METHOD_ALIASES.get(raw.lower())


@dataclass(frozen=True)
class MethodResult:
    method: str
    confidence: float
    evidence: Optional[str] = None


def detect_method(narration: Optional[str],
                  declared: Optional[str] = None) -> MethodResult:
    """Which rail carried this transaction.

    `declared` is whatever the statement parser already put on the row. It wins
    only when the narration says nothing, because the narration is the primary
    source and the parser's value is a derivative of it.
    """
    text = str(narration or "")
    for method, pattern in _METHOD_PATTERNS:
        m = pattern.search(text)
        if m:
            return MethodResult(method, _METHOD_CONFIDENCE[method], m.group(0).strip())

    fallback = normalize_method(declared)
    if fallback:
        # Lower than a narration match: this is a second-hand reading of the
        # same string, so it cannot be more reliable than reading it here.
        return MethodResult(fallback, 0.60, f"declared:{declared}")

    return MethodResult(OTHER_METHOD, 0.0, None)


# ---------------------------------------------------------------------------
# Flow type
# ---------------------------------------------------------------------------

# A reversal undoes something. The words are distinctive and the consequence of
# missing one is a refund counted as revenue, so this is checked first.
_REVERSAL = re.compile(
    r"\b(REVERSAL|REVERSED|REV\s*OF|CHARGEBACK|CHRGBCK|"
    r"FAILED\s*(?:TXN|TRANSACTION|PAYMENT)|"
    r"RETURN(?:ED)?\s*(?:CHQ|CHEQUE|ECS|ACH|NACH|PAYMENT)|"
    r"REFUND\s*OF|REFUND\s*FOR|RVSL|RRN\s*REVERSAL|"
    r"AUTO\s*REVERSAL|TRANSACTION\s*DECLINED)\b",
    re.I,
)

# Phrases that say, in a bank's own vocabulary, "this money did not change
# hands". `SELF` is the strongest of them and the most common on Indian rails.
_SELF_TRANSFER = re.compile(
    r"\b(SELF|OWN\s*ACC?O?U?N?T?|OWN\s*TRANSFER|SELF\s*TRANSFER|"
    r"TO\s*SELF|FROM\s*SELF|INTERNAL\s*TRANSFER|INTRA\s*BANK|"
    r"ACCOUNT\s*TO\s*ACCOUNT|A/?C\s*TO\s*A/?C|SWEEP\s*(?:IN|OUT)|"
    r"FD\s*(?:BOOKING|CLOSURE|RENEWAL)|RD\s*(?:INSTAL?MENT|BOOKING))\b",
    re.I,
)


@dataclass(frozen=True)
class FlowResult:
    flow_type: str
    confidence: float
    evidence: Optional[str] = None


def detect_flow(direction: Optional[str],
                narration: Optional[str] = None,
                *,
                counterparty_is_self: Optional[bool] = None,
                category_path: Optional[Sequence[str]] = None) -> FlowResult:
    """Inflow, outflow, transfer or reversal.

    `direction` is the only input that is close to certain — a debit column with
    a figure in it is not a matter of interpretation. Everything above that is
    inference, and is reported with a confidence that says so.

    `counterparty_is_self` is for the caller that actually knows: if the payee
    matches one of the user's own registered accounts, that is evidence no
    amount of narration parsing can match, so it is taken as decisive.
    """
    text = str(narration or "")
    dir_norm = (str(direction or "").strip().lower() or None)
    is_debit = dir_norm in {"debit", "dr", "d", "outflow"}
    is_credit = dir_norm in {"credit", "cr", "c", "inflow"}

    m = _REVERSAL.search(text)
    if m:
        return FlowResult(REVERSAL, 0.90, m.group(0).strip())

    if counterparty_is_self:
        return FlowResult(TRANSFER, 0.96, "counterparty is a registered account of this user")

    # The category can settle it where the narration alone would not: a path
    # the classifier resolved to Transfers > Own Account Transfer already
    # represents a judgement made on more evidence than this function sees.
    if category_path:
        head = str(category_path[0])
        if head == "Transfers":
            sub = str(category_path[1]) if len(category_path) > 1 else ""
            if sub == "Own Account Transfer":
                return FlowResult(TRANSFER, 0.92, "category: own account transfer")
            # Person-to-person and external transfers DO change hands. They are
            # a transfer in the everyday sense and an inflow or outflow in the
            # accounting sense, and the accounting sense is what reports need.
            return FlowResult(
                OUTFLOW if is_debit else INFLOW, 0.70,
                "category: transfer between different parties",
            )
        if head == "Refunds & Reversals":
            return FlowResult(REVERSAL, 0.85, "category: refunds & reversals")

    m = _SELF_TRANSFER.search(text)
    if m:
        return FlowResult(TRANSFER, 0.82, m.group(0).strip())

    if is_debit:
        return FlowResult(OUTFLOW, 0.99, "debit")
    if is_credit:
        # Deliberately not certain. A credit is money arriving; whether it is
        # income, a transfer or borrowing is a different question, and one this
        # function has just failed to find evidence for. Reporting 0.99 here
        # would be reporting confidence in the direction column as though it
        # were confidence in the interpretation.
        return FlowResult(INFLOW, 0.80, "credit, with no transfer or reversal marker")

    return FlowResult(OUTFLOW if not is_credit else INFLOW, 0.0, "no direction given")


def is_income_flow(flow_type: Optional[str]) -> bool:
    """Should this row count toward income totals?

    The single question that made flow type worth having. Transfers and
    reversals must not.
    """
    return flow_type == INFLOW


def is_expense_flow(flow_type: Optional[str]) -> bool:
    return flow_type == OUTFLOW


__all__ = [
    "INFLOW", "OUTFLOW", "TRANSFER", "REVERSAL", "FLOW_TYPES",
    "UPI", "NEFT", "IMPS", "RTGS", "ECS", "ACH", "CARD", "ATM", "CASH",
    "CHEQUE", "BANK_TRANSFER", "DIRECT_DEBIT", "OTHER_METHOD", "METHODS",
    "MethodResult", "FlowResult", "detect_method", "detect_flow",
    "normalize_method", "is_income_flow", "is_expense_flow",
]
