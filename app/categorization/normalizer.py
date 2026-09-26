"""Narration normalisation for the categorisation pipeline.

Rules match against the output of `normalize_narration`, so this module decides
what evidence the rule engine ever gets to see. The guiding constraint is that
normalisation must remove noise (reference numbers, padding, separators) while
preserving every token that carries meaning: merchant names and semantic words
such as REFUND, REVERSAL, CHARGE, FEE, SALARY, RENT, TRANSFER.

    "upi-swIGgy-ORDER-12345"  ->  "UPI SWIGGY ORDER"
"""

from __future__ import annotations

import re
from typing import List

# Words that change the meaning of a transaction and must survive stripping even
# when they look like noise or sit adjacent to a reference number.
SEMANTIC_TOKENS = {
    "REFUND",
    "REVERSAL",
    "REVERSED",
    "CHARGE",
    "CHARGES",
    "FEE",
    "FEES",
    "SALARY",
    "RENT",
    "TRANSFER",
    "CREDIT",
    "DEBIT",
    "PAYMENT",
    "RECHARGE",
    "BILL",
    "SELF",
    "CASH",
    "WITHDRAWAL",
    "INTEREST",
    "BONUS",
    "SIP",
}

# Payment-rail prefixes. Kept (they are genuine evidence for Transfers) but they
# must never on their own outrank a merchant match.
RAIL_TOKENS = {"UPI", "NEFT", "RTGS", "IMPS", "ACH", "ATM", "POS", "ECS", "CHQ", "CHEQUE"}

# Separators that banks use between fields.
_SEPARATORS = re.compile(r"[\-_/\\|:;,.@#*~+()\[\]{}<>\"']+")
_WHITESPACE = re.compile(r"\s+")

# A token that is almost certainly a machine reference rather than a word:
#   - pure digits of length >= 4          (536883, 20261110001)
#   - long alphanumeric mixtures          (YESB0000123456, 4A7F9C2E11)
# Short numbers are preserved because they can be meaningful ("1MG", "5 STAR").
_PURE_DIGITS = re.compile(r"^\d{4,}$")
_ALNUM_REF = re.compile(r"^(?=.*\d)[A-Z0-9]{8,}$")

# Bank filler words that carry no categorisation signal.
_FILLER = {
    "REF",
    "REFNO",
    "RRN",
    "TXN",
    "TXNID",
    "TRANSACTION",
    "ID",
    "NO",
    "NUM",
    "UTR",
    "MSG",
    "INFO",
    "DESC",
    "NARRATION",
    "RTN",
    "SEQ",
    "BATCH",
}


def _is_reference_token(token: str) -> bool:
    """True when a token looks like a machine-generated identifier."""
    if token in SEMANTIC_TOKENS or token in RAIL_TOKENS:
        return False
    if _PURE_DIGITS.match(token):
        return True
    if _ALNUM_REF.match(token):
        return True
    return False


def tokenize_narration(text: str) -> List[str]:
    """Normalise and return the surviving tokens."""
    if not text:
        return []

    upper = str(text).upper().strip()
    # Collapse separators to spaces so "UPI-SWIGGY/ORDER" becomes three tokens.
    spaced = _SEPARATORS.sub(" ", upper)
    spaced = _WHITESPACE.sub(" ", spaced).strip()

    tokens: List[str] = []
    for tok in spaced.split(" "):
        if not tok:
            continue
        if tok in _FILLER:
            continue
        if _is_reference_token(tok):
            continue
        tokens.append(tok)

    return tokens


def normalize_narration(text: str) -> str:
    """Return the normalised narration string used for rule matching.

    Uppercased, separator-collapsed, whitespace-squeezed, with reference numbers
    and bank filler removed. Merchant and semantic words are preserved.
    """
    return " ".join(tokenize_narration(text))


def normalize_for_ml(text: str) -> str:
    """Narration form fed to the ML vectoriser.

    Deliberately the same transform as the rule engine uses. Training and
    inference must see identical preprocessing, and keeping one function makes
    that impossible to get wrong.
    """
    return normalize_narration(text)
