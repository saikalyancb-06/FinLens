import re
import datetime
import logging
from typing import List, Dict, Any, Tuple, Optional
from financial_parser.models.transaction import UniversalTransaction

logger = logging.getLogger(__name__)

MAX_REASONABLE_AMOUNT = 50000000.0  # 5 Crore MAX per single line

INVALID_DESCRIPTIONS = {
    'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC',
    'DEBIT', 'CREDIT', 'BALANCE', 'PAGE', 'SUBTOTAL', 'OPENING BALANCE', 'CLOSING BALANCE',
    'SL. NO', 'SL NO', 'MONTH', 'PARTICULARS', 'NARRATION', 'TRAN DATE', 'VALUE DATE'
}

DATE_PATTERNS = [
    r'^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$',                          # 31/03/2026, 31-03-2026
    r'^\d{4}[/-]\d{1,2}[/-]\d{1,2}$',                          # 2026-03-31
    r'^\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4}$' # 31 Mar 2026
]

def excel_serial_to_date(serial) -> str:
    """
    Converts Excel integer/float date serial numbers (e.g. 45839 -> '2025-07-01')
    or returns formatted string for datetime objects.
    """
    if isinstance(serial, (datetime.date, datetime.datetime)):
        return serial.strftime('%Y-%m-%d')
    try:
        val = float(serial)
        if 30000 <= val <= 60000:
            dt = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=val)
            return dt.strftime('%Y-%m-%d')
    except (ValueError, TypeError):
        pass
    return str(serial) if serial is not None else ""

def parse_currency_amount(val) -> Tuple[float, bool]:
    """
    Parses currency amount text into non-negative float.
    Rejects absurd amounts (e.g. 52,02,50,70,10,09,01,976 or 744605000000024)
    resulting from merged cell/parsing corruptions.
    """
    if val is None or str(val).strip() == '':
        return 0.0, True

    s = str(val).strip()
    s_clean = re.sub(r'(?:₹|Rs\.?|INR|\bCr\b|\bDr\b)', '', s, flags=re.IGNORECASE).strip()
    s_clean = s_clean.replace(',', '').replace(' ', '').replace('\t', '')

    match = re.search(r'\d+(?:\.\d+)?', s_clean)
    if not match:
        return 0.0, True

    try:
        amt = abs(float(match.group(0)))
        if amt > MAX_REASONABLE_AMOUNT:
            logger.warning(f"Rejected absurd amount: {amt} from raw string '{val}'")
            return 0.0, False
        return amt, True
    except ValueError:
        return 0.0, False

def validate_date_string(val) -> Tuple[str, bool]:
    """
    Strict Date Validation. Rejects month-only values like 'Jul' or 'Jan'.
    Accepts full valid dates DD/MM/YYYY, YYYY-MM-DD, DD-Mon-YYYY.
    """
    s = str(val).strip()
    if not s or s.upper() in INVALID_DESCRIPTIONS:
        return "", False

    for pat in DATE_PATTERNS:
        if re.match(pat, s, flags=re.IGNORECASE):
            return s, True

    return "", False

def validate_and_build_transaction(tx_data: Dict[str, Any]) -> Tuple[Optional[UniversalTransaction], List[str]]:
    """
    Strict validation rule check for candidate transactions.
    """
    errors = []
    
    raw_date = tx_data.get('date', '')
    raw_desc = str(tx_data.get('description', '')).strip()
    
    # 1. Date Validation
    date_str, is_valid_date = validate_date_string(raw_date)
    if not is_valid_date:
        errors.append(f"Invalid date: '{raw_date}'")

    # 2. Description Validation
    if not raw_desc or raw_desc.upper() in INVALID_DESCRIPTIONS or len(raw_desc) < 3:
        errors.append(f"Invalid description: '{raw_desc}'")

    # 3. Amount Validation
    debit_val, debit_ok = parse_currency_amount(tx_data.get('debit_str', tx_data.get('debit', 0.0)))
    credit_val, credit_ok = parse_currency_amount(tx_data.get('credit_str', tx_data.get('credit', 0.0)))
    
    if not debit_ok:
        errors.append(f"Absurd debit amount in row date '{raw_date}'")
    if not credit_ok:
        errors.append(f"Absurd credit amount in row date '{raw_date}'")

    # Both empty check
    if debit_val == 0.0 and credit_val == 0.0:
        errors.append("Both debit and credit are zero.")

    # Both populated check
    if debit_val > 0.0 and credit_val > 0.0:
        errors.append("Both debit and credit populated in same row.")

    # Balance extraction
    raw_bal = str(tx_data.get('balance_str', tx_data.get('balance', 0.0))).strip()
    bal_clean = re.sub(r'(?:₹|Rs\.?|INR|\bCr\b|\bDr\b)', '', raw_bal, flags=re.IGNORECASE).replace(',', '').replace(' ', '')
    match_bal = re.search(r'-?\d+(?:\.\d+)?', bal_clean)
    bal_val = float(match_bal.group(0)) if match_bal else 0.0

    if errors:
        return None, errors

    conf = 1.0
    if bal_val == 0.0:
        conf -= 0.1

    tx = UniversalTransaction(
        date=date_str,
        description=raw_desc,
        debit=debit_val,
        credit=credit_val,
        balance=bal_val,
        currency="INR",
        reference="",
        transaction_id="",
        page_number=int(tx_data.get('page_number', 1)),
        confidence=round(conf, 2)
    )

    return tx, []
