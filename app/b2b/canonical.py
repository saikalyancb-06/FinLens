"""The one transaction shape everything downstream agrees on.

Every supported input format — PDF, XLSX, CSV, TSV, JSON, OFX, CAMT — is
reduced to a list of `CanonicalTxn` before any analysis runs. Nothing in
`app/b2b/analysis/` is allowed to know which format a row came from; that is
the whole point of this module.

Two design constraints shaped it:

1. **It must be attribute-compatible with the existing pure kernels.** The
   recurring detector (`app/treasury/recurring_detector.py`), the sixteen
   anomaly detectors and the eight policy evaluators all read attributes off
   whatever objects they are handed — `debit_paise`, `credit_paise`,
   `balance_paise`, `txn_date`, `row_index`, `narration_clean`, `counterparty`,
   `flow_type` and so on. They type-hint `Transaction` but never check it. So
   `CanonicalTxn` carries exactly those names and can be passed to them
   unchanged, with no database and no ORM session.

2. **Money is integer minor units, never float.** The production ledger stores
   paise in `BigInteger` and only divides by 100 at the response boundary.
   Doing the same here means an API figure and a ledger figure computed from
   the same statement agree exactly, instead of differing in the third decimal
   because one path went through binary floating point.
"""
from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

# Mirrors app.models.transaction.Direction values so a canonical row and a
# stored row describe direction with the same two strings.
DEBIT = "debit"
CREDIT = "credit"


def to_minor(value: Any) -> Optional[int]:
    """Rupees (or any major unit) to integer minor units, half-up.

    Deliberately identical to `app/services/transaction_storage.py::_paise`,
    including the part that surprises people: **zero becomes None, not 0**. An
    empty debit column in a CSV parses to 0.00, and storing that as 0 would
    make "no debit on this row" indistinguishable from "a debit of nothing" in
    every SUM and every `debit_paise > 0` test downstream.
    """
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    if d == 0:
        return None
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor(value: Optional[int]) -> Optional[float]:
    """Minor units back to major, for the response boundary only."""
    if value is None:
        return None
    return round(value / 100.0, 2)


@dataclass
class CanonicalTxn:
    """One transaction, format-agnostic.

    Field names in the first block are dictated by the existing kernels and
    must not be renamed without checking every caller in
    `app/b2b/analysis/engine.py`.
    """

    # -- read by the existing pure kernels -----------------------------------
    txn_date: datetime.date
    direction: str                                  # "debit" | "credit"
    debit_paise: Optional[int] = None               # money out, minor units
    credit_paise: Optional[int] = None              # money in, minor units
    balance_paise: Optional[int] = None             # running balance after row
    narration_raw: str = ""
    narration_clean: str = ""
    row_index: Optional[int] = None
    counterparty: Optional[str] = None
    merchant: Optional[str] = None
    flow_type: Optional[str] = None                 # INFLOW/OUTFLOW/TRANSFER/REVERSAL
    category: Optional[str] = None                  # tree level 1
    category_path: Optional[str] = None             # "A > B > C"
    legacy_category: Optional[str] = None
    category_confidence: Optional[float] = None
    transaction_method: Optional[str] = None        # UPI/NEFT/IMPS/RTGS/ATM/...
    # The detectors group by these; None is fine and means "single unnamed
    # account", which is the normal case for a one-file API request.
    id: Optional[str] = None
    account_id: Optional[str] = None
    statement_id: Optional[str] = None

    # -- carried for the API response, not read by the kernels ---------------
    currency: str = "INR"
    value_date: Optional[datetime.date] = None
    reference_number: Optional[str] = None
    parse_confidence: float = 1.0                   # from the parser/validator
    parse_warnings: List[str] = field(default_factory=list)
    source_format: Optional[str] = None             # "pdf", "csv", "ofx", ...
    classification_method: Optional[str] = None     # rule|ml|hybrid|residual|none
    classification_rule: Optional[str] = None
    requires_review: bool = False
    balance_anomaly: bool = False

    # ------------------------------------------------------------------ utils
    @property
    def amount_paise(self) -> int:
        """Magnitude, whichever side it sits on."""
        return int(self.debit_paise or 0) + int(self.credit_paise or 0)

    @property
    def signed_paise(self) -> int:
        """Positive for money in, negative for money out."""
        return int(self.credit_paise or 0) - int(self.debit_paise or 0)

    @property
    def is_debit(self) -> bool:
        return self.direction == DEBIT

    @property
    def is_credit(self) -> bool:
        return self.direction == CREDIT

    def fingerprint(self) -> str:
        """Stable per-row hash, used for duplicate reporting."""
        parts = [
            self.txn_date.isoformat() if self.txn_date else "",
            str(self.debit_paise or 0),
            str(self.credit_paise or 0),
            (self.narration_clean or self.narration_raw or "")[:120],
            self.reference_number or "",
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]

    def to_api(self, include_narration: bool = True) -> Dict[str, Any]:
        """The public per-transaction shape documented in the OpenAPI spec.

        Amounts are majors here — this is the response boundary. `amount` is
        unsigned, matching the sign convention the rest of the payload uses;
        `direction` carries the sign information instead, because a client
        summing a column should not have to know whether we chose to negate
        debits.
        """
        out: Dict[str, Any] = {
            "date": self.txn_date.isoformat() if self.txn_date else None,
            "value_date": self.value_date.isoformat() if self.value_date else None,
            "description": (self.narration_raw or "") if include_narration else None,
            "amount": from_minor(self.amount_paise) or 0.0,
            "type": "DEBIT" if self.is_debit else "CREDIT",
            "balance": from_minor(self.balance_paise),
            "currency": self.currency,
            "reference": self.reference_number,
            "category": self.category,
            "category_path": self.category_path,
            "category_confidence": (
                round(float(self.category_confidence), 3)
                if self.category_confidence is not None else None
            ),
            "flow_type": self.flow_type,
            "method": self.transaction_method,
            "counterparty": self.counterparty,
            "merchant": self.merchant,
            "requires_review": self.requires_review,
        }
        if self.parse_warnings:
            out["warnings"] = sorted(set(self.parse_warnings))
        if self.balance_anomaly:
            out["balance_anomaly"] = True
        return out
