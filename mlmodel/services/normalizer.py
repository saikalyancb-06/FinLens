import pandas as pd

STANDARD_COLUMNS = ['date', 'narration', 'withdrawal', 'deposit', 'balance']

def normalize_to_schema(df: pd.DataFrame) -> pd.DataFrame:
    """
    Converts any extracted DataFrame into the standard schema:
    [date, narration, withdrawal, deposit, balance]
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    normalized = pd.DataFrame()
    col_mapping = {}

    for col in df.columns:
        col_lower = str(col).lower().strip()

        if col_lower == 'balance_dr_cr':
            continue

        if 'date' in col_lower or 'time' in col_lower:
            if 'date' not in col_mapping.values(): col_mapping[col] = 'date'
        elif any(k in col_lower for k in ['narration', 'particular', 'description', 'detail', 'remark']):
            if 'narration' not in col_mapping.values(): col_mapping[col] = 'narration'
        elif any(k in col_lower for k in ['withdrawal', 'debit', 'outflow']) or col_lower == 'dr':
            if 'withdrawal' not in col_mapping.values(): col_mapping[col] = 'withdrawal'
        elif any(k in col_lower for k in ['deposit', 'credit', 'inflow']) or col_lower == 'cr':
            if 'deposit' not in col_mapping.values(): col_mapping[col] = 'deposit'
        elif 'balance' in col_lower or col_lower == 'bal':
            if 'balance' not in col_mapping.values(): col_mapping[col] = 'balance'

    renamed_df = df.rename(columns=col_mapping)

    for std_col in STANDARD_COLUMNS:
        if std_col in renamed_df.columns:
            target_data = renamed_df[std_col]
            if isinstance(target_data, pd.DataFrame):
                normalized[std_col] = target_data.iloc[:, 0]
            else:
                normalized[std_col] = target_data
        else:
            normalized[std_col] = 0.0 if std_col in ['withdrawal', 'deposit', 'balance'] else ""

    return normalized[STANDARD_COLUMNS]
