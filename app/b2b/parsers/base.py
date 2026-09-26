"""What every parser returns, and the row-shaping every parser shares.

The registry hands the rest of the API one shape, `ParseOutput`, regardless of
whether the bytes were a PDF, a semicolon CSV or an ISO 20022 message. Two
things in it are worth reading before writing a new parser:

`continuity_pass_rate` / `continuity_passed` are **nullable on purpose.**
    The legacy validator reports a 1.0 pass rate for a statement with no
    balance column at all, because it divides by `max(1, checked_rows)`. That
    turns "we checked nothing" into "everything passed", which is exactly the
    kind of unearned confidence this API is not allowed to emit. Here, a parser
    that could not check a single row reports `None` for both, sets
    `rows_checked_for_continuity = 0`, and says so with a
    `CONTINUITY_UNVERIFIABLE` warning. OFX and CAMT carry no running balance at
    all, so they are permanently in that state and it is not a defect.

`parse_warnings` are `(code, message)` pairs, not prose.
    The codes come from `app.b2b.metrics`; the caller lifts them into a
    `WarningCollector` unchanged, so a client can switch on them.
"""
from __future__ import annotations

import datetime
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.b2b.canonical import CREDIT, DEBIT, CanonicalTxn, to_minor
from app.b2b.errors import ApiError, FILE_EMPTY, PARSE_FAILED
from app.b2b.metrics import (
    W_LOW_PARSE_CONFIDENCE,
    W_NO_BALANCE_COLUMN,
    W_ROWS_REJECTED,
)

logger = logging.getLogger(__name__)

#: Mirrors `app/parsers/validator.py::BALANCE_TOLERANCE`. Re-declared rather
#: than imported so the two can diverge if the legacy gate is ever retuned
#: without silently changing what this API reports.
BALANCE_TOLERANCE = 1.00

#: Below this mean row confidence the parse is flagged, not rejected. The
#: caller decides whether a 0.6-confidence statement is worth analysing.
LOW_CONFIDENCE_MEAN = 0.70


@dataclass
class ParseOutput:
    """One statement, parsed. The only thing a parser is allowed to return."""

    transactions: List[CanonicalTxn] = field(default_factory=list)
    statement_meta: Dict[str, Any] = field(default_factory=dict)
    parse_warnings: List[Tuple[str, str]] = field(default_factory=list)
    continuity_pass_rate: Optional[float] = None
    continuity_passed: Optional[bool] = None
    rows_checked_for_continuity: int = 0

    def warn(self, code: str, message: str) -> None:
        self.parse_warnings.append((code, message))

    @property
    def warning_codes(self) -> List[str]:
        return [code for code, _ in self.parse_warnings]


# --------------------------------------------------------------------- guards

def require_readable_file(path: str) -> int:
    """Fail fast and precisely on the two cases every parser shares."""
    if not os.path.isfile(path):
        raise ApiError(PARSE_FAILED, "the uploaded file could not be read")
    size = os.path.getsize(path)
    if size == 0:
        raise ApiError(FILE_EMPTY, "the uploaded file contains no data")
    return size


# ------------------------------------------------------------ row conversion

def _direction_of(row: Dict[str, Any],
                  debit_paise: Optional[int],
                  credit_paise: Optional[int]) -> str:
    """Prefer the amounts over the label.

    `build_normalized_transaction` already reconciles a `transaction_type`
    column against the debit/credit columns, but PDF rows arrive from a dozen
    layout heuristics and can carry a label that contradicts the money. The
    money is what a client will sum, so the money wins and the label is only
    consulted when neither side carries a value.
    """
    if debit_paise and not credit_paise:
        return DEBIT
    if credit_paise and not debit_paise:
        return CREDIT
    label = str(row.get("transaction_type") or "").strip().lower()
    if label in ("debit", "credit"):
        return label
    return "unknown"


def row_to_canonical(row: Dict[str, Any],
                     *,
                     source_format: str,
                     currency: str = "INR",
                     fallback_index: int = 0) -> Optional[CanonicalTxn]:
    """One normalizer row dict -> one `CanonicalTxn`, or None if undatable.

    A row with an unparseable date is dropped rather than stamped with today:
    every downstream figure (period, monthly aggregates, recurrence) is keyed
    on the date, so a guessed one corrupts more than it rescues.
    """
    raw_date = str(row.get("date") or "").strip()
    try:
        txn_date = datetime.date.fromisoformat(raw_date)
    except (TypeError, ValueError):
        logger.debug("[b2b.parse] dropping row with unparseable date %r", raw_date)
        return None

    debit_paise = to_minor(row.get("debit"))
    credit_paise = to_minor(row.get("credit"))
    direction = _direction_of(row, debit_paise, credit_paise)

    # `to_minor` maps 0 to None by design (see canonical.py), which is right for
    # a debit/credit column but wrong for a balance: a genuine zero balance is a
    # fact about the account. It stays None here anyway, because the legacy row
    # dict uses 0.0 both for "balance was zero" and for "there was no balance
    # column", and inventing a distinction the source never made would be worse
    # than losing one. `rows_checked_for_continuity` is what tells the caller
    # whether balances were present at all.
    balance_paise = to_minor(row.get("balance"))

    description = str(row.get("description") or "").strip()
    warnings = [str(w) for w in (row.get("warnings") or [])]

    try:
        confidence = float(row.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0

    reference = str(row.get("reference_number") or "").strip() or None
    row_index = row.get("row_index")
    if not isinstance(row_index, int):
        row_index = fallback_index

    txn = CanonicalTxn(
        txn_date=txn_date,
        direction=direction,
        debit_paise=debit_paise,
        credit_paise=credit_paise,
        balance_paise=balance_paise,
        narration_raw=description,
        narration_clean=description,
        row_index=row_index,
        currency=currency,
        reference_number=reference,
        parse_confidence=round(confidence, 4),
        parse_warnings=warnings,
        source_format=source_format,
        balance_anomaly=bool(row.get("balance_anomaly")),
        requires_review=direction == "unknown",
    )

    # The legacy pipeline runs its rule/ML classifier on the way through. That
    # verdict is carried as `legacy_category` only — `category` and
    # `category_path` belong to the B2B classifier, which runs later and must
    # not find them pre-filled.
    decision = row.get("decision")
    if isinstance(decision, dict):
        txn.legacy_category = decision.get("category")
        txn.classification_method = decision.get("prediction_source")
        final_conf = decision.get("final_confidence")
        if isinstance(final_conf, (int, float)):
            txn.category_confidence = float(final_conf)
        txn.classification_rule = decision.get("matched_rule")

    return txn


def rows_to_canonical(rows: Sequence[Dict[str, Any]],
                      *,
                      source_format: str,
                      currency: str = "INR") -> Tuple[List[CanonicalTxn], int]:
    """Convert a batch, returning the transactions and how many were dropped."""
    out: List[CanonicalTxn] = []
    dropped = 0
    for idx, row in enumerate(rows):
        txn = row_to_canonical(row, source_format=source_format,
                               currency=currency, fallback_index=idx)
        if txn is None:
            dropped += 1
        else:
            out.append(txn)
    return out, dropped


# ------------------------------------------------------------------ continuity

def count_continuity_checkable(rows: Sequence[Dict[str, Any]]) -> int:
    """How many rows the balance-continuity check could actually evaluate.

    This deliberately re-derives a number the legacy validator computes and
    throws away. `_check_balance_continuity` counts `checked_rows` internally,
    divides by `max(1, checked_rows)` and returns only the ratio — so a
    statement with no balance column and one with a perfect balance column both
    come back as `1.0`, indistinguishable. Re-deriving it under the same
    conditions is the smallest change that makes the difference visible without
    editing a module other agents are working in.

    The conditions mirror the validator exactly: a comparison needs a previous
    non-zero balance and a current non-zero balance, is skipped for
    balance-only rows (no debit and no credit), and is skipped across rows
    carrying different intraday timestamps, where intervening transactions may
    exist.
    """
    prev_balance: Optional[float] = None
    prev_time: Optional[str] = None
    checked = 0

    for row in rows:
        try:
            debit = float(row.get("debit", 0.0) or 0.0)
            credit = float(row.get("credit", 0.0) or 0.0)
            curr_balance = float(row.get("balance", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        curr_time = str(row.get("time", "") or "").strip()
        is_balance_only = (debit == 0.0 and credit == 0.0 and curr_balance != 0.0)

        if (prev_balance is not None and prev_balance != 0.0
                and curr_balance != 0.0 and not is_balance_only):
            different_timestamps = bool(curr_time and prev_time and curr_time != prev_time)
            if not different_timestamps:
                checked += 1

        prev_balance = curr_balance
        if curr_time:
            prev_time = curr_time

    return checked


def apply_row_quality_warnings(output: ParseOutput,
                               rows: Sequence[Dict[str, Any]],
                               *,
                               rejected: int = 0,
                               dropped_undatable: int = 0) -> None:
    """Attach the caveats that apply to any tabular source."""
    if rejected:
        output.warn(W_ROWS_REJECTED,
                    f"{rejected} row(s) failed validation and were excluded")
    if dropped_undatable:
        output.warn(W_ROWS_REJECTED,
                    f"{dropped_undatable} row(s) had no parseable date and were excluded")

    if not any(float(r.get("balance", 0.0) or 0.0) for r in rows):
        output.warn(W_NO_BALANCE_COLUMN,
                    "no running balance was present on any row")

    confidences = []
    for row in rows:
        try:
            confidences.append(float(row.get("confidence", 1.0)))
        except (TypeError, ValueError):
            continue
    if confidences:
        mean_conf = sum(confidences) / len(confidences)
        if mean_conf < LOW_CONFIDENCE_MEAN:
            output.warn(
                W_LOW_PARSE_CONFIDENCE,
                f"mean row parse confidence {mean_conf:.2f} is below "
                f"{LOW_CONFIDENCE_MEAN:.2f}; figures derived from this statement "
                f"should be treated as provisional",
            )


# --------------------------------------------------------------------- meta

def observed_period(transactions: Sequence[CanonicalTxn]) -> Dict[str, Any]:
    """The date range the rows actually span.

    Reported under `period_source: "observed"` so it is never confused with a
    period printed on the statement (OFX `DTSTART/DTEND`, CAMT `FrToDt`), which
    is authoritative and reported as `"declared"`.
    """
    dates = [t.txn_date for t in transactions if t.txn_date]
    if not dates:
        return {}
    return {
        "period_start": min(dates).isoformat(),
        "period_end": max(dates).isoformat(),
        "period_source": "observed",
    }


def mask_account(identifier: Optional[str]) -> Optional[str]:
    """Keep the last four characters; a statement identifier is not ours to log."""
    if not identifier:
        return None
    cleaned = "".join(ch for ch in str(identifier) if ch.isalnum())
    if len(cleaned) <= 4:
        return "*" * len(cleaned)
    return "*" * (len(cleaned) - 4) + cleaned[-4:]


def set_balance(meta: Dict[str, Any], key: str, major: Optional[float],
                basis: Optional[str] = None) -> None:
    """Record a balance as both a major float and integer minor units.

    The major float carries the contractual key name a client reads; the
    `_paise` twin is what any arithmetic downstream should use, for the same
    reason the transactions themselves are integers.
    """
    if major is None:
        return
    meta[key] = round(float(major), 2)
    paise = to_minor(major)
    meta[f"{key}_paise"] = paise if paise is not None else 0
    if basis:
        meta[f"{key}_basis"] = basis
