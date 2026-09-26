"""The one function that turns an uploaded file into an analysis.

Kept out of the router deliberately: the HTTP layer should decide status codes
and headers, not know how a statement becomes a number. The async worker and
the synchronous path both call `run_analysis`, so there is exactly one
implementation of the pipeline and no chance of the two drifting.

Sequence — detect, parse, enrich, analyse — with every stage allowed to fail
loudly. Nothing here writes a transaction to the database: the whole point of
the B2B service is that a client's financial data is processed and returned,
not accumulated.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.b2b import errors
from app.b2b.canonical import CanonicalTxn
from app.b2b.detect import detect_format
from app.b2b.errors import ApiError
from app.b2b.ingest import IngestedFile
from app.b2b.parsers.registry import format_entry, get_parser

logger = logging.getLogger("b2b.service")


@dataclass
class AnalysisOutcome:
    payload: Dict[str, Any]
    detected_format: str
    transaction_count: int
    overall_confidence: Optional[float]
    duration_ms: int


def _period_from(txns: List[CanonicalTxn]) -> Dict[str, Any]:
    dated = [t for t in txns if t.txn_date]
    if not dated:
        return {}
    return {
        "start": min(t.txn_date for t in dated).isoformat(),
        "end": max(t.txn_date for t in dated).isoformat(),
    }


#: Suffix each detected format must carry on disk for the legacy pipeline's
#: extension-based dispatch to route it correctly.
_EXTENSION_FOR_FORMAT = {
    "pdf": ".pdf", "xlsx": ".xlsx", "xls": ".xls", "csv": ".csv",
    "tsv": ".tsv", "txt": ".txt", "json": ".json",
    "ofx": ".ofx", "qfx": ".qfx", "xml_camt": ".xml",
}


def _path_matching_format(path: str, detected) -> str:
    """Return a path whose extension agrees with the detected format.

    Links rather than copies where the filesystem allows it, so a 50 MB upload
    is not duplicated on disk to correct a suffix. The link lands beside the
    original inside the same per-request temp directory, so the existing
    cleanup removes both.
    """
    wanted = _EXTENSION_FOR_FORMAT.get(detected.format)
    if not wanted or path.lower().endswith(wanted):
        return path

    aligned = os.path.splitext(path)[0] + wanted
    if os.path.exists(aligned):
        return aligned
    try:
        os.link(path, aligned)
    except OSError:
        # Cross-device, or a filesystem without hard links. Copying is the
        # fallback rather than the default because of the size.
        import shutil
        shutil.copy2(path, aligned)
    return aligned


def run_analysis(ingested: IngestedFile,
                 *,
                 request_id: str,
                 password: Optional[str] = None,
                 country: str = "IN",
                 currency: str = "INR",
                 loan: Optional[Dict[str, Any]] = None,
                 include_transactions: bool = True,
                 config: Optional[Dict[str, Any]] = None) -> AnalysisOutcome:
    started = time.monotonic()

    # -- 1. what is this file, really ------------------------------------
    detected = ingested.detected or detect_format(
        ingested.filename, ingested.head_bytes, ingested.path)

    if not detected.is_supported:
        entry = format_entry(detected.format)
        reason = (entry or {}).get("reason") if entry else None
        raise ApiError(
            errors.UNSUPPORTED_FILE_FORMAT,
            reason or f"Files of type '{detected.format}' are not supported.",
            detail={"detected_format": detected.format,
                    "filename": ingested.filename},
        )

    parser = get_parser(detected.format)

    # -- 2. parse to the canonical shape ---------------------------------
    # The legacy pipeline dispatches on the file's EXTENSION, not on what we
    # detected. A CSV uploaded as `statement.pdf` would therefore be handed to
    # the PDF parser and fail, despite detection having correctly identified
    # it — the extension would silently win after all the work of not trusting
    # it. So the parser is given a path whose suffix matches the detected
    # format; content stays the deciding vote all the way down.
    parse_path = _path_matching_format(ingested.path, detected)

    try:
        parsed = parser(parse_path, password=password, currency=currency)
    except ApiError:
        raise
    except Exception as exc:                      # noqa: BLE001
        # The exception text can name a library, a path or a byte offset. The
        # client gets the request id; the detail goes to our log only.
        logger.exception("[%s] parser %s failed", request_id, detected.format)
        raise ApiError(
            errors.PARSE_FAILED,
            "The file could not be parsed. It may be corrupt, or its layout "
            "may not be one this service recognises.",
            detail={"detected_format": detected.format},
        ) from exc

    txns: List[CanonicalTxn] = list(parsed.transactions)
    if not txns:
        raise ApiError(
            errors.NO_TRANSACTIONS_FOUND,
            "The file was read successfully but contained no transaction rows.",
            detail={"detected_format": detected.format},
        )

    # -- 3. analyse (stateless; see app/b2b/analysis/engine.py) ----------
    from app.b2b.analysis import analyze          # deferred: pulls in the ML stack

    statement_meta = dict(parsed.statement_meta or {})
    statement_meta.setdefault("period", _period_from(txns))
    statement_meta["source_format"] = detected.format
    statement_meta["filename"] = ingested.filename

    parse_quality = {
        "continuity_pass_rate": parsed.continuity_pass_rate,
        "continuity_passed": parsed.continuity_passed,
        "rows_checked_for_continuity": parsed.rows_checked_for_continuity,
        "warnings": list(parsed.parse_warnings or []),
    }

    try:
        result = analyze(txns,
                         statement_meta=statement_meta,
                         parse_quality=parse_quality,
                         country=country,
                         currency=currency,
                         loan=loan,
                         config=config)
    except ApiError:
        raise
    except Exception as exc:                      # noqa: BLE001
        logger.exception("[%s] analysis failed", request_id)
        raise ApiError(
            errors.ANALYSIS_FAILED,
            "The file was parsed but the analysis could not be completed.",
        ) from exc

    payload = result.to_api()

    # Transactions are opt-out: a lender scoring an application wants the rows,
    # a dashboard polling for aggregates does not, and the row list dominates
    # the response size.
    if not include_transactions:
        payload["data"] = dict(payload["data"])
        payload["data"]["transactions"] = []
        payload["data"]["transactions_omitted"] = True

    duration_ms = int((time.monotonic() - started) * 1000)
    confidence = (payload.get("quality") or {}).get("overall_confidence")

    return AnalysisOutcome(
        payload=payload,
        detected_format=detected.format,
        transaction_count=len(txns),
        overall_confidence=confidence,
        duration_ms=duration_ms,
    )
