"""
pipeline.py  —  Transaction parsing pipeline with structured logging and accuracy report.

Changes vs original:
  NEW  Stage-by-stage logging: extracted / dropped / corrected / validated counts.
  NEW  process_file returns list of transactions WITH metadata fields intact.
  NEW  generate_accuracy_report() compares extracted vs expected counts per field.
  KEEP All existing public API surface unchanged.
"""

import os
import logging
from typing import List, Dict, Any, Optional

from app.parsers.base import BaseParser
from app.parsers.pdf_parser import PDFParser
from app.parsers.excel_csv_parser import ExcelCSVParser
from app.parsers.image_parser import ImageParser
from app.parsers.validator import TransactionValidator, ValidationResult
from app.ai.decision_engine import HybridDecisionEngine

logger = logging.getLogger(__name__)


class TransactionParsingPipeline:

    def __init__(
        self,
        confidence_threshold: float = 0.80,
        artifact_dir: Optional[str] = None,
        models_dir: Optional[str] = None,
    ):
        self.pdf_parser       = PDFParser()
        self.excel_csv_parser = ExcelCSVParser()
        self.image_parser     = ImageParser()
        self.validator        = TransactionValidator()
        self.decision_engine  = HybridDecisionEngine(
            confidence_threshold=confidence_threshold,
            artifact_dir=artifact_dir,
            models_dir=models_dir,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Public API (unchanged signatures)
    # ──────────────────────────────────────────────────────────────────────────

    def process_file(self, file_path: str, pdf_password: Optional[str] = None) -> List[Dict[str, Any]]:
        """Parse → Validate → Classify. Returns classified transactions."""
        raw_txns  = self.parse_raw_file(file_path, pdf_password=pdf_password)
        val_result = self.validator.validate(raw_txns)

        if val_result.errors:
            logger.warning(
                f"[Pipeline] {len(val_result.errors)} validation issue(s) in '{file_path}'"
            )
        if val_result.rejected:
            logger.warning(
                f"[Pipeline] {len(val_result.rejected)} row(s) rejected from '{file_path}'"
            )

        evaluated = self.decision_engine.evaluate_transactions(val_result.transactions)
        logger.info(
            f"[Pipeline] '{os.path.basename(file_path)}': "
            f"raw={len(raw_txns)} → valid={len(val_result.transactions)} "
            f"→ classified={len(evaluated)}"
        )
        return evaluated

    def process_file_with_validation(self, file_path: str, pdf_password: Optional[str] = None) -> Dict[str, Any]:
        """Parse → Validate → Classify. Returns full summary dict."""
        raw_txns   = self.parse_raw_file(file_path, pdf_password=pdf_password)
        val_result = self.validator.validate(raw_txns)
        evaluated  = self.decision_engine.evaluate_transactions(val_result.transactions)

        logger.info(
            f"[Pipeline] '{os.path.basename(file_path)}': "
            f"raw={len(raw_txns)} | rejected={len(val_result.rejected)} "
            f"| valid={len(val_result.transactions)} | classified={len(evaluated)}"
        )

        # Propagate any statement-level metadata produced by the parser (e.g. explicit closing balance)
        statement_meta = None
        try:
            # pdf parser sets `last_statement_closing` when it can detect a footer closing balance
            statement_meta = getattr(self.pdf_parser, "last_statement_closing", None)
        except Exception:
            statement_meta = None

        return {
            "transactions":   evaluated,
            "errors":         val_result.errors,
            "rejected":       val_result.rejected,
            "total_extracted": len(raw_txns),
            "total_valid":    len(evaluated),
            "total_rejected": len(val_result.rejected),
            "continuity_pass_rate": val_result.continuity_pass_rate,
            "continuity_passed": val_result.continuity_passed,
            "statement_meta": {"closing_balance": statement_meta} if statement_meta is not None else {},
        }

    def parse_raw_file(self, file_path: str, pdf_password: Optional[str] = None) -> List[Dict[str, Any]]:
        """Route file to the appropriate parser and return raw transactions."""
        if not os.path.exists(file_path):
            logger.error(f"[Pipeline] File not found: {file_path}")
            raise FileNotFoundError(f"File not found: {file_path}")

        ext = os.path.splitext(file_path)[1].lower()
        logger.info(f"[Pipeline] Reading {ext.upper()} file: {os.path.basename(file_path)}")

        try:
            if ext == ".pdf":
                result = self.pdf_parser.parse(file_path, password=pdf_password)
            elif ext in (".csv", ".xls", ".xlsx"):
                result = self.excel_csv_parser.parse(file_path)
            elif ext in (".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp"):
                result = self.image_parser.parse(file_path)
            else:
                raise ValueError(f"Unsupported file format: '{ext}'")
        except Exception as e:
            logger.error(f"[Pipeline] Parse error for '{file_path}': {e}", exc_info=True)
            raise

        logger.info(f"[Pipeline] Extracted {len(result)} raw row(s) from '{os.path.basename(file_path)}'")
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Accuracy report
    # ──────────────────────────────────────────────────────────────────────────

    def generate_accuracy_report(
        self,
        file_path: str,
        expected_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Run the full pipeline and produce an accuracy / coverage report.

        Args:
            file_path:      Path to the statement file.
            expected_count: Known transaction count from the source PDF (if available).

        Returns a dict with:
            rows_expected, rows_extracted, rows_valid, rows_rejected,
            coverage_pct, field_completeness, warnings_summary.
        """
        raw      = self.parse_raw_file(file_path)
        val_res  = self.validator.validate(raw)
        valid    = val_res.transactions
        rejected = val_res.rejected

        extracted = len(raw)
        n_valid   = len(valid)
        n_rejected = len(rejected)

        # Field completeness (% of valid rows where field is non-zero/non-empty)
        def _pct(field: str, truthy_check=None) -> float:
            if not valid:
                return 0.0
            count = sum(
                1 for t in valid
                if (truthy_check(t.get(field)) if truthy_check else bool(t.get(field)))
            )
            return round(count / len(valid) * 100, 1)

        field_completeness = {
            "date":        _pct("date"),
            "description": _pct("description"),
            "amount":      _pct("amount", lambda v: float(v or 0) > 0),
            "debit":       _pct("debit",  lambda v: float(v or 0) > 0),
            "credit":      _pct("credit", lambda v: float(v or 0) > 0),
            "balance":     _pct("balance", lambda v: float(v or 0) > 0),
            "reference":   _pct("reference_number"),
        }

        # Warnings summary
        all_warnings = [w for t in valid for w in t.get("warnings", [])]
        from collections import Counter
        warnings_summary = dict(Counter(all_warnings).most_common(10))

        # Error type summary
        error_types = [e.get("error_type", "unknown") for e in val_res.errors]
        errors_summary = dict(Counter(error_types).most_common(10))

        coverage_pct = round(n_valid / expected_count * 100, 1) if expected_count else None

        report = {
            "file":              os.path.basename(file_path),
            "rows_expected":     expected_count,
            "rows_extracted":    extracted,
            "rows_valid":        n_valid,
            "rows_rejected":     n_rejected,
            "coverage_pct":      coverage_pct,
            "field_completeness": field_completeness,
            "warnings_summary":  warnings_summary,
            "errors_summary":    errors_summary,
        }

        logger.info(f"[Pipeline] Accuracy report: {report}")
        return report
