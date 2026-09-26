import logging
from typing import List, Dict, Any, Tuple
from financial_parser.models.transaction import UniversalTransaction, ExtractionResult
from financial_parser.services.parser_router import route_and_extract, detect_document_type
from financial_parser.validation.validator import validate_and_build_transaction

logger = logging.getLogger(__name__)

class OfflineFinancialParserEngine:
    """
    Production Offline Financial Document Parsing Engine v2.0.
    Enforces quality checks (max 2% failure threshold) and returns strict ExtractionResult schema.
    """
    def process_document(self, file_content: bytes, filename: str) -> ExtractionResult:
        file_fmt = detect_document_type(file_content, filename)
        logger.info(f"Engine processing file '{filename}' (detected file_type='{file_fmt}')")

        # Step 1 & 2: Route and extract raw candidates
        raw_candidates, header_conf = route_and_extract(file_content, filename)
        
        valid_transactions: List[UniversalTransaction] = []
        validation_errors: List[str] = []
        failed_rows_count = 0
        seen_dedupe_keys = set()

        total_extracted = len(raw_candidates)

        for candidate in raw_candidates:
            tx, errors = validate_and_build_transaction(candidate)
            if tx:
                # Calculate unique key for deduplication
                amt_key = tx.credit if tx.credit > 0 else tx.debit
                dedupe_key = (tx.date, tx.description.strip(), amt_key, tx.balance)
                if dedupe_key in seen_dedupe_keys:
                    logger.info(f"Deduplicating row: {dedupe_key}")
                    continue
                seen_dedupe_keys.add(dedupe_key)
                valid_transactions.append(tx)
            else:
                failed_rows_count += 1
                validation_errors.extend(errors)

        # Quality Check: If failure rate exceeds 2% or zero transactions extracted
        failure_rate = (failed_rows_count / total_extracted) if total_extracted > 0 else 1.0

        if total_extracted == 0 or failure_rate > 0.02:
            logger.error(f"PARSING_FAILED: Total rows={total_extracted}, failed_rows={failed_rows_count}, rate={failure_rate:.2%}")
            return ExtractionResult(
                document_type="Bank Statement",
                file_type=file_fmt.upper(),
                header_confidence=header_conf,
                total_transactions=0,
                failed_rows=failed_rows_count,
                validation_errors=[f"PARSING_FAILED: Validation failure rate ({failure_rate:.1%}) exceeded 2.0% threshold."] + validation_errors[:5],
                transactions=[]
            )

        return ExtractionResult(
            document_type="Bank Statement",
            file_type=file_fmt.upper(),
            header_confidence=header_conf,
            total_transactions=len(valid_transactions),
            failed_rows=failed_rows_count,
            validation_errors=validation_errors[:10],
            transactions=valid_transactions
        )
