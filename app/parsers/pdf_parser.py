"""
pdf_parser.py  —  Production-grade PDF transaction extractor.

Fixes applied (vs original):
  BUG-003  Engine output guarded: debit/credit values from OfflineFinancialParserEngine
           are re-validated through clean_amount_string before use.
  BUG-005  _process_raw_text: positional number extraction (not greedy replace);
           integer amounts (no decimals) now captured.
  BUG-009  Description extraction uses positional slicing, not string.replace().
  BUG-012  Every transaction carries confidence, source_page, source_method.
  NEW      Structured logging at every stage.
  NEW      Page-level and method-level metadata propagated.
  NEW      Fallback chain: engine → pdfplumber table → pdfplumber text → OCR.
"""

import re
import os
import sys
import logging
import tempfile
from typing import List, Dict, Any, Optional, Tuple

import pdfplumber
import fitz  # PyMuPDF

from app.parsers.base import BaseParser
from app.parsers.pdf_detector import is_digital_pdf
from app.parsers.ocr_engine import perform_ocr_on_pdf
from app.parsers.normalizer import (
    map_headers,
    clean_amount_string,
    build_normalized_transaction,
    normalize_date_string,
    clean_ocr_text,
    DATE_PATTERNS,
)

logger = logging.getLogger(__name__)

# ── mlmodel path ──────────────────────────────────────────────────────────────
_MLMODEL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "mlmodel"))
if _MLMODEL_DIR not in sys.path:
    sys.path.append(_MLMODEL_DIR)

try:
    from financial_parser.engine import OfflineFinancialParserEngine
    _ENGINE_AVAILABLE = True
except ImportError:
    _ENGINE_AVAILABLE = False
    logger.warning("[PDFParser] OfflineFinancialParserEngine not available — will use fallback parsers")

# ── Amount number regex ───────────────────────────────────────────────────────
# Matches: 1,23,456.78  OR  50000  OR  50,000  (with optional 2-decimal suffix)
_AMOUNT_RE = re.compile(r'-?(?:\d{1,3}(?:,\d{2,3})*|\d+)(?:\.\d{1,2})?')

# Statement summary lines — "Closing Balance as on 30-Nov-2026   70,000.00" and
# friends. They carry a date and an amount, so the line parser used to ingest
# them as transactions: a phantom row whose "amount" is the whole closing
# balance, which then inflated the movement total and broke balance continuity.
# They are statement METADATA (see `_extract_statement_footer`), never rows.
_CLOSING_LABEL_RE = re.compile(
    r'\b(?:'
    r'statement\s+closing\s+balance|closing\s+balance|'
    r'balance\s+at\s+the\s+end(?:\s+of\s+statement)?|'
    r'balance\s+carried\s+(?:forward|down)'
    r')\b(?:\s+as\s+on)?[:\s]*',
    re.IGNORECASE,
)
# CLOSING labels only, and the same ones both times: a line is skipped as a row
# exactly when it is the line the closing balance is read from. An "Opening
# Balance" / "Balance brought forward" line looks similar but is the anchor the
# running-balance inference starts from, so it must stay a row.


class PDFParser(BaseParser):

    def __init__(self):
        self._engine = OfflineFinancialParserEngine() if _ENGINE_AVAILABLE else None
        # Holds statement-level metadata extracted from the last parsed PDF
        self.last_statement_closing = None

    # ──────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ──────────────────────────────────────────────────────────────────────────

    def _orient_chronologically(self, transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(transactions) > 1:
            first_dt = str(transactions[0].get("date", ""))
            last_dt  = str(transactions[-1].get("date", ""))
            if first_dt and last_dt and first_dt > last_dt:
                logger.info(f"[PDFParser] Detected reverse chronological order ({first_dt} -> {last_dt}). Reversing to chronological order.")
                transactions = list(reversed(transactions))
        for i, t in enumerate(transactions):
            t["row_index"] = i
        return transactions

    def parse(self, file_path: str, password: Optional[str] = None) -> List[Dict[str, Any]]:
        logger.info(f"[PDFParser] Starting parse: {file_path} (Password Provided: {bool(password)})")
        transactions: List[Dict[str, Any]] = []

        # Statement metadata is per-file. The pipeline holds ONE PDFParser for its
        # lifetime, so without this reset a footer read from an earlier statement
        # is still hanging off the instance and gets reported as this statement's
        # closing balance — a footer-less statement inherits the previous one's.
        self.last_statement_closing = None

        # If the PDF is password-protected, decrypt it to a temp file so every
        # downstream strategy (engine, pdfplumber, OCR) works on a plain-text PDF.
        working_path = file_path
        _tmp_file = None
        if password:
            try:
                decrypted_path = self._decrypt_pdf_to_temp(file_path, password)
                if decrypted_path:
                    working_path = decrypted_path
                    _tmp_file = decrypted_path
                    logger.info(f"[PDFParser] Decrypted password-protected PDF to temp file for processing")
            except Exception as de:
                logger.warning(f"[PDFParser] Decryption to temp file failed ({de}), will pass password inline")

        # Pass password only when using the original (still-encrypted) path.
        _inline_pwd = None if working_path != file_path else password

        try:
            # An explicit closing balance printed on the statement is a fact about
            # the STATEMENT, not about any one row, so it is read up front — before
            # the extraction strategies, every one of which returns early on
            # success. Reading it after them (as this used to) meant the footer was
            # only ever captured on the OCR path: a statement parsed by the engine
            # or by raw-text line parsing silently lost its stated closing balance,
            # and reconciliation then fell back to the last row's running balance.
            try:
                footer_bal = self._extract_statement_footer(working_path, password=_inline_pwd)
                if footer_bal is not None:
                    self.last_statement_closing = footer_bal
                    logger.info(f"[PDFParser] Statement footer closing balance: {footer_bal}")
            except Exception:
                # Non-fatal: do not break parsing if footer extraction fails
                logger.debug("[PDFParser] Statement footer extraction failed or not present")

            # Strategy 1: OfflineFinancialParserEngine (best accuracy for digital PDFs)
            if self._engine is not None:
                try:
                    transactions = self._parse_with_engine(working_path)
                    if transactions:
                        logger.info(f"[PDFParser] Engine extracted {len(transactions)} rows")
                        return self._orient_chronologically(transactions)
                    logger.info("[PDFParser] Engine returned 0 rows — trying fallback")
                except Exception as e:
                    logger.warning(f"[PDFParser] Engine failed ({e}) — trying fallback")

            # Strategy 2: pdfplumber table extraction (good for digital PDFs with borders)
            try:
                transactions = self._parse_with_pdfplumber_tables(working_path, password=_inline_pwd)
                if transactions:
                    logger.info(f"[PDFParser] pdfplumber-tables extracted {len(transactions)} rows")
                    return self._orient_chronologically(transactions)
            except Exception as e:
                logger.warning(f"[PDFParser] pdfplumber-tables failed: {e}")

            # Strategy 3: pdfplumber raw-text line parsing
            try:
                transactions = self._parse_with_pdfplumber_text(working_path, password=_inline_pwd)
                if transactions:
                    logger.info(f"[PDFParser] pdfplumber-text extracted {len(transactions)} rows")
                    return self._orient_chronologically(transactions)
            except Exception as e:
                logger.warning(f"[PDFParser] pdfplumber-text failed: {e}")

            # Strategy 4: OCR (scanned PDFs)
            logger.info("[PDFParser] Falling back to OCR")
            try:
                transactions = self._parse_scanned_pdf(working_path)
                logger.info(f"[PDFParser] OCR extracted {len(transactions)} rows")
            except Exception as e:
                logger.error(f"[PDFParser] OCR also failed: {e}")

        finally:
            # Clean up the decrypted temp file
            if _tmp_file and os.path.exists(_tmp_file):
                try:
                    os.unlink(_tmp_file)
                except Exception:
                    pass

        return self._orient_chronologically(transactions)

    def _decrypt_pdf_to_temp(self, file_path: str, password: str) -> Optional[str]:
        """
        Opens a password-protected PDF with PyMuPDF, authenticates with the given
        password, and saves a decrypted (unencrypted) copy to a temporary file.

        Returns the path to the temp file, or None if decryption fails.
        The caller is responsible for deleting the temp file.
        """
        doc = fitz.open(file_path)
        try:
            if not doc.is_encrypted:
                return None  # Not encrypted; caller should use original file
            auth_res = doc.authenticate(password)
            if auth_res == 0:
                raise ValueError(f"Invalid password for PDF: {os.path.basename(file_path)}")
            # Save as a new unencrypted PDF
            suffix = os.path.splitext(file_path)[1] or ".pdf"
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            os.close(tmp_fd)
            doc.save(tmp_path, encryption=fitz.PDF_ENCRYPT_NONE)
            return tmp_path
        finally:
            doc.close()

    def _extract_statement_footer(self, file_path: str, password: Optional[str] = None) -> Optional[float]:
        """
        Scans the PDF text for explicit statement-level closing balance phrases and returns
        the numeric value (float) when found, otherwise None.

        This intentionally does NOT create a transaction — it only returns metadata.
        """
        text_accum = []
        open_kwargs = {"password": password} if password else {}
        with pdfplumber.open(file_path, **open_kwargs) as pdf:
            for page in pdf.pages:
                ptext = page.extract_text() or ""
                ptext = clean_ocr_text(ptext)
                text_accum.append(ptext)

        full_text = "\n".join(text_accum)

        # These used to be one regex per phrasing, each trying to capture the
        # amount itself with a character class. That never fired on a real
        # statement: "Closing Balance as on 30-Nov-2026  70,000.00" defeated
        # both halves of it — the date sub-pattern only allowed digits and
        # separators, so a month NAME failed it, and the fallback's greedy
        # `[^\n]*` backtracked into the amount and captured a bare trailing
        # "0", which parses to 0.0 and was then discarded as falsy. So the
        # label is matched here, and finding the amount is a separate step
        # that reuses the same tokeniser the row parser uses.
        from app.parsers.normalizer import clean_amount_string

        best: Optional[float] = None
        for raw_line in full_text.splitlines():
            line = raw_line.strip()
            if not line or not _CLOSING_LABEL_RE.search(line):
                continue

            # Everything after the label — a leading "01-Nov-2026" from a
            # period header is not this line's closing balance.
            tail = line[_CLOSING_LABEL_RE.search(line).end():]

            # Drop date spans so "30-Nov-2026" cannot be read as an amount.
            spans = []
            for pattern in DATE_PATTERNS:
                for dm in re.finditer(pattern, tail, re.IGNORECASE):
                    spans.append((dm.start(), dm.end()))

            candidates = [
                m for m in _AMOUNT_RE.finditer(tail)
                if not any(s <= m.start() < e for s, e in spans)
            ]

            # The labelled amount is the last number on the line: "Closing
            # Balance as on 31-Mar-2026 Rs. 65,000.00".
            for m in reversed(candidates):
                try:
                    val, _ = clean_amount_string(m.group(0))
                except Exception:
                    continue
                if val and val > 0:
                    best = float(val)
                    break

        # A multi-page statement repeats the label per page; the closing
        # balance is the one on the last page that carries it.
        return best

    # ──────────────────────────────────────────────────────────────────────────
    # Strategy 1: OfflineFinancialParserEngine
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_with_engine(self, file_path: str) -> List[Dict[str, Any]]:
        with open(file_path, "rb") as f:
            file_bytes = f.read()

        filename = os.path.basename(file_path)
        res = self._engine.process_document(file_bytes, filename)

        output: List[Dict[str, Any]] = []
        for tx in res.transactions:
            # BUG-003 fix: guard engine outputs through clean_amount_string
            raw_debit   = str(tx.debit)   if tx.debit   is not None else ""
            raw_credit  = str(tx.credit)  if tx.credit  is not None else ""
            raw_balance = str(tx.balance) if tx.balance is not None else ""

            w_val, w_ind = clean_amount_string(raw_debit)
            d_val, d_ind = clean_amount_string(raw_credit)
            b_val, _     = clean_amount_string(raw_balance)

            raw_date = str(tx.date) if tx.date else ""
            date_str = normalize_date_string(raw_date)

            # Resolve type from engine + indicators
            txn_type = ""
            if w_val > 0 and d_val == 0:
                txn_type = "debit"
            elif d_val > 0 and w_val == 0:
                txn_type = "credit"

            output.append(build_normalized_transaction(
                date=date_str,
                description=str(tx.description or "").strip(),
                debit=w_val,
                credit=d_val,
                amount=d_val if d_val > 0 else w_val,
                balance=b_val,
                transaction_type=txn_type,
                reference_number=str(tx.reference or "").strip(),
                raw_text=f"{date_str} | {tx.description} | {w_val} | {d_val} | {b_val}",
                source_method="engine",
                source_page=0,
            ))

        return output

    # ──────────────────────────────────────────────────────────────────────────
    # Strategy 2: pdfplumber table extraction
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_with_pdfplumber_tables(self, file_path: str, password: Optional[str] = None) -> List[Dict[str, Any]]:
        transactions: List[Dict[str, Any]] = []
        with pdfplumber.open(file_path, password=password) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                tables = page.extract_tables()
                for table in tables:
                    # Inspect small tables for explicit closing-balance labels (summary/footer tables)
                    try:
                        flat = [str(c).strip() if c is not None else "" for row in table for c in row]
                        joined = " ".join(flat).lower()
                        # Only a fallback. `_extract_statement_footer` has already run
                        # against the statement text and parses the labelled amount
                        # precisely; this scan just grabs the last positive number in
                        # any table mentioning a closing balance, so it must not
                        # overwrite the more precise reading.
                        if self.last_statement_closing is None and (
                            "closing balance" in joined
                            or "statement closing" in joined
                            or "balance at the end" in joined
                        ):
                            # find the first numeric token in the table likely to be the amount
                            from app.parsers.normalizer import clean_amount_string
                            for cell in flat[::-1]:
                                val, _ = clean_amount_string(cell)
                                if val and val > 0:
                                    self.last_statement_closing = float(val)
                                    break
                    except Exception:
                        pass
                    if not table or len(table) < 2:
                        continue
                    txns = self._process_table_matrix(table, page_num=page_num)
                    transactions.extend(txns)
        return transactions

    def _process_table_matrix(
        self, table: List[List[Any]], page_num: int = 0
    ) -> List[Dict[str, Any]]:
        transactions: List[Dict[str, Any]] = []
        header_idx = -1
        col_map: Dict[str, int] = {}

        # Find header row in first 15 rows
        for i, row in enumerate(table[:15]):
            row_str = [str(c).strip() if c is not None else "" for c in row]
            mapping = map_headers(row_str)
            if "date" in mapping or (
                "description" in mapping
                and ("amount" in mapping or "debit" in mapping or "credit" in mapping)
            ):
                header_idx = i
                col_map = mapping
                break

        if header_idx == -1:
            return transactions

        prev_description = ""
        for row in table[header_idx + 1:]:
            if not row or not any(row):
                continue

            row_str = [str(c).strip() if c is not None else "" for c in row]
            raw_line = " | ".join(s for s in row_str if s)

            if not raw_line:
                continue

            def _get(field: str) -> str:
                idx = col_map.get(field, -1)
                return row_str[idx] if 0 <= idx < len(row_str) else ""

            date_val  = _get("date")
            desc_val  = _get("description")
            ref_val   = _get("reference_number")
            type_val  = _get("transaction_type")  # e.g. "Debit" / "Credit" from a Type column

            debit_val,  debit_ind  = clean_amount_string(_get("debit"))
            credit_val, credit_ind = clean_amount_string(_get("credit"))
            amount_val, amount_ind = clean_amount_string(_get("amount"))
            balance_val, _         = clean_amount_string(_get("balance"))

            # Guard: if amount column mapped to the same index as balance column, clear amount_val
            if col_map.get("amount") is not None and col_map.get("amount") == col_map.get("balance"):
                amount_val = 0.0

            # Handle continuation rows (description spans multiple table rows)
            if not date_val and debit_val == 0.0 and credit_val == 0.0 and amount_val == 0.0:
                if desc_val and transactions:
                    # Append to last transaction's description
                    transactions[-1]["description"] += " " + desc_val.strip()
                    transactions[-1]["raw_text"]    += " " + raw_line
                continue

            # If no separate debit/credit columns but a "Type" column exists, use it to
            # split the amount column into debit or credit.
            if debit_val == 0.0 and credit_val == 0.0 and amount_val > 0.0 and type_val:
                type_lower = type_val.strip().lower()
                if re.search(r'\b(cr|credit|deposit|inflow|receipt|received|salary|refund)\b', type_lower):
                    credit_val = amount_val
                    debit_ind  = None
                    credit_ind = "credit"
                elif re.search(r'\b(dr|debit|withdrawal|withdraw|outflow|payment|charge|fee)\b', type_lower):
                    debit_val  = amount_val
                    credit_ind = None
                    debit_ind  = "debit"

            indicator = debit_ind or credit_ind or amount_ind or ""
            txn = build_normalized_transaction(
                date=date_val,
                description=desc_val,
                debit=debit_val,
                credit=credit_val,
                amount=amount_val,
                balance=balance_val,
                transaction_type=indicator,
                reference_number=ref_val,
                raw_text=raw_line,
                source_page=page_num,
                source_method="pdfplumber_table",
            )
            transactions.append(txn)

        return transactions

    # ──────────────────────────────────────────────────────────────────────────
    # Strategy 3: pdfplumber raw-text
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_with_pdfplumber_text(self, file_path: str, password: Optional[str] = None) -> List[Dict[str, Any]]:
        transactions: List[Dict[str, Any]] = []
        with pdfplumber.open(file_path, password=password) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                text = clean_ocr_text(text)
                txns = self._process_raw_text(text, page_num=page_num, method="pdfplumber_text")
                transactions.extend(txns)
        return transactions

    # ──────────────────────────────────────────────────────────────────────────
    # Strategy 4: OCR
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_scanned_pdf(self, file_path: str) -> List[Dict[str, Any]]:
        transactions: List[Dict[str, Any]] = []
        ocr_pages = perform_ocr_on_pdf(file_path)
        logger.info(f"[PDFParser] OCR produced {len(ocr_pages)} pages")
        for page_num, page_text in enumerate(ocr_pages, start=1):
            cleaned = clean_ocr_text(page_text)
            txns = self._process_raw_text(cleaned, page_num=page_num, method="ocr")
            transactions.extend(txns)
        return transactions

    # ──────────────────────────────────────────────────────────────────────────
    # Shared raw-text → transactions
    # ──────────────────────────────────────────────────────────────────────────

    def _process_raw_text(
        self,
        text: str,
        page_num: int = 0,
        method: str = "text",
    ) -> List[Dict[str, Any]]:
        """
        Parse a raw text block (from pdfplumber or OCR) line-by-line.

        BUG-005 / BUG-009 fixes:
          - Amount regex captures integer amounts too (not just .dd)
          - Description extracted by positional slicing (not str.replace)
          - Numbers identified by position: last 2 = amount + balance
        """
        transactions: List[Dict[str, Any]] = []
        lines = text.splitlines()
        logger.debug(f"[PDFParser._process_raw_text] Processing {len(lines)} lines (page={page_num})")

        for line in lines:
            line_str = line.strip()
            if not line_str or len(line_str) < 8:
                continue

            # A closing-balance footer is statement metadata, not a transaction.
            # It carries a date and an amount, so this parser used to ingest it
            # as a phantom row whose "amount" was the whole closing balance,
            # inflating the movement total and breaking balance continuity.
            if _CLOSING_LABEL_RE.search(line_str):
                logger.debug(f"[PDFParser._process_raw_text] Skipping summary line: {line_str!r}")
                continue

            # ── 1. Find date ──────────────────────────────────────────────────
            date_match: Optional[re.Match] = None
            for pattern in DATE_PATTERNS:
                m = re.search(pattern, line_str, re.IGNORECASE)
                if m:
                    date_match = m
                    break

            if not date_match:
                continue

            date_str = date_match.group(0)
            date_start, date_end = date_match.start(), date_match.end()

            # ── 2. Find all numeric tokens ────────────────────────────────────
            # BUG-005 fix: use _AMOUNT_RE which captures integers too
            number_matches = list(_AMOUNT_RE.finditer(line_str))
            if not number_matches:
                continue

            # Filter out the date's numeric parts (they're not amounts)
            amount_matches = [
                m for m in number_matches
                if not (date_start <= m.start() < date_end)
            ]

            if not amount_matches:
                continue

            amounts_raw = [m.group(0) for m in amount_matches]
            amounts: List[float] = []
            for raw in amounts_raw:
                val, _ = clean_amount_string(raw)
                if val > 0:
                    amounts.append(val)

            if not amounts:
                continue

            # Positional heuristic:
            # If 2+ numeric tokens exist: last value = balance, second-last = amount.
            # If 1 numeric token exists: it represents running balance; if previous balance exists, amount = |balance - prev_bal|.
            if len(amounts) >= 2:
                balance = amounts[-1]
                main_amount = amounts[-2]
            else:
                balance = amounts[0]
                prev_bal = float(transactions[-1]["balance"]) if (transactions and transactions[-1].get("balance") is not None) else 0.0
                main_amount = abs(balance - prev_bal) if prev_bal > 0 else 0.0

            # ── 3. Extract description (positional) ───────────────────────────
            # BUG-009 fix: slice the line, don't str.replace
            # Description = text between end-of-date and start-of-first-amount
            first_amount_start = amount_matches[0].start() if amount_matches else len(line_str)
            desc = line_str[date_end:first_amount_start].strip()
            # Clean leftover separators
            desc = re.sub(r'^[\s\|\-_]+|[\s\|\-_]+$', '', desc)

            if not desc:
                # If description is empty, take everything after the date
                desc = line_str[date_end:].strip()

            # ── 4. Detect debit / credit ──────────────────────────────────────
            is_debit = bool(re.search(
                r'\b(dr|debit|withdrawal|withdraw|w/d|paid\s+out)\b',
                line_str, re.IGNORECASE
            ))
            is_credit = bool(re.search(
                r'\b(cr|credit|deposit|dep|paid\s+in|salary|received)\b',
                line_str, re.IGNORECASE
            ))

            debit = 0.0
            credit = 0.0

            if is_debit and not is_credit:
                debit = main_amount
            elif is_credit and not is_debit:
                credit = main_amount
            else:
                # Format-specific fallback: no explicit direction marker found.
                # If running balance is present and previous transaction balance exists,
                # infer direction from balance delta.
                if transactions and transactions[-1].get("balance") is not None and balance > 0:
                    prev_bal = float(transactions[-1]["balance"])
                    if balance > prev_bal:
                        credit = main_amount
                    elif balance < prev_bal:
                        debit = main_amount
                    else:
                        debit = main_amount
                else:
                    debit = main_amount

            txn = build_normalized_transaction(
                date=date_str,
                description=desc,
                debit=debit,
                credit=credit,
                amount=main_amount,
                balance=balance,
                raw_text=line_str,
                source_page=page_num,
                source_method=method,
            )
            transactions.append(txn)

        return transactions
