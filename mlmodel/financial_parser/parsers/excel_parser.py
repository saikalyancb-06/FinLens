import io
import datetime
import openpyxl
import logging
from typing import List, Dict, Any, Tuple
from financial_parser.validation.validator import excel_serial_to_date

logger = logging.getLogger(__name__)

def score_header_row(row_cells: List[Any]) -> Tuple[float, Dict[str, int]]:
    """
    Scores a candidate header row across first 30 rows based on keyword match density.
    """
    row_str = [str(c).upper() if c is not None else "" for c in row_cells]
    joined = " ".join(row_str)

    col_map = {'date': -1, 'description': -1, 'debit': -1, 'credit': -1, 'balance': -1}
    score = 0.0

    for idx, val in enumerate(row_str):
        if any(k in val for k in ['TRAN DATE', 'TXN DATE', 'VALUE DATE', 'DATE']):
            col_map['date'] = idx
            score += 2.0
        elif any(k in val for k in ['NARRATION', 'PARTICULAR', 'DESCRIPTION', 'REMARKS', 'DETAILS']):
            col_map['description'] = idx
            score += 2.0
        elif any(k in val for k in ['WITHDRAWAL', 'DEBIT', 'DR', 'PAID OUT']):
            col_map['debit'] = idx
            score += 2.0
        elif any(k in val for k in ['DEPOSIT', 'CREDIT', 'CR', 'PAID IN']):
            col_map['credit'] = idx
            score += 2.0
        elif any(k in val for k in ['BALANCE', 'RUNNING BALANCE']):
            col_map['balance'] = idx
            score += 2.0

    return score, col_map

def parse_excel_document(file_content: bytes, filename: str) -> Tuple[List[Dict[str, Any]], float]:
    """
    Production-grade Excel Parser evaluating first 30 rows for best scoring header row.
    Ignores merged cells, formatting, and converts date serials.
    """
    records = []
    best_header_confidence = 0.0

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_content), data_only=True)
        
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue

            # Step 1: Search first 30 rows for highest scoring header row
            best_score = -1.0
            best_idx = -1
            best_map = {'date': -1, 'description': -1, 'debit': -1, 'credit': -1, 'balance': -1}

            for idx, r in enumerate(rows[:30]):
                if not any(r):
                    continue
                score, col_map = score_header_row(r)
                if score > best_score:
                    best_score = score
                    best_idx = idx
                    best_map = col_map

            best_header_confidence = min(1.0, best_score / 10.0)

            if best_idx == -1 or best_map['date'] == -1:
                # Fallback mapping
                best_idx = 0
                best_map = {'date': 0, 'description': 1, 'debit': 2, 'credit': 3, 'balance': 4}

            # Parse data rows below best header row
            for row in rows[best_idx + 1:]:
                if not any(row):
                    continue

                raw_date = row[best_map['date']] if 0 <= best_map['date'] < len(row) else None
                raw_desc = row[best_map['description']] if 0 <= best_map['description'] < len(row) else None
                raw_debit = row[best_map['debit']] if 0 <= best_map['debit'] < len(row) else None
                raw_credit = row[best_map['credit']] if 0 <= best_map['credit'] < len(row) else None
                raw_bal = row[best_map['balance']] if 0 <= best_map['balance'] < len(row) else None

                if raw_date is None and raw_desc is None:
                    continue

                date_str = excel_serial_to_date(raw_date)

                records.append({
                    'page_number': 1,
                    'date': date_str,
                    'description': str(raw_desc or "").strip(),
                    'debit_str': str(raw_debit or 0.0),
                    'credit_str': str(raw_credit or 0.0),
                    'balance_str': str(raw_bal or 0.0)
                })

    except Exception as e:
        logger.error(f"Excel parsing error for {filename}: {e}", exc_info=True)

    return records, best_header_confidence
