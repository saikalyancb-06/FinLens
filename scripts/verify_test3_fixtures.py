import os
import sys
import re

# Ensure project root is on sys.path for local imports
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mlmodel.financial_parser.parsers import csv_parser as csvp
from app.parsers.pdf_parser import PDFParser

FIX = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'fixtures')
CSV_PATH = os.path.join(FIX, 'books_ledger_test3_nov2026.csv')
PDF_A = os.path.join(FIX, 'bank_stmt_test3a_v2_bank_high.pdf')
PDF_B = os.path.join(FIX, 'bank_stmt_test3b_v2_books_high.pdf')

NUM = re.compile(r'-?\d+[\d,]*\.?\d*')

def parse_amount(s):
    if s is None:
        return 0.0
    ss = str(s)
    m = NUM.search(ss.replace('₹','').replace('Rs',''))
    if not m:
        return 0.0
    return float(m.group(0).replace(',',''))


def main():
    # Parse ledger CSV
    with open(CSV_PATH, 'rb') as f:
        content = f.read()
    recs = csvp.parse_csv_document(content, os.path.basename(CSV_PATH))
    print('Ledger raw records:', recs)

    # Compute net movement: credit - debit (money_in - money_out)
    total_debit = 0.0
    total_credit = 0.0
    for r in recs:
        total_debit += parse_amount(r.get('debit_str', 0))
        total_credit += parse_amount(r.get('credit_str', 0))
    net_movement = total_credit - total_debit
    print(f'Total debit={total_debit}, total credit={total_credit}, net_movement={net_movement}')

    # Book opening = 50000, closing = opening + net_movement
    opening = 50000.0
    book_closing = opening + net_movement
    print(f'Book opening={opening}, book_closing={book_closing}')

    # Parse PDFs
    parser = PDFParser()
    for label, pdf in [('3A', PDF_A), ('3B', PDF_B)]:
        txns = parser.parse(pdf)
        print(f'PDF {label} parsed {len(txns)} txns')
        if txns:
            # compute computed_bank_closing: opening + sum(bank_only adjustments) ???
            # Simpler: computed bank by engine uses book_closing + net_bridge + bank_only
            # But we only need to verify last txn balance is extracted
            last_bal = txns[-1].get('balance') or txns[-1].get('balance_str') or txns[-1].get('balance_str')
            # try numeric
            last_bal_val = parse_amount(last_bal)
            print(f'PDF {label} last txn balance parsed as {last_bal_val}')
            # Also compute sum of transaction amounts applied to opening (to check computed)
            amt_sum = 0.0
            for t in txns:
                d = parse_amount(t.get('debit_str') or t.get('debit') or t.get('debit_str'))
                c = parse_amount(t.get('credit_str') or t.get('credit') or t.get('credit_str'))
                # assume credit increases balance
                amt_sum += (c - d)
            computed_by_tx = opening + amt_sum
            print(f'PDF {label} computed by tx arithmetic (opening+sum(c-d)) = {computed_by_tx}')
            residual = last_bal_val - computed_by_tx
            print(f'PDF {label} residual = {residual}')

            # Check no single transaction equals the residual amount
            abs_res = abs(residual)
            tx_amounts = [abs(parse_amount(t.get('debit_str') or t.get('debit') or t.get('debit_str')) - parse_amount(t.get('credit_str') or t.get('credit') or t.get('credit_str'))) for t in txns]
            matches = [a for a in tx_amounts if abs(a - abs_res) < 0.01]
            if matches:
                print(f'WARNING: Found transaction(s) matching residual amount: {matches}')
            else:
                print(f'No single transaction matches residual {abs_res}')

    # Residual verification as per user's requirements:
    # For these fixtures, computed_bank must be 65000 for both? Actually design expects computed 65000 for 3A and 40k for 3B.

if __name__ == '__main__':
    main()
