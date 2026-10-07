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
                result = self._parse_pdf_verified(file_path, pdf_password)
                if result is None:
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

    def _parse_pdf_verified(self, file_path: str, pdf_password: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        """Bank PDFs through the balance-verified extractor first.

        The same extractor the statement API uses (app/b2b/consolidate). Its rows
        are accepted only when EVERY consecutive pair reconciles
        (previous balance +/- amount = balance), so it can only replace the
        legacy parser where it is provably right. On an Axis current-account
        statement the legacy parser recorded 51 receipts as payments (debits
        2.80 cr against a true 1.48 cr) and attached wrapped narration lines to
        the wrong rows; this reads all 348 rows and reconciles to the paisa.
        Anything it cannot reconcile falls through to the legacy parser
        unchanged.
        """
        try:
            from app.b2b.consolidate.extract import CREDIT, continuity_score, extract_pdf
            from app.parsers.normalizer import build_normalized_transaction
            ex = extract_pdf(file_path, password=pdf_password)
            ok, checked = continuity_score(ex.rows)
        except Exception as exc:  # noqa: BLE001 - the legacy parser is the fallback
            logger.info(f"[Pipeline] verified extractor declined '{os.path.basename(file_path)}': {exc}")
            return None
        if not ex.rows or checked < 1 or ok != checked:
            return None
        rows: List[Dict[str, Any]] = []
        for r in ex.rows:
            amount = r.amount_paise / 100.0
            credit = r.direction == CREDIT
            rows.append(build_normalized_transaction(
                date=r.date.isoformat(),
                description=r.narration,
                debit=0.0 if credit else amount,
                credit=amount if credit else 0.0,
                amount=amount,
                balance=(r.balance_paise / 100.0) if r.balance_paise is not None else 0.0,
                transaction_type="credit" if credit else "debit",
                reference_number=r.reference or "",
                raw_text=r.narration,
                source_method="verified_extractor",
            ))
        # The legacy parser publishes a printed closing balance this way.
        self.pdf_parser.last_statement_closing = (
            ex.closing_paise / 100.0 if ex.closing_paise is not None else None)
        logger.info(f"[Pipeline] verified extractor: {len(rows)} rows, {checked} balance checks passed")
        return rows

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
