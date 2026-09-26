import os
import logging
import pandas as pd
from services.normalizer import normalize_to_schema
from services.cleaner import clean_transaction_df

logger = logging.getLogger(__name__)

def validate_and_build_transaction_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Takes raw parsed DataFrame, normalizes columns to standard schema,
    applies clean date/currency/narration rules, and validates every row.

    Validation Rules:
    - Date exists and is not '1970-01-01' or empty.
    - Narration exists and is not empty.
    - At least one of withdrawal, deposit, or balance is > 0.

    Returns clean pandas.DataFrame with exact schema:
    [date, narration, withdrawal, deposit, balance]
    """
    if df is None or df.empty:
        logger.warning("Empty DataFrame passed to transaction_builder.")
        return pd.DataFrame(columns=['date', 'narration', 'withdrawal', 'deposit', 'balance'])

    # Step 1: Normalize column schema
    df_norm = normalize_to_schema(df)

    # Step 2: Apply cleaning rules
    df_clean = clean_transaction_df(df_norm)

    # Step 3: Validate rows strictly
    valid_rows = []
    for idx, row in df_clean.iterrows():
        date_val = str(row['date']).strip()
        narr_val = str(row['narration']).strip()
        w_val = float(row['withdrawal']) if pd.notnull(row['withdrawal']) else 0.0
        d_val = float(row['deposit']) if pd.notnull(row['deposit']) else 0.0
        b_val = float(row['balance']) if pd.notnull(row['balance']) else 0.0

        if not date_val or date_val == "1970-01-01":
            logger.warning(f"Row {idx} skipped: Missing/invalid date '{date_val}'")
            continue

        if not narr_val or narr_val == "UNSPECIFIED TRANSACTION":
            logger.warning(f"Row {idx} skipped: Missing narration")
            continue

        if w_val <= 0 and d_val <= 0 and b_val <= 0:
            logger.warning(f"Row {idx} skipped: No positive withdrawal, deposit, or balance value found.")
            continue

        valid_rows.append({
            'date': date_val,
            'narration': narr_val,
            'withdrawal': w_val if w_val > 0 else 0.0,
            'deposit': d_val if d_val > 0 else 0.0,
            'balance': b_val
        })

    result_df = pd.DataFrame(valid_rows, columns=['date', 'narration', 'withdrawal', 'deposit', 'balance'])
    logger.info(f"Successfully validated and built {len(result_df)} transaction rows.")
    return result_df
