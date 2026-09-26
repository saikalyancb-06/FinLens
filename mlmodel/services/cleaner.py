import re
import pandas as pd
import numpy as np

def clean_currency_str(val) -> float:
    """
    Removes ₹, Rs., commas, spaces, tabs, newlines, and trailing Cr/Dr tags.
    Preserves decimal points.
    Examples:
    '2,47,934.98' -> 247934.98
    '33,48,206.00' -> 3348206.00
    '1,10,73,64,312.00' -> 1107364312.00
    '2,47,934.98 Cr' -> 247934.98
    """
    if pd.isnull(val) or val is None or str(val).strip() == '':
        return 0.0
    
    val_str = str(val).strip()

    # Step 1: Remove currency tags (₹, Rs., INR, Cr, Dr)
    val_str = re.sub(r'(?:₹|Rs\.?|INR|\bCr\b|\bDr\b)', '', val_str, flags=re.IGNORECASE).strip()

    # Step 2: Remove spaces, tabs, newlines, and thousands separator commas
    val_str = val_str.replace(',', '').replace(' ', '').replace('\t', '').replace('\n', '').replace('\r', '')

    # Step 3: Extract valid numeric float string
    match = re.search(r'-?\d+(?:\.\d+)?', val_str)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return 0.0
    return 0.0

def normalize_date_str(date_val) -> str:
    """
    Normalizes dates into YYYY-MM-DD format.
    """
    if pd.isnull(date_val) or not date_val:
        return "1970-01-01"
    
    date_str = str(date_val).strip()
    try:
        parsed_dt = pd.to_datetime(date_str, dayfirst=True, errors='coerce')
        if pd.isnull(parsed_dt):
            parsed_dt = pd.to_datetime(date_str, errors='coerce')
        if not pd.isnull(parsed_dt):
            return parsed_dt.strftime('%Y-%m-%d')
    except Exception:
        pass
    return date_str

def clean_narration_str(narr_val) -> str:
    """
    Trims narration, removes tabs, newlines, extra spaces, and trailing Cr/Dr artifacts.
    """
    if pd.isnull(narr_val) or narr_val is None:
        return "UNSPECIFIED TRANSACTION"
    narr_str = str(narr_val).replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')
    narr_str = re.sub(r'\s+', ' ', narr_str).strip()
    # Remove trailing Cr/Dr artifact if present at end of narration
    narr_str = re.sub(r'\s+(?:Cr|Dr)$', '', narr_str, flags=re.IGNORECASE)
    return narr_str if narr_str else "UNSPECIFIED TRANSACTION"

def clean_transaction_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Cleans raw DataFrame to ensure all required fields follow standard formats.
    """
    cleaned = df.copy()
    if 'date' in cleaned.columns:
        cleaned['date'] = cleaned['date'].apply(normalize_date_str)
    if 'narration' in cleaned.columns:
        cleaned['narration'] = cleaned['narration'].apply(clean_narration_str)
    if 'withdrawal' in cleaned.columns:
        cleaned['withdrawal'] = cleaned['withdrawal'].apply(clean_currency_str)
    if 'deposit' in cleaned.columns:
        cleaned['deposit'] = cleaned['deposit'].apply(clean_currency_str)
    if 'balance' in cleaned.columns:
        cleaned['balance'] = cleaned['balance'].apply(clean_currency_str)
    return cleaned
