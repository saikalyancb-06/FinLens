import io
import pandas as pd
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

def parse_csv_document(file_content: bytes, filename: str) -> List[Dict[str, Any]]:
    """
    CSV Document Parser.
    Reads raw CSV text and maps rows to universal transaction dictionary representation.
    """
    records = []
    try:
        text = file_content.decode('utf-8', errors='ignore')
        df = pd.read_csv(io.StringIO(text))

        for idx, row in df.iterrows():
            d_val = row.get('date', row.get('Date', ''))
            n_val = row.get('narration', row.get('description', row.get('Narration', row.get('Particulars', ''))))
            w_val = row.get('withdrawal', row.get('debit', row.get('Withdrawal', 0.0)))
            c_val = row.get('deposit', row.get('credit', row.get('Deposit', 0.0)))
            b_val = row.get('balance', row.get('Balance', 0.0))

            records.append({
                'page_number': 1,
                'date': str(d_val),
                'description': str(n_val),
                'debit_str': str(w_val),
                'credit_str': str(c_val),
                'balance_str': str(b_val)
            })
    except Exception as e:
        logger.error(f"CSV parsing error for {filename}: {e}")

    return records
