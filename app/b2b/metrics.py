"""How a number gets to say where it came from.

The brief is explicit: never claim a metric is verified when it was inferred.
That distinction is not a documentation problem, it is a data-model problem, so
every figure this API returns is wrapped in a `Metric` carrying its own
provenance rather than being a bare float in a JSON object.

Three sources, and the line between them is about evidence, not difficulty:

`EXTRACTED`
    Printed on the statement and read off it. A closing balance in the footer,
    a running balance in a column. If the document says it, it is extracted.

`CALCULATED`
    Arithmetic over extracted values, with no judgement in between. Total
    credits is a sum. Net flow is a subtraction. A calculated figure is exactly
    as reliable as the rows it was computed from, so its confidence is
    inherited from parse quality rather than invented.

`INFERRED`
    A judgement about what the data means. "This ₹78,000 monthly credit is a
    salary" is inference: the statement never says salary, a recurring-credit
    pattern says it. Every inferred metric must carry a `method` naming the
    technique and a `confidence` that is genuinely below 1.0.

The rule that keeps this honest: **confidence is only meaningful on INFERRED
metrics.** Extracted and calculated metrics take their confidence from the
parse, and if the parse was clean that is legitimately 1.0. An inferred metric
returning 1.0 is a bug — it means someone forgot to think about it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

EXTRACTED = "EXTRACTED"
CALCULATED = "CALCULATED"
INFERRED = "INFERRED"

VALID_SOURCES = {EXTRACTED, CALCULATED, INFERRED}


@dataclass
class Metric:
    """A single figure plus everything a reader needs to judge it."""

    value: Any
    source: str
    method: Optional[str] = None
    confidence: Optional[float] = None
    unit: Optional[str] = None          # "INR", "count", "ratio", "days", "months"
    basis: Optional[str] = None         # e.g. reported|movement|reconstructed|derived
    note: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.source not in VALID_SOURCES:
            raise ValueError(f"unknown metric source {self.source!r}")
        if self.source == INFERRED:
            if not self.method:
                raise ValueError("an INFERRED metric must name its method")
            if self.confidence is None:
                raise ValueError("an INFERRED metric must carry a confidence")
        if self.confidence is not None:
            self.confidence = round(max(0.0, min(1.0, float(self.confidence))), 3)

    def to_api(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"value": self.value, "source": self.source}
        for key in ("method", "confidence", "unit", "basis", "note", "evidence"):
            val = getattr(self, key)
            if val is not None:
                out[key] = val
        return out


# ---------------------------------------------------------------- constructors
# Thin helpers so call sites read as prose and cannot forget a required field.

def extracted(value, unit=None, basis=None, note=None, confidence=None) -> Metric:
    return Metric(value=value, source=EXTRACTED, unit=unit, basis=basis,
                  note=note, confidence=confidence)


def calculated(value, unit=None, method=None, confidence=None,
               basis=None, note=None) -> Metric:
    return Metric(value=value, source=CALCULATED, unit=unit, method=method,
                  confidence=confidence, basis=basis, note=note)


def inferred(value, method: str, confidence: float, unit=None,
             note=None, evidence=None) -> Metric:
    return Metric(value=value, source=INFERRED, method=method,
                  confidence=confidence, unit=unit, note=note, evidence=evidence)


def unavailable(reason: str) -> Metric:
    """A metric we could not compute, said out loud.

    Returning `null` with a reason beats omitting the key: an integrator can
    tell "we looked and could not determine this" from "this API version does
    not have that field", and neither is silently read as zero. A missing
    salary figure that a client defaults to 0 is how a lending decision gets
    made on a number nobody produced.
    """
    return Metric(value=None, source=CALCULATED, note=reason, unit=None)


def block(metrics: Dict[str, Optional[Metric]]) -> Dict[str, Any]:
    """Render a dict of Metrics into the API shape, dropping absent entries."""
    return {k: v.to_api() for k, v in metrics.items() if v is not None}


@dataclass
class Warning_:
    """A caveat attached to the whole analysis rather than one figure."""
    code: str
    message: str
    severity: str = "warning"           # info | warning | critical
    detail: Optional[Dict[str, Any]] = None

    def to_api(self) -> Dict[str, Any]:
        out = {"code": self.code, "message": self.message, "severity": self.severity}
        if self.detail:
            out["detail"] = self.detail
        return out


class WarningCollector:
    """Accumulates caveats during an analysis run."""

    def __init__(self) -> None:
        self._items: List[Warning_] = []

    def add(self, code: str, message: str, severity: str = "warning",
            detail: Optional[Dict[str, Any]] = None) -> None:
        self._items.append(Warning_(code, message, severity, detail))

    def extend(self, other: "WarningCollector") -> None:
        self._items.extend(other._items)

    @property
    def items(self) -> List[Warning_]:
        return self._items

    def to_api(self) -> List[Dict[str, Any]]:
        return [w.to_api() for w in self._items]

    def has(self, code: str) -> bool:
        return any(w.code == code for w in self._items)


# ------------------------------------------------------------- warning codes
# Stable strings. Clients switch on these, so they are part of the contract.
W_NO_BALANCE_COLUMN = "NO_BALANCE_COLUMN"
W_BALANCE_CONTINUITY_FAILED = "BALANCE_CONTINUITY_FAILED"
W_CONTINUITY_UNVERIFIABLE = "CONTINUITY_UNVERIFIABLE"
W_SHORT_HISTORY = "SHORT_HISTORY"
W_SPARSE_HISTORY = "SPARSE_HISTORY"
W_ROWS_REJECTED = "ROWS_REJECTED"
W_DUPLICATE_ROWS = "DUPLICATE_ROWS"
W_LOW_PARSE_CONFIDENCE = "LOW_PARSE_CONFIDENCE"
W_NO_SALARY_DETECTED = "NO_SALARY_DETECTED"
W_MULTI_CURRENCY = "MULTI_CURRENCY"
W_OCR_USED = "OCR_USED"
W_NO_OPENING_BALANCE = "NO_OPENING_BALANCE"
W_CLASSIFIER_DEGRADED = "CLASSIFIER_DEGRADED"
W_UNCLASSIFIED_MAJORITY = "UNCLASSIFIED_MAJORITY"
