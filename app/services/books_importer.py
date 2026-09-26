import hashlib
import re
from decimal import Decimal
from typing import Dict, Any, List, Tuple, Optional
import pandas as pd


def parse_amount_to_paise(val: Any) -> int:
    """
    Parse source string via Decimal to integer paise.
    Never via float. Handle Indian formatting:
    - 1,00,000.00
    - (1,000.00) for negative
    - 1000 Dr / 1000 Cr
    - blank / None / NaN as 0
    """
    if val is None or pd.isna(val):
        return 0
    
    s = str(val).strip()
    if not s:
        return 0

    is_negative = False
    
    # Check for (1,000.00) syntax
    if s.startswith("(") and s.endswith(")"):
        is_negative = True
        s = s[1:-1].strip()

    # Check Dr / Cr suffix
    s_upper = s.upper()
    if s_upper.endswith("DR"):
        s = s[:-2].strip()
    elif s_upper.endswith("CR"):
        s = s[:-2].strip()

    # Remove currency symbols and commas
    s = re.sub(r"[^\d.-]", "", s)
    if not s or s == "-":
        return 0

    if s.startswith("-"):
        is_negative = True
        s = s[1:]

    try:
        d = Decimal(s)
        paise = int(round(d * 100))
        return -paise if is_negative else paise
    except Exception:
        return 0


def compute_row_hash(row_dict: Dict[str, Any]) -> str:
    """Compute deterministic SHA256 hash of row dict items."""
    s = "|".join(f"{k}:{str(v).strip()}" for k, v in sorted(row_dict.items()))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class BooksImporterService:
    """Handles Tally/ERP books ledger import with sign convention normalization."""

    COLUMN_KEYWORDS = {
        "entry_date": ["date", "entry date", "txndate", "txn date", "voucher date"],
        "voucher_no": ["voucher no", "vch no", "voucher_no", "voucher number", "ref no"],
        "voucher_type": ["voucher type", "vch type", "type"],
        "narration": ["narration", "particulars", "description", "remarks"],
        "party_name": ["party", "party name", "ledger", "account name"],
        "instrument_no": ["instrument no", "chq no", "cheque no", "ref", "instrument_no", "cheque/ref no"],
        "instrument_date": ["instrument date", "chq date", "cheque date"],
        "money_in": ["debit", "dr", "deposit", "receipt", "money in", "inflow"],
        "money_out": ["credit", "cr", "withdrawal", "payment", "money out", "outflow"],
    }

    @classmethod
    def score_column(cls, field: str, col_name: str, values: List[str], df: pd.DataFrame) -> float:
        h = str(col_name).strip().lower()
        non_blank = [str(v).strip() for v in values if v is not None and str(v).strip() != "" and not pd.isna(v)]
        total = len(non_blank)
        if total == 0:
            return 0.0

        # Exact literal Debit / Credit mapping for books ledger
        headers_lower = [str(c).strip().lower() for c in df.columns]
        if ("debit" in headers_lower and "credit" in headers_lower) or ("dr" in headers_lower and "cr" in headers_lower):
            if field == "money_in" and h in ["debit", "dr"]:
                return 1.0
            if field == "money_out" and h in ["credit", "cr"]:
                return 1.0

        # Keyword matching score
        kw_score = 0.0
        keywords = cls.COLUMN_KEYWORDS.get(field, [])
        for kw in keywords:
            if kw == h:
                kw_score = 1.0
                break
            elif kw in h:
                kw_score = 0.8
                break

        # Data shape score calculation
        if field in ["money_in", "money_out"]:
            parseable_num = 0
            for v in non_blank:
                clean_v = re.sub(r"[^\d.-]", "", v.replace("(", "").replace(")", ""))
                if clean_v and clean_v != "-":
                    try:
                        float(clean_v)
                        parseable_num += 1
                    except ValueError:
                        pass
            num_ratio = parseable_num / total
            if num_ratio < 0.9:
                return 0.0  # Reject non-numeric
            return kw_score * 0.5 + num_ratio * 0.5

        elif field == "entry_date":
            parseable_date = 0
            for v in non_blank:
                if any(char in v for char in ["-", "/", "."]) and any(c.isdigit() for c in v):
                    parseable_date += 1
            date_ratio = parseable_date / total
            if date_ratio < 0.9:
                return 0.0  # Reject non-date
            return kw_score * 0.5 + date_ratio * 0.5

        elif field == "instrument_no":
            distinct_ratio = len(set(non_blank)) / total
            if distinct_ratio < 0.5:
                return 0.0  # Reject non-unique categories like UPI/NEFT
            return kw_score * 0.5 + distinct_ratio * 0.5

        return kw_score

    @classmethod
    def detect_columns(cls, df: pd.DataFrame) -> Dict[str, Optional[str]]:
        mapping = {}
        for field in cls.COLUMN_KEYWORDS.keys():
            best_col = None
            best_score = 0.0
            for col in df.columns:
                values = df[col].tolist()
                score = cls.score_column(field, col, values, df)
                if score > best_score:
                    best_score = score
                    best_col = str(col)
            
            # Refuse auto-select below 0.8 score threshold
            mapping[field] = best_col if best_score >= 0.8 else None
            
        return mapping

    @classmethod
    def preview_file(cls, file_bytes: bytes, filename: str) -> Dict[str, Any]:
        """Read all rows, compute column sample values & column shape analysis."""
        if filename.endswith(".csv"):
            df = pd.read_csv(pd.io.common.BytesIO(file_bytes), dtype=str)
        else:
            df = pd.read_excel(pd.io.common.BytesIO(file_bytes), dtype=str)

        df = df.fillna("")
        columns = [str(c) for c in df.columns]
        detected = cls.detect_columns(df)
        
        # Build 3 sample non-blank values per column
        column_samples = {}
        for col in columns:
            non_blank = [str(v).strip() for v in df[col].tolist() if v is not None and str(v).strip() != "" and not pd.isna(v)]
            column_samples[col] = non_blank[:3]

        # Detect if file looks like a Bank Statement instead of Books Ledger
        headers_lower = [c.lower() for c in columns]
        statement_signals = sum(1 for kw in ["withdrawal", "deposit", "closing balance", "value date", "balance_dr_cr", "nature_of_transaction", "cheque no."] if any(kw in h for h in headers_lower))
        books_signals = sum(1 for kw in ["particulars", "vch type", "vch no", "voucher", "ledger"] if any(kw in h for h in headers_lower))
        is_bank_statement = statement_signals > books_signals and statement_signals >= 2

        all_rows = df.to_dict(orient="records")

        return {
            "columns": columns,
            "detected_mapping": detected,
            "column_samples": column_samples,
            "is_bank_statement": is_bank_statement,
            "sample_rows": all_rows
        }
