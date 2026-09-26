"""
excel_csv_parser.py  —  Production-grade Excel/CSV parser.

Fixes applied (vs original):
  BUG-008  CSV now uses csv.reader with proper quoting (not manual split(','))
           so quoted fields containing commas are handled correctly.
  NEW      Continuation-row merging (description spans multiple rows).
  NEW      Metadata (source_page, source_method) on every transaction.
  NEW      Structured logging.
"""

import csv
import io
import logging
import os
from typing import List, Dict, Any

import pandas as pd

from app.parsers.base import BaseParser
from app.parsers.normalizer import (
    map_headers,
    clean_amount_string,
    build_normalized_transaction,
    clean_ocr_text,
)

logger = logging.getLogger(__name__)


class ExcelCSVParser(BaseParser):

    def parse(self, file_path: str) -> List[Dict[str, Any]]:
        logger.info(f"[ExcelCSVParser] Parsing: {file_path}")
        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".csv":
            df = self._read_csv_safe(file_path)
        else:
            df = self._read_excel_safe(file_path)

        if df is None or df.empty:
            logger.warning(f"[ExcelCSVParser] Empty dataframe for {file_path}")
            return []

        transactions = self._process_dataframe(df)
        logger.info(f"[ExcelCSVParser] Extracted {len(transactions)} transactions")
        return self._orient_chronologically(transactions)

    def _orient_chronologically(self, transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(transactions) > 1:
            first_dt = str(transactions[0].get("date", ""))
            last_dt  = str(transactions[-1].get("date", ""))
            if first_dt and last_dt and first_dt > last_dt:
                logger.info(f"[ExcelCSVParser] Detected reverse chronological order ({first_dt} -> {last_dt}). Reversing to chronological order.")
                transactions = list(reversed(transactions))
        for i, t in enumerate(transactions):
            t["row_index"] = i
        return transactions

    # ──────────────────────────────────────────────────────────────────────────
    # File readers
    # ──────────────────────────────────────────────────────────────────────────

    def _read_csv_safe(self, file_path: str) -> pd.DataFrame:
        """
        BUG-008 fix: Use Python's csv.reader instead of manual split(',')
        so quoted fields (e.g. "NEFT transfer, to XYZ") are handled correctly.
        Try multiple encodings.
        """
        encodings = ["utf-8-sig", "utf-8", "latin-1", "cp1252", "iso-8859-1"]
        for enc in encodings:
            try:
                with open(file_path, "r", encoding=enc, errors="replace", newline="") as f:
                    raw = f.read()

                reader = csv.reader(io.StringIO(raw))
                rows = list(reader)
                if not rows:
                    continue

                # Normalise row widths to max-width
                max_cols = max(len(r) for r in rows)
                padded = [r + [""] * (max_cols - len(r)) for r in rows]

                df = pd.DataFrame(padded, dtype=str)
                df = df.fillna("")

                if df.shape[1] >= 2:
                    logger.debug(f"[ExcelCSVParser] CSV read OK ({enc}): {df.shape}")
                    return df
            except Exception as e:
                logger.debug(f"[ExcelCSVParser] CSV read failed ({enc}): {e}")
                continue

        logger.error(f"[ExcelCSVParser] All encodings failed for {file_path}")
        return pd.DataFrame()

    def _read_excel_safe(self, file_path: str) -> pd.DataFrame:
        """
        Robust Excel Reader:
        1. Reads multi-sheet Excel files (evaluating all sheets to find valid transactions).
        2. Tries engines: openpyxl, xlrd.
        3. Fallback: If file is an HTML table exported with .xlsx / .xls extension, parses via pd.read_html.
        """
        # Try native Excel engines first (multi-sheet support)
        for engine in [None, "openpyxl", "xlrd"]:
            try:
                kwargs = {"header": None, "dtype": str}
                if engine:
                    kwargs["engine"] = engine
                
                sheet_dict = pd.read_excel(file_path, sheet_name=None, **kwargs)
                if isinstance(sheet_dict, dict) and sheet_dict:
                    best_df = None
                    max_rows = -1
                    for sheet_name, df in sheet_dict.items():
                        df = df.fillna("")
                        if len(df) > max_rows and not df.empty and df.shape[1] >= 2:
                            max_rows = len(df)
                            best_df = df
                    if best_df is not None and not best_df.empty:
                        logger.debug(f"[ExcelCSVParser] Excel read OK (engine={engine}, best_sheet shape={best_df.shape})")
                        return best_df
            except Exception as e:
                logger.debug(f"[ExcelCSVParser] Excel engine='{engine}' read failed: {e}")

        # Fallback for HTML tables saved with .xls / .xlsx extension (common bank export behaviour)
        try:
            html_tables = pd.read_html(file_path)
            if html_tables:
                best_df = max(html_tables, key=lambda t: len(t))
                best_df = best_df.astype(str).fillna("")
                logger.info(f"[ExcelCSVParser] Read Excel as HTML table fallback: shape={best_df.shape}")
                return best_df
        except Exception as e:
            logger.debug(f"[ExcelCSVParser] HTML table fallback failed for {file_path}: {e}")

        return pd.DataFrame()

    # ──────────────────────────────────────────────────────────────────────────
    # DataFrame → transactions
    # ──────────────────────────────────────────────────────────────────────────

    def _process_dataframe(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        transactions: List[Dict[str, Any]] = []
        header_idx = -1
        col_map: Dict[str, int] = {}

        # Check if df.columns contains header names (e.g. from pd.read_html or default read_excel)
        col_headers = [str(c).strip() for c in df.columns]
        mapping = map_headers(col_headers)
        if "date" in mapping or ("description" in mapping and ("amount" in mapping or "debit" in mapping or "credit" in mapping)):
            header_idx = -1
            col_map = mapping
            logger.debug(f"[ExcelCSVParser] Header found in df.columns: {col_map}")
        else:
            # Find header row in first 100 rows
            for idx in range(min(100, len(df))):
                row_vals = [str(x).strip() for x in df.iloc[idx].values]
                mapping = map_headers(row_vals)
                if "date" in mapping or (
                    "description" in mapping
                    and ("amount" in mapping or "debit" in mapping or "credit" in mapping)
                ):
                    header_idx = idx
                    col_map = mapping
                    logger.debug(f"[ExcelCSVParser] Header found at row {idx}: {col_map}")
                    break

        if not col_map:
            logger.warning("[ExcelCSVParser] No header row detected — skipping file")
            return transactions

        def _get(row_list: List[str], field: str) -> str:
            idx = col_map.get(field, -1)
            return clean_ocr_text(row_list[idx]) if 0 <= idx < len(row_list) else ""

        for row_idx in range(header_idx + 1, len(df)):
            row = df.iloc[row_idx]
            row_str = [str(c).strip() for c in row.values]
            raw_line = " | ".join(s for s in row_str if s)

            if not raw_line:
                continue

            date_val  = _get(row_str, "date")
            desc_val  = _get(row_str, "description")
            ref_val   = _get(row_str, "reference_number")

            debit_val,  debit_ind  = clean_amount_string(_get(row_str, "debit"))
            credit_val, credit_ind = clean_amount_string(_get(row_str, "credit"))
            amount_val, amount_ind = clean_amount_string(_get(row_str, "amount"))
            balance_val, _         = clean_amount_string(_get(row_str, "balance"))

            # Continuation row: no date and no amounts → merge description upward
            if not date_val and debit_val == 0.0 and credit_val == 0.0 and amount_val == 0.0:
                if desc_val and transactions:
                    transactions[-1]["description"] += " " + desc_val
                    transactions[-1]["raw_text"]    += " | " + raw_line
                continue

            type_val  = _get(row_str, "transaction_type")
            indicator = debit_ind or credit_ind or amount_ind or type_val or ""
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
                source_page=1,
                source_method="excel_csv",
            )
            transactions.append(txn)

        return transactions
