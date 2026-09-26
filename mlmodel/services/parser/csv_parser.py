import io
import re
import pandas as pd
from services.normalizer import normalize_to_schema
from services.cleaner import clean_transaction_df

def parse_csv_file(file_content: bytes, filename: str = "") -> pd.DataFrame:
    """
    Robust CSV parser handling diverse delimiter formats and header variations.
    """
    try:
        # Step 1: Read raw text lines
        text = file_content.decode('utf-8', errors='ignore')
        if not text.strip():
            text = file_content.decode('latin-1', errors='ignore')

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return pd.DataFrame(columns=['date', 'narration', 'withdrawal', 'deposit', 'balance'])

        # Step 2: Detect header row index
        header_idx = 0
        for i, line in enumerate(lines[:15]):
            line_lower = line.lower()
            if any(k in line_lower for k in ['date', 'narration', 'particular', 'description', 'withdrawal', 'deposit', 'debit', 'credit', 'amount', 'balance']):
                header_idx = i
                break

        csv_data = "\n".join(lines[header_idx:])
        
        # Try standard comma separator
        try:
            df_raw = pd.read_csv(io.StringIO(csv_data))
        except Exception:
            df_raw = pd.read_csv(io.StringIO(csv_data), sep=None, engine='python')

        normalized_df = normalize_to_schema(df_raw)
        cleaned_df = clean_transaction_df(normalized_df)
        return cleaned_df

    except Exception:
        # Fallback to regex line-by-line extraction
        return parse_text_lines_fallback(file_content)

def parse_text_lines_fallback(file_content: bytes) -> pd.DataFrame:
    text = file_content.decode('utf-8', errors='ignore')
    lines = text.splitlines()
    records = []

    date_pattern = r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\b'
    amount_pattern = r'([\d,]+\.?\d*)'

    for line in lines:
        line_str = line.strip()
        if not line_str: continue
            
        date_match = re.search(date_pattern, line_str)
        if date_match:
            date_val = date_match.group(1)
            # Find numbers
            numbers = re.findall(amount_pattern, line_str.replace(date_val, ''))
            valid_nums = [n for n in numbers if len(n.replace(',', '')) > 0 and n != date_val]
            
            narr_val = line_str
            w_val = 0.0
            d_val = 0.0
            b_val = 0.0

            if len(valid_nums) >= 1:
                try: w_val = float(valid_nums[0].replace(',', ''))
                except: pass
            if len(valid_nums) >= 2:
                try: d_val = float(valid_nums[1].replace(',', ''))
                except: pass
            if len(valid_nums) >= 3:
                try: b_val = float(valid_nums[2].replace(',', ''))
                except: pass

            records.append({
                'date': date_val,
                'narration': narr_val,
                'withdrawal': w_val,
                'deposit': d_val,
                'balance': b_val
            })

    if records:
        df_raw = pd.DataFrame(records)
        return clean_transaction_df(normalize_to_schema(df_raw))

    return pd.DataFrame(columns=['date', 'narration', 'withdrawal', 'deposit', 'balance'])
