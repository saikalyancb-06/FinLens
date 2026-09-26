"""Adapter over the production parsing pipeline: PDF, XLSX, XLS, comma CSV.

Nothing here re-implements parsing. `TransactionParsingPipeline` already knows
how to pull a table out of a bank PDF, fall back to text layout when the table
extraction fails, read a multi-sheet workbook, and recover an HTML table that a
bank saved with an `.xls` extension. Rewriting any of that for the B2B API
would mean two parsers drifting apart, and the one behind the API would be the
less-tested of the two.

What this module adds is the honesty layer.

The pipeline reports `continuity_pass_rate = 1.0` for a statement with no
balance column, because the validator divides by `max(1, checked_rows)`. A
client reading that sees a perfectly reconciled statement where in fact nothing
was reconciled. This adapter re-derives how many row pairs were genuinely
checkable and, when that is zero, reports `None` for both continuity fields and
raises `CONTINUITY_UNVERIFIABLE`. That is the single most important thing this
file does.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

from app.b2b.errors import (
    ApiError,
    FILE_CORRUPT,
    NO_TRANSACTIONS_FOUND,
    PDF_PASSWORD_INVALID,
    PDF_PASSWORD_REQUIRED,
    UNSUPPORTED_FILE_FORMAT,
)
from app.b2b.metrics import (
    W_BALANCE_CONTINUITY_FAILED,
    W_CONTINUITY_UNVERIFIABLE,
    W_OCR_USED,
)
from app.b2b.parsers.base import (
    ParseOutput,
    apply_row_quality_warnings,
    count_continuity_checkable,
    observed_period,
    require_readable_file,
    rows_to_canonical,
    set_balance,
)

logger = logging.getLogger(__name__)

#: Extensions the pipeline dispatches on, mapped to the B2B format id.
FORMAT_BY_EXTENSION = {
    ".pdf": "pdf",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".xls": "xls",
    ".csv": "csv",
}

_pipeline = None
_pipeline_lock = threading.Lock()


def get_pipeline():
    """One pipeline per process, built on first use.

    Constructing it loads the TF-IDF vectoriser and the logistic-regression
    classifier from disk — about a second, and pure waste if the request turns
    out to be an OFX file. Guarded by a lock because a threaded server can
    otherwise build several at once during a cold burst.
    """
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                from app.parsers.pipeline import TransactionParsingPipeline
                _pipeline = TransactionParsingPipeline()
    return _pipeline


def _password_error(exc: Exception, password: Optional[str]) -> Optional[ApiError]:
    """Turn a PDF library's encryption complaint into a code a client can act on."""
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = ("password", "encrypt", "decrypt", "pdfpasswordincorrect")
    if not any(marker in text for marker in markers):
        return None
    if password:
        return ApiError(PDF_PASSWORD_INVALID,
                        "the supplied password did not open this PDF")
    return ApiError(PDF_PASSWORD_REQUIRED,
                    "this PDF is password-protected; resend it with the 'password' field")


def _require_xls_engine() -> None:
    """`.xls` needs xlrd; without it pandas silently falls through to zero rows.

    Reported as UNSUPPORTED_FILE_FORMAT rather than a 500 because the client
    can act on it — resaving as `.xlsx` works immediately — and because from
    the caller's side the format genuinely is not available on this deployment.
    """
    try:
        import xlrd  # noqa: F401
    except ImportError:
        raise ApiError(
            UNSUPPORTED_FILE_FORMAT,
            "legacy .xls files are not supported by this deployment "
            "(the xlrd engine is not installed); please send .xlsx or CSV",
        )


def parse(path: str, *, password: Optional[str] = None,
          currency: str = "INR") -> ParseOutput:
    """Parse a PDF, workbook or comma CSV through the production pipeline."""
    require_readable_file(path)
    ext = os.path.splitext(path)[1].lower()
    source_format = FORMAT_BY_EXTENSION.get(ext, "unknown")
    if ext == ".xls":
        _require_xls_engine()

    try:
        result: Dict[str, Any] = get_pipeline().process_file_with_validation(
            path, pdf_password=password)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see below
        # The pipeline surfaces failures from four different libraries
        # (pdfplumber, PyMuPDF, openpyxl, pandas) with no shared exception
        # type. Encryption is the one case a client can fix, so it is
        # separated out; everything else becomes FILE_CORRUPT with the
        # library's own words dropped, not forwarded.
        pw_error = _password_error(exc, password)
        if pw_error:
            raise pw_error
        logger.warning("[b2b.legacy] parse failed for %s: %s", os.path.basename(path), exc)
        raise ApiError(FILE_CORRUPT,
                       "the file could not be parsed as a bank statement; "
                       "it may be damaged or in an unexpected layout")

    rows = result.get("transactions") or []
    transactions, dropped = rows_to_canonical(
        rows, source_format=source_format, currency=currency)
    if not transactions:
        raise ApiError(
            NO_TRANSACTIONS_FOUND,
            "the file was read successfully but no transactions could be extracted",
        )

    output = ParseOutput(transactions=transactions)

    # ---- continuity, told straight ----------------------------------------
    checked = count_continuity_checkable(rows)
    output.rows_checked_for_continuity = checked
    if checked == 0:
        output.continuity_pass_rate = None
        output.continuity_passed = None
        output.warn(
            W_CONTINUITY_UNVERIFIABLE,
            "no row pair carried a usable running balance, so balance "
            "continuity could not be checked; the pipeline's 1.00 pass rate "
            "for this statement reflects zero checks, not zero failures",
        )
    else:
        output.continuity_pass_rate = result.get("continuity_pass_rate")
        output.continuity_passed = result.get("continuity_passed")
        if output.continuity_passed is False:
            rate = output.continuity_pass_rate or 0.0
            output.warn(
                W_BALANCE_CONTINUITY_FAILED,
                f"balance continuity held on only {rate:.0%} of {checked} "
                f"checked row pair(s)",
            )

    apply_row_quality_warnings(output, rows,
                               rejected=int(result.get("total_rejected") or 0),
                               dropped_undatable=dropped)

    if any(str(r.get("source_method", "")).lower().startswith("ocr") for r in rows):
        output.warn(W_OCR_USED,
                    "some rows were recovered by OCR; character-level errors in "
                    "narrations and amounts are possible")

    # ---- statement metadata -----------------------------------------------
    meta: Dict[str, Any] = {"currency": currency}
    meta.update(observed_period(transactions))

    reported_closing = (result.get("statement_meta") or {}).get("closing_balance")
    if reported_closing is not None:
        # Printed in the statement footer: extracted, not derived.
        set_balance(meta, "closing_balance", reported_closing, basis="reported")
    else:
        last_balance = transactions[-1].balance_paise
        if last_balance is not None:
            set_balance(meta, "closing_balance", last_balance / 100.0,
                        basis="last_row_balance")

    output.statement_meta = meta
    return output
