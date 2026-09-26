"""Deciding whether a bank line is cash, from narration text alone.

This is the weakest link in every statutory cash rule, and it deserves to be
honest about itself. A bank statement does not carry a "this was cash" flag. All
you get is a narration string, and the wording varies by bank. So every cash
determination here is an inference with a confidence, never a fact — and the UI
labels violations built on it accordingly.

What this does catch reliably: ATM withdrawals, branch cash deposits, CDM/cash
recycler entries, and self-cheque encashment, because banks are fairly
consistent about those. What it cannot catch: a transfer that was really a cash
deal settled through the account, or a cash entry a bank labels only with a
branch code.
"""
import re

# Ordered most-specific first; the first hit wins.
_CASH_PATTERNS = [
    (r"\bcash\s*dep(osit)?\b", "cash_deposit", 0.95),
    (r"\bcash\s*wdl\b|\bcash\s*withdrawal\b", "cash_withdrawal", 0.95),
    (r"\bcdm\b|\bcash\s*deposit\s*machine\b", "cash_deposit", 0.9),
    (r"\batm\s*wdl\b|\batm\s*cash\b|\bnwd\b", "cash_withdrawal", 0.9),
    (r"\batm\b", "cash_withdrawal", 0.75),
    (r"\bself\b.*\bchq\b|\bchq\b.*\bself\b", "cash_withdrawal", 0.7),
    (r"\bby\s+cash\b|\bto\s+cash\b|\bcash\b", "cash_generic", 0.6),
]

# Narrations that contain "cash" but are definitely not a cash movement.
_NOT_CASH = re.compile(
    r"cashback|cash\s*back|cashless|encashment\s*of\s*fd|cash\s*credit\s*a/?c|"
    r"cc\s*limit|cash\s*mgmt|cash\s*management",
    re.IGNORECASE,
)

# Electronic rails: if the narration says one of these, it was not cash.
_ELECTRONIC = re.compile(
    r"\bneft\b|\brtgs\b|\bimps\b|\bupi\b|\bnach\b|\becs\b|\bach\b|"
    r"\bpos\b|\bcard\b|\bnetbank\b|\bib\s*txn\b|\bmobile\s*bank\b",
    re.IGNORECASE,
)


def classify_cash(narration: str):
    """Return (is_cash, kind, confidence) for a narration string.

    confidence is 0.0-1.0. Anything below ~0.7 should be treated as a hint for a
    human rather than grounds for asserting a statutory breach.
    """
    if not narration:
        return False, None, 0.0

    text = narration.lower()

    if _NOT_CASH.search(text):
        return False, None, 0.0

    for pattern, kind, confidence in _CASH_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            # An explicit electronic rail beats a loose "cash" mention: a
            # narration like "UPI/CASHIER" is not a cash transaction.
            if confidence < 0.9 and _ELECTRONIC.search(text):
                return False, None, 0.0
            return True, kind, confidence

    return False, None, 0.0


def is_cash(narration: str, min_confidence: float = 0.6) -> bool:
    cash, _, confidence = classify_cash(narration)
    return cash and confidence >= min_confidence
