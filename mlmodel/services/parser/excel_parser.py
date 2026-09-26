import io
import pandas as pd
from services.normalizer import normalize_to_schema
from services.cleaner import clean_transaction_df
from services.parser.csv_parser import parse_text_lines_fallback

def parse_excel_file(file_content: bytes, filename: str = "") -> pd.DataFrame:
    """
    Parses Excel (.xlsx, .xls) supporting multiple sheets and header row detection.
    """
    try:
        excel_file = pd.ExcelFile(io.BytesIO(file_content))
        all_dfs = []

        for sheet_name in excel_file.sheet_names:
            # First attempt: standard header
            df_sheet = pd.read_excel(excel_file, sheet_name=sheet_name)
            
            # If standard columns not found, look for header row
            has_headers = any(c in [str(x).lower() for x in df_sheet.columns] for c in ['date', 'narration', 'particular', 'withdrawal', 'deposit', 'debit', 'credit', 'amount', 'balance'])
            
            if not has_headers:
                # Find header row in first 15 rows
                df_raw_sheet = pd.read_excel(excel_file, sheet_name=sheet_name, header=None)
                header_row_idx = None
                for r_idx, row_vals in df_raw_sheet.iterrows():
                    row_str = " ".join([str(v).lower() for v in row_vals.values])
                    if any(k in row_str for k in ['date', 'narration', 'particular', 'description', 'withdrawal', 'deposit', 'debit', 'credit', 'amount', 'balance']):
                        header_row_idx = r_idx
                        break
                
                if header_row_idx is not None:
                    df_sheet = pd.read_excel(excel_file, sheet_name=sheet_name, header=header_row_idx)

            if not df_sheet.empty:
                norm_sheet = normalize_to_schema(df_sheet)
                all_dfs.append(norm_sheet)

        if all_dfs:
            combined_df = pd.concat(all_dfs, ignore_index=True)
            cleaned = clean_transaction_df(combined_df)
            if not cleaned.empty:
                return cleaned
        
        return parse_text_lines_fallback(file_content)
    except Exception:
        return parse_text_lines_fallback(file_content)
