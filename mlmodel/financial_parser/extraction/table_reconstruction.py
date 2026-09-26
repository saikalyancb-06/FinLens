import re
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

def reconstruct_table_from_word_coords(words_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Robust Table Reconstruction Engine for Digital PDFs.
    Uses per-page dynamic column boundary estimation, Y-coordinate clustering,
    and row group clustering to prevent multi-line / page overlap corruptions.
    """
    if not words_list:
        return []

    date_regex = re.compile(r'^(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})$')

    # Group words by page
    words_by_page: Dict[int, List[Dict[str, Any]]] = {}
    for w in words_list:
        p = w.get('page', 1)
        words_by_page.setdefault(p, []).append(w)

    transactions = []

    last_valid_header_x = None

    for page, p_words in sorted(words_by_page.items()):
        # Cluster line items by Y-coordinate with 2.5pt line tolerance
        lines_by_y: Dict[float, List[Dict[str, Any]]] = {}
        for w in p_words:
            y_val = w['y0']
            matched_key = None
            for k in lines_by_y.keys():
                if abs(k - y_val) <= 2.5:
                    matched_key = k
                    break
            if matched_key is None:
                matched_key = round(y_val, 1)
                lines_by_y[matched_key] = []
            lines_by_y[matched_key].append(w)

        sorted_keys = sorted(lines_by_y.keys())

        # Detect page headers dynamically
        has_type_col = False
        header_x = dict(last_valid_header_x) if last_valid_header_x else {
            'date': (0, 80),
            'description': (80, 280),
            'debit': (280, 360),
            'credit': (360, 440),
            'balance': (440, 600)
        }

        for k in sorted_keys:
            words_in_line = sorted(lines_by_y[k], key=lambda x: x['x0'])
            line_str = " ".join([w['text'] for w in words_in_line]).upper()

            if ('DATE' in line_str or 'TRAN' in line_str) and any(kw in line_str for kw in ['NARRATION', 'PARTICULAR', 'DESCRIPTION', 'REFERENCE', 'AMOUNT', 'BALANCE', 'WITHDRAWAL', 'DEPOSIT']):
                has_debit_credit_header = any(kw in line_str for kw in ['WITHDRAWAL', 'DEBIT', 'DEPOSIT', 'CREDIT', 'PAID OUT', 'PAID IN'])
                type_word = None
                amt_word = None
                bal_word = None
                date_word = None
                desc_word = None
                deb_word = None
                cred_word = None
                
                for w in words_in_line:
                    t = w['text'].upper()
                    if 'DATE' in t and date_word is None:
                        date_word = w
                    elif any(kw in t for kw in ['NARRATION', 'PARTICULAR', 'DESCRIPTION', 'DETAILS', 'REFERENCE']) and desc_word is None:
                        desc_word = w
                    elif any(kw in t for kw in ['WITHDRAWAL', 'DEBIT', 'DR', 'PAID OUT']) and deb_word is None:
                        deb_word = w
                    elif any(kw in t for kw in ['DEPOSIT', 'CREDIT', 'CR', 'PAID IN']) and cred_word is None:
                        cred_word = w
                    elif 'AMOUNT' in t and amt_word is None:
                        amt_word = w
                    elif 'TYPE' in t and type_word is None:
                        type_word = w
                    elif ('BALANCE' in t or 'RUNNING' in t) and bal_word is None:
                        bal_word = w

                if date_word and deb_word and cred_word and bal_word:
                    d_end = date_word['x1'] + 15
                    deb_s = deb_word['x0'] - 15
                    cred_s = cred_word['x0'] - 15
                    bal_s = bal_word['x0'] - 15
                    header_x['date'] = (0, d_end)
                    header_x['description'] = (d_end, deb_s)
                    header_x['debit'] = (deb_s, cred_s)
                    header_x['credit'] = (cred_s, bal_s)
                    header_x['balance'] = (bal_s, 2000.0)
                    last_valid_header_x = dict(header_x)
                elif not has_debit_credit_header and amt_word is not None and bal_word is not None:
                    # Single Amount column + Type column + Balance column structure
                    # e.g., Date | Reference | Amount | Type | Running Balance
                    has_type_col = True
                    amt_x = amt_word['x0']
                    bal_x = bal_word['x0']
                    type_x = type_word['x0'] if type_word else (amt_x + bal_x) / 2.0
                    header_x['debit'] = (amt_x - 30, type_x)      # Amount column
                    header_x['credit'] = (type_x, bal_x - 10)     # Type column (CR/DR)
                    header_x['balance'] = (bal_x - 10, 800)       # Running Balance column
                    last_valid_header_x = dict(header_x)
                break

        # Group consecutive Y-lines into logical transaction row groups
        row_groups = []
        current_group = []

        for k in sorted_keys:
            words_in_line = sorted(lines_by_y[k], key=lambda x: x['x0'])
            line_text = " ".join([w['text'] for w in words_in_line]).strip()
            upper = line_text.upper()

            if not line_text:
                continue

            if 'STATEMENT OF' in upper or 'PAGE ' in upper or ('TRAN' in upper and 'DATE' in upper) or 'YOUR ACCOUNT' in upper or '*THIS IS' in upper or 'MAIN ACCOUNT' in upper or 'JOINT ACCOUNT' in upper:
                continue

            first_w = words_in_line[0]['text'].strip()
            if date_regex.match(first_w) and words_in_line[0]['x0'] < header_x['description'][1]:
                if current_group:
                    row_groups.append(current_group)
                current_group = [words_in_line]
            else:
                if current_group:
                    current_group.append(words_in_line)

        if current_group:
            row_groups.append(current_group)

        # Assemble transaction dicts from row groups
        for grp in row_groups:
            first_line = grp[0]
            tx_date = first_line[0]['text'].strip()

            desc_tokens = []
            debit_tokens = []
            credit_tokens = []
            balance_tokens = []
            type_tokens = []

            for line in grp:
                for w in line:
                    xc = (w['x0'] + w['x1']) / 2.0
                    t = w['text'].strip()

                    if xc < header_x['date'][1]:
                        pass # Date column
                    elif header_x['description'][0] <= xc < header_x['debit'][0]:
                        desc_tokens.append(t)
                    elif header_x['debit'][0] <= xc < header_x['credit'][0]:
                        debit_tokens.append(t)
                    elif header_x['credit'][0] <= xc < header_x['balance'][0]:
                        if has_type_col:
                            type_tokens.append(t)
                        else:
                            credit_tokens.append(t)
                    else:
                        balance_tokens.append(t)

            clean_desc = " ".join(desc_tokens)
            clean_desc = re.sub(r'^(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\s*', '', clean_desc).strip()

            if has_type_col:
                # debit_tokens contains the amount string, type_tokens contains CR/DR
                amt_str = " ".join(debit_tokens)
                type_str = " ".join(type_tokens).upper()
                
                # Check description or type_tokens for CR/DR
                is_cr = 'CR' in type_str or 'CREDIT' in type_str or re.search(r'\bCR\b', clean_desc)
                is_dr = 'DR' in type_str or 'DEBIT' in type_str or re.search(r'\bDR\b', clean_desc)

                if is_cr and not is_dr:
                    credit_tokens = [amt_str]
                    debit_tokens = []
                else:
                    debit_tokens = [amt_str]
                    credit_tokens = []

            transactions.append({
                'page_number': page,
                'date': tx_date,
                'description': clean_desc,
                'debit_str': " ".join(debit_tokens),
                'credit_str': " ".join(credit_tokens),
                'balance_str': " ".join(balance_tokens)
            })

    return transactions
