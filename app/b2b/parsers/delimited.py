"""Delimited text: TSV, TXT, and CSV that is not comma-separated.

Comma CSV already has a working path through `ExcelCSVParser`, so this module
exists for the three cases that path silently mishandles: a tab-separated
export, a European semicolon CSV, and a pipe-delimited dump. `csv.reader` with
the wrong delimiter does not fail — it returns one wide column per line, no
header maps, and the file parses to zero transactions with no error to explain
why. Sniffing the delimiter first is the whole fix.

Everything after the delimiter decision is deliberately the *same* code the CSV
path uses: `map_headers` for the ~130 column aliases, `clean_amount_string` for
lakh-crore grouping and CR/DR suffixes, `build_normalized_transaction` for
direction resolution, then `TransactionValidator` for row validation and
balance continuity. A semicolon CSV and its comma equivalent therefore produce
identical rows, which `test_semicolon_matches_comma_equivalent` asserts
directly rather than trusting the claim.

The decision engine is *not* run here. It is a classifier, not an ingest step,
and the B2B analysis layer does its own categorisation.
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.b2b.errors import ApiError, NO_TRANSACTIONS_FOUND, PARSE_FAILED
from app.b2b.metrics import (
    W_BALANCE_CONTINUITY_FAILED,
    W_CONTINUITY_UNVERIFIABLE,
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
from app.parsers.normalizer import (
    build_normalized_transaction,
    clean_amount_string,
    clean_ocr_text,
    map_headers,
)

logger = logging.getLogger(__name__)

#: Delimiters worth considering. Anything else in a bank export is a quoting
#: accident rather than a format.
CANDIDATE_DELIMITERS = ",;\t|"

#: How far down the file a header row may hide. Bank exports routinely open
#: with a dozen lines of account preamble.
MAX_HEADER_SCAN_ROWS = 100

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


# ------------------------------------------------------------------ delimiter

def sniff_delimiter(sample: str) -> Tuple[Optional[str], float, str]:
    """Guess the delimiter of a text sample.

    Returns `(delimiter, confidence, reason)`; a `None` delimiter means the
    text has no consistent tabular structure at all, which is how
    `detect_format` tells a TXT statement dump from a delimited table.

    `csv.Sniffer` is tried first and is right nearly always. Its failure mode
    is a small or ragged sample, where it raises rather than guessing, so a
    column-count fallback follows: the winning delimiter is the one appearing
    at least twice on every one of the first few non-empty lines with a stable
    count, which is what a header plus data rows looks like.
    """
    lines = [ln for ln in sample.splitlines() if ln.strip()][:20]
    if not lines:
        return None, 0.0, "no non-empty lines"
    probe = "\n".join(lines)

    try:
        dialect = csv.Sniffer().sniff(probe, delimiters=CANDIDATE_DELIMITERS)
        delimiter = dialect.delimiter
        if delimiter in CANDIDATE_DELIMITERS:
            return delimiter, 0.9, "csv.Sniffer"
    except csv.Error:
        pass

    best: Optional[str] = None
    best_count = 0
    for candidate in CANDIDATE_DELIMITERS:
        counts = [ln.count(candidate) for ln in lines[:6]]
        if min(counts) >= 2 and (max(counts) - min(counts)) <= 1:
            if min(counts) > best_count:
                best, best_count = candidate, min(counts)
    if best:
        return best, 0.7, f"consistent column counts on {len(lines[:6])} leading lines"

    # A two-column file (one delimiter per line) is still a table.
    for candidate in CANDIDATE_DELIMITERS:
        counts = [ln.count(candidate) for ln in lines[:6]]
        if min(counts) == 1 and max(counts) == 1:
            return candidate, 0.5, "single consistent separator per line"

    return None, 0.2, "no delimiter appears consistently"


def _read_text(path: str) -> str:
    for enc in _ENCODINGS:
        try:
            with open(path, "r", encoding=enc, newline="") as handle:
                return handle.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
        except OSError as exc:
            raise ApiError(PARSE_FAILED, f"the uploaded file could not be read: {exc}")
    # latin-1 cannot fail, so reaching here means every open() raised.
    raise ApiError(PARSE_FAILED, "the uploaded file is not decodable as text")


# ------------------------------------------------------- grid -> normalizer rows

def find_header(grid: Sequence[Sequence[str]]) -> Tuple[int, Dict[str, int]]:
    """Locate the header row and its column mapping.

    A row qualifies as the header when it yields a date column, or a
    description column paired with any money column — the same test the CSV
    path applies, so the two agree on files that contain a preamble.
    """
    for idx, raw_row in enumerate(grid[:MAX_HEADER_SCAN_ROWS]):
        cells = [str(cell).strip() for cell in raw_row]
        mapping = map_headers(cells)
        if "date" in mapping or (
            "description" in mapping
            and ("amount" in mapping or "debit" in mapping or "credit" in mapping)
        ):
            return idx, mapping
    return -1, {}


def grid_to_normalized_rows(grid: Sequence[Sequence[str]],
                            *,
                            source_method: str) -> List[Dict[str, Any]]:
    """Header-mapped rows of raw cells -> normalizer transaction dicts.

    Shared with `jsonfmt`, which flattens objects into exactly this shape so a
    JSON statement inherits the same aliases and the same numeric guards as a
    spreadsheet.
    """
    header_idx, col_map = find_header(grid)
    if not col_map:
        raise ApiError(
            PARSE_FAILED,
            "no transaction header row was found; expected columns such as "
            "date, description and debit/credit or amount",
        )

    def cell(row: Sequence[str], field_name: str) -> str:
        idx = col_map.get(field_name, -1)
        if 0 <= idx < len(row):
            return clean_ocr_text(str(row[idx]))
        return ""

    rows: List[Dict[str, Any]] = []
    for raw_row in grid[header_idx + 1:]:
        cells = [str(c).strip() for c in raw_row]
        raw_line = " | ".join(c for c in cells if c)
        if not raw_line:
            continue

        date_val = cell(cells, "date")
        desc_val = cell(cells, "description")
        ref_val = cell(cells, "reference_number")
        debit_val, debit_ind = clean_amount_string(cell(cells, "debit"))
        credit_val, credit_ind = clean_amount_string(cell(cells, "credit"))
        amount_val, amount_ind = clean_amount_string(cell(cells, "amount"))
        balance_val, _ = clean_amount_string(cell(cells, "balance"))

        # Continuation row: a narration that wrapped onto its own line. Merged
        # upward exactly as the CSV path does, so a wrapped description does
        # not become a phantom zero-amount transaction.
        if not date_val and debit_val == 0.0 and credit_val == 0.0 and amount_val == 0.0:
            if desc_val and rows:
                rows[-1]["description"] += " " + desc_val
                rows[-1]["raw_text"] += " | " + raw_line
            continue

        type_val = cell(cells, "transaction_type")
        indicator = debit_ind or credit_ind or amount_ind or type_val or ""
        rows.append(build_normalized_transaction(
            date=date_val,
            description=desc_val,
            debit=debit_val,
            credit=credit_val,
            amount=amount_val,
            balance=balance_val,
            transaction_type=indicator,
            reference_number=ref_val,
            raw_text=raw_line,
            source_page=1,
            source_method=source_method,
        ))

    for i, row in enumerate(rows):
        row["row_index"] = i
    return rows


def validated_output(rows: List[Dict[str, Any]],
                     *,
                     source_format: str,
                     currency: str) -> ParseOutput:
    """Run the shared validator over normalizer rows and build a ParseOutput.

    Imported lazily inside the function: `TransactionValidator` pulls in the
    rest of `app.parsers`, and a JSON or OFX request should not pay for that
    import until it is actually needed.
    """
    from app.parsers.validator import TransactionValidator

    result = TransactionValidator().validate(rows)
    clean_rows = result.transactions

    transactions, dropped = rows_to_canonical(
        clean_rows, source_format=source_format, currency=currency)
    if not transactions:
        raise ApiError(NO_TRANSACTIONS_FOUND,
                       "the file was read successfully but contained no transactions")

    output = ParseOutput(transactions=transactions)
    checked = count_continuity_checkable(clean_rows)
    output.rows_checked_for_continuity = checked
    if checked == 0:
        # See base.ParseOutput: the validator would report 1.0 here.
        output.continuity_pass_rate = None
        output.continuity_passed = None
        output.warn(W_CONTINUITY_UNVERIFIABLE,
                    "no row pair carried a running balance, so balance "
                    "continuity could not be checked on this statement")
    else:
        output.continuity_pass_rate = result.continuity_pass_rate
        output.continuity_passed = result.continuity_passed
        if not result.continuity_passed:
            output.warn(
                W_BALANCE_CONTINUITY_FAILED,
                f"balance continuity held on only "
                f"{result.continuity_pass_rate:.0%} of {checked} checked row pair(s)",
            )

    apply_row_quality_warnings(output, clean_rows,
                               rejected=len(result.rejected),
                               dropped_undatable=dropped)

    meta: Dict[str, Any] = {"currency": currency}
    meta.update(observed_period(transactions))
    last_balance = transactions[-1].balance_paise
    if last_balance is not None:
        set_balance(meta, "closing_balance", last_balance / 100.0,
                    basis="last_row_balance")
    output.statement_meta = meta
    return output


# ------------------------------------------------------------------- entrypoint

def parse(path: str, *, password: Optional[str] = None,
          currency: str = "INR") -> ParseOutput:
    """Parse a delimited text statement. `password` is accepted and ignored."""
    require_readable_file(path)
    text = _read_text(path)

    delimiter, confidence, reason = sniff_delimiter(text)
    if delimiter is None:
        # Fall through on a comma rather than refusing outright: a one-column
        # file still gets a fair attempt at a header, and `find_header` gives a
        # better error than "no delimiter" if there is nothing tabular here.
        delimiter = ","
        logger.debug("[b2b.delimited] no delimiter sniffed (%s); defaulting to comma", reason)

    try:
        grid = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error as exc:
        raise ApiError(PARSE_FAILED, f"the delimited file could not be read: {exc}")

    if not grid:
        raise ApiError(NO_TRANSACTIONS_FOUND, "the file contained no rows")

    source_format = {"\t": "tsv", ";": "csv_semicolon", "|": "csv_pipe"}.get(delimiter, "csv")
    rows = grid_to_normalized_rows(grid, source_method="delimited")
    output = validated_output(rows, source_format=source_format, currency=currency)
    output.statement_meta["delimiter"] = delimiter
    output.statement_meta["delimiter_confidence"] = round(confidence, 2)
    return output
