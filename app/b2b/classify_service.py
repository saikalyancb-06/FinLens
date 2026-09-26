"""Statement file + caller's rules -> classified transactions.

The one function that implements `POST /v1/classify`. Kept out of the router for
the same reason `service.py` is: the HTTP layer decides status codes, this layer
decides what a classified statement is.

WHY THIS IS A SEPARATE ENDPOINT AND NOT A FLAG ON /v1/analyze
-------------------------------------------------------------
`/v1/analyze` answers "what is this person's financial position" — income,
expenses, debt, affordability, risk. This answers "put my categories on these
rows". A caller that wants the second does not want to pay for the first, and
folding them together would mean either computing analysis nobody asked for or
adding a flag that silently changes what the response means.

Concretely, this path never imports `app.b2b.analysis`, so it does not touch the
recurring-series detector, the sixteen anomaly detectors or the ML stack unless
the caller explicitly asks for the built-in classifier as a fallback.

STATELESS, AND WHY THERE IS NO IDEMPOTENCY KEY
----------------------------------------------
Nothing here writes a transaction, a statement or a classification to the
database. The operation is a pure function of (file bytes, ruleset) — which is
also why, unlike `/v1/analyze`, it has no `Idempotency-Key` handling and no
`AnalysisRequest` row: replaying it cannot double-apply anything, so the
machinery that exists to make a retry safe has nothing to protect. Usage is
still metered, because that is how the caller is billed.

Synchronous only. A delimited or JSON statement of a few thousand rows
classifies in tens of milliseconds. The one input that can be slow is a scanned
PDF needing OCR; a caller sending those should use `/v1/analyze` with
`async_mode=true`, and `docs/CLASSIFY_API.md` says so rather than leaving it to
be discovered by timeout.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.b2b import errors
from app.b2b.canonical import CanonicalTxn
from app.b2b.detect import detect_format
from app.b2b.errors import ApiError
from app.b2b.ingest import IngestedFile
from app.b2b.parsers.registry import format_entry, get_parser
from app.b2b.rules import (
    FALLBACK_BUILTIN,
    METHOD_BUILTIN,
    METHOD_DEFAULT,
    METHOD_NONE,
    METHOD_RULE,
    Decision,
    RuleSet,
    classify_one,
)
from app.b2b.service import _path_matching_format

logger = logging.getLogger("b2b.classify")

#: How many distinct unmatched narrations to return. The point of these is to
#: let a caller write the next rule, which takes a handful of examples, not a
#: copy of the statement.
UNMATCHED_SAMPLE_LIMIT = 25


@dataclass
class ClassifyOutcome:
    payload: Dict[str, Any]
    detected_format: str
    transaction_count: int
    classified_count: int
    duration_ms: int
    warnings: List[str] = field(default_factory=list)


def _period(txns: List[CanonicalTxn]) -> Dict[str, Any]:
    dated = [t for t in txns if t.txn_date]
    if not dated:
        return {}
    return {"start": min(t.txn_date for t in dated).isoformat(),
            "end": max(t.txn_date for t in dated).isoformat()}


def _apply(txn: CanonicalTxn, decision: Decision) -> Optional[List[str]]:
    """Write a decision onto a canonical row. Returns any tags the rule set.

    Only `category`, `category_path` and the descriptive fields in
    `rules.SETTABLE_FIELDS` are touched. Amounts, dates, balance and direction
    are left exactly as the parser read them — see the whitelist argument in
    `app/b2b/rules.py`.
    """
    txn.category = decision.category
    txn.classification_method = decision.method
    txn.classification_rule = decision.rule_id

    # The shared parser path runs the LEGACY rule/ML engine on the way through
    # and leaves its confidence on the row (app/b2b/parsers/base.py). That
    # number describes a category the caller's rule has just replaced, and
    # `to_api()` publishes the field — so left alone it renders as a confidence
    # in a verdict that is no longer there. It is replaced here, not cleared
    # conditionally: a caller rule is a deterministic assertion and carries no
    # probability, so None is the honest value, and only the built-in
    # classifier has a real one to report.
    txn.category_confidence = decision.confidence
    # A row nobody could classify is flagged for review, which is the same
    # signal the internal ledger uses for an abstention. OR-ed rather than
    # assigned: the parser sets this flag when it could not determine the row's
    # direction, and that is a reason to review the row which has nothing to do
    # with its category. Assigning would have hidden a parse problem behind a
    # successful classification.
    txn.requires_review = bool(txn.requires_review) or decision.method in (
        METHOD_NONE, METHOD_DEFAULT)

    tags: Optional[List[str]] = None
    for key, value in (decision.assign or {}).items():
        if key == "tags":
            tags = list(value)
            continue
        setattr(txn, key, value)

    # A rule that named a category but no path still gets a usable path, so a
    # caller grouping on `category_path` does not see nulls for half its rows.
    if decision.category and not txn.category_path:
        txn.category_path = decision.category
    return tags


def _builtin_decision(txn: CanonicalTxn) -> Decision:
    """Our own classifier, used only when the caller asked for it.

    Imported lazily: it pulls in the rule config, the taxonomy and the ML
    service, and a caller who did not ask for the fallback should not pay that
    import cost.

    The category returned is from OUR taxonomy, not the caller's, so `method` is
    reported as `builtin` to make the vocabulary switch visible in the response
    rather than leaving the caller to notice unfamiliar names.
    """
    from app.categorization.hybrid import classify_transaction
    from app.categorization.taxonomy import UNCATEGORIZED

    amount = (txn.amount_paise or 0) / 100.0
    result = classify_transaction(
        narration=txn.narration_raw or txn.narration_clean or "",
        amount=amount,
        direction=(txn.direction or "").upper() or None,
    )
    if not result.category or result.category == UNCATEGORIZED:
        return Decision(
            category=None, method=METHOD_NONE,
            explanation="No rule matched and the built-in classifier abstained.")
    return Decision(
        category=result.category,
        method=METHOD_BUILTIN,
        rule_id=result.classification_rule,
        confidence=result.classification_confidence,
        explanation=(f"No caller rule matched; the built-in classifier assigned "
                     f"'{result.category}' "
                     f"(method={result.classification_method}, "
                     f"confidence={result.classification_confidence:.2f}). "
                     f"This name is from the built-in taxonomy, not your ruleset."),
    )


def run_classification(ingested: IngestedFile,
                       ruleset: RuleSet,
                       *,
                       request_id: str,
                       password: Optional[str] = None,
                       currency: str = "INR",
                       include_transactions: bool = True,
                       include_unmatched_samples: bool = True) -> ClassifyOutcome:
    started = time.monotonic()

    # -- 1. what is this file ---------------------------------------------
    detected = ingested.detected or detect_format(
        ingested.filename, ingested.head_bytes, ingested.path)

    if not detected.is_supported:
        entry = format_entry(detected.format)
        reason = (entry or {}).get("reason") if entry else None
        raise ApiError(
            errors.UNSUPPORTED_FILE_FORMAT,
            reason or f"Files of type '{detected.format}' are not supported.",
            detail={"detected_format": detected.format,
                    "filename": ingested.filename})

    parser = get_parser(detected.format)

    # Detection wins over the extension, exactly as in `service.py`: the legacy
    # pipeline dispatches on the suffix, so the suffix is corrected to agree
    # with what the content actually is.
    parse_path = _path_matching_format(ingested.path, detected)

    # -- 2. parse ---------------------------------------------------------
    try:
        parsed = parser(parse_path, password=password, currency=currency)
    except ApiError:
        raise
    except Exception as exc:                        # noqa: BLE001
        logger.exception("[%s] parser %s failed", request_id, detected.format)
        raise ApiError(
            errors.PARSE_FAILED,
            "The file could not be parsed. It may be corrupt, or its layout may "
            "not be one this service recognises.",
            detail={"detected_format": detected.format}) from exc

    txns: List[CanonicalTxn] = list(parsed.transactions)
    if not txns:
        raise ApiError(
            errors.NO_TRANSACTIONS_FOUND,
            "The file was read successfully but contained no transaction rows.",
            detail={"detected_format": detected.format})

    # -- 3. classify ------------------------------------------------------
    want_builtin = ruleset.fallback == FALLBACK_BUILTIN

    by_category: Counter = Counter()
    by_method: Counter = Counter()
    rule_hits: Counter = Counter()
    ambiguous: List[Dict[str, Any]] = []
    unmatched_samples: List[str] = []
    unmatched_seen: set = set()
    rows: List[Dict[str, Any]] = []
    classified = 0

    for txn in txns:
        decision = classify_one(ruleset, txn)

        if decision.method == METHOD_NONE and want_builtin:
            # The caller's rules had no opinion, so ours are consulted. A
            # `default_category` is applied only if this also abstains, which is
            # why the built-in attempt happens before the default is considered.
            decision = _builtin_decision(txn)
            if decision.method == METHOD_NONE and ruleset.default_category:
                decision = Decision(
                    category=ruleset.default_category, method=METHOD_DEFAULT,
                    explanation="No rule matched and the built-in classifier "
                                "abstained; 'default_category' applied.")

        tags = _apply(txn, decision)

        if decision.method == METHOD_RULE:
            rule_hits[decision.rule_id] += 1
        if decision.category:
            classified += 1
            by_category[decision.category] += 1
        by_method[decision.method] += 1

        if decision.ambiguous:
            ambiguous.append({
                "row_index": txn.row_index,
                "description": txn.narration_raw,
                "chosen_rule_id": decision.rule_id,
                "runner_up_rule_id": decision.runner_up_rule_id,
                "priority": decision.priority,
            })

        if decision.method in (METHOD_NONE, METHOD_DEFAULT):
            narration = (txn.narration_raw or "").strip()
            key = narration.upper()[:160]
            if narration and key not in unmatched_seen:
                unmatched_seen.add(key)
                if len(unmatched_samples) < UNMATCHED_SAMPLE_LIMIT:
                    unmatched_samples.append(narration)

        if include_transactions:
            row = txn.to_api()
            row["classification"] = decision.to_api()
            if tags:
                row["tags"] = tags
            rows.append(row)

    # -- 4. assemble ------------------------------------------------------
    #
    # `rule_usage` lists EVERY rule, including the ones that never fired. A
    # caller iterating on a ruleset needs to see the zeroes: a rule that matches
    # nothing is usually a typo in a term, and it is invisible in output that
    # only reports what did match.
    rule_usage = [{"rule_id": r.rule_id, "category": r.category,
                   "priority": r.priority, "matched": int(rule_hits.get(r.rule_id, 0))}
                  for r in ruleset.rules]
    rule_usage.sort(key=lambda e: (-e["matched"], e["rule_id"]))

    statement_meta = dict(parsed.statement_meta or {})
    statement_meta.setdefault("period", _period(txns))
    statement_meta["source_format"] = detected.format
    statement_meta["filename"] = ingested.filename

    warning_codes = sorted({code for code, _ in (parsed.parse_warnings or [])})

    summary: Dict[str, Any] = {
        "transaction_count": len(txns),
        "classified": classified,
        "unclassified": len(txns) - classified,
        "coverage": round(classified / len(txns), 4) if txns else 0.0,
        "by_category": dict(by_category.most_common()),
        "by_method": dict(by_method.most_common()),
        "rule_count": len(ruleset.rules),
        "rules_that_matched": sum(1 for e in rule_usage if e["matched"]),
        "rules_that_never_matched": [e["rule_id"] for e in rule_usage
                                     if not e["matched"]],
        "rule_usage": rule_usage,
        "ambiguous_count": len(ambiguous),
    }
    if ambiguous:
        # Capped: a ruleset with a systemic priority collision would otherwise
        # return one entry per row.
        summary["ambiguous"] = ambiguous[:UNMATCHED_SAMPLE_LIMIT]
    if include_unmatched_samples and unmatched_samples:
        summary["unmatched_samples"] = unmatched_samples

    payload: Dict[str, Any] = {
        "data": {
            "statement": statement_meta,
            "transactions": rows,
        },
        "summary": summary,
        "quality": {
            "continuity_pass_rate": parsed.continuity_pass_rate,
            "continuity_passed": parsed.continuity_passed,
            "rows_checked_for_continuity": parsed.rows_checked_for_continuity,
            "warnings": warning_codes,
        },
    }
    if not include_transactions:
        payload["data"]["transactions_omitted"] = True

    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info("[%s] classified %d/%d rows from %s using %d rules in %dms",
                request_id, classified, len(txns), detected.format,
                len(ruleset.rules), duration_ms)

    return ClassifyOutcome(
        payload=payload,
        detected_format=detected.format,
        transaction_count=len(txns),
        classified_count=classified,
        duration_ms=duration_ms,
        warnings=warning_codes,
    )


__all__ = ["ClassifyOutcome", "run_classification", "UNMATCHED_SAMPLE_LIMIT"]
