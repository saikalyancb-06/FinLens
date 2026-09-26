import re
import pandas as pd
import numpy as np

def clean_text(text: str) -> str:
    """
    1. Text Cleaning
    - Lowercase
    - Remove transaction IDs / reference numbers
    - Remove timestamps
    - Remove punctuation / special characters
    - Normalize narration
    """
    if not isinstance(text, str):
        return ""

    text = text.lower()

    # Remove timestamps like 16:42:16 or 2025-07-01
    text = re.sub(r'\b\d{2}:\d{2}(?::\d{2})?\b', ' ', text)
    text = re.sub(r'\b\d{2}/\d{2}/\d{4}\b', ' ', text)

    # Remove long transaction reference IDs (alphanumeric sequences with length > 6)
    text = re.sub(r'\b[a-z0-9]{8,}\b', ' ', text)
    text = re.sub(r'\b\d{6,}\b', ' ', text)

    # Remove punctuation
    text = re.sub(r'[^\w\s]', ' ', text)

    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_derived_features(narration: str, withdrawal: float, deposit: float) -> dict:
    """
    Extracts features required for rule-engine & ML pipeline.
    Input Features: Narration, Withdrawal, Deposit
    Derived Features:
      - amount = max(withdrawal, deposit)
      - is_credit
      - is_debit
      - word_count
      - contains_numbers
      - contains_special_characters
      - merchant_name
      - bank_name
      - transaction_mode
    """
    w = float(withdrawal) if pd.notnull(withdrawal) and str(withdrawal).strip() != "" else 0.0
    d = float(deposit) if pd.notnull(deposit) and str(deposit).strip() != "" else 0.0

    amount = max(w, d)
    is_credit = 1 if d > 0 else 0
    is_debit = 1 if w > 0 else 0

    narr_str = str(narration) if pd.notnull(narration) else ""
    cleaned_narr = clean_text(narr_str)

    word_count = len(cleaned_narr.split()) if cleaned_narr else 0
    contains_numbers = 1 if re.search(r'\d', narr_str) else 0
    contains_special_characters = 1 if re.search(r'[^\w\s]', narr_str) else 0

    # Extract transaction mode
    transaction_mode = "OTHER"
    upper_narr = narr_str.upper()
    if "NEFT" in upper_narr:
        transaction_mode = "NEFT"
    elif "RTGS" in upper_narr:
        transaction_mode = "RTGS"
    elif "IMPS" in upper_narr:
        transaction_mode = "IMPS"
    elif "UPI" in upper_narr:
        transaction_mode = "UPI"
    elif "EBANK" in upper_narr:
        transaction_mode = "EBANK"
    elif "POS" in upper_narr or "POSRENT" in upper_narr:
        transaction_mode = "POS"
    elif "ATM" in upper_narr:
        transaction_mode = "ATM"

    # Extracted entity heuristics
    bank_names = ["ICICI", "CANARA", "STATE BANK", "HDFC", "KOTAK", "AXIS", "UNION BANK", "INDIAN BANK", "KARNATAKA BANK", "YES BANK"]
    bank_name = "UNKNOWN"
    for b in bank_names:
        if b in upper_narr:
            bank_name = b
            break

    # Merchant entity guess
    words = cleaned_narr.split()
    merchant_name = words[0] if len(words) > 0 else "UNKNOWN"

    return {
        'cleaned_narration': cleaned_narr,
        'amount': amount,
        'is_credit': is_credit,
        'is_debit': is_debit,
        'word_count': word_count,
        'contains_numbers': contains_numbers,
        'contains_special_characters': contains_special_characters,
        'merchant_name': merchant_name,
        'bank_name': bank_name,
        'transaction_mode': transaction_mode
    }
