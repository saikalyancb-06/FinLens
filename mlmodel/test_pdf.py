import pandas as pd
from services.parser.pdf_parser import parse_pdf_file

def test_pdf_parsing(pdf_path: str, ref_csv_path: str):
    print(f"Reading PDF from {pdf_path}...")
    with open(pdf_path, 'rb') as f:
        pdf_bytes = f.read()

    parsed_df = parse_pdf_file(pdf_bytes, pdf_path)
    ref_df = pd.read_csv(ref_csv_path)

    print(f"\nExtracted Rows Count: {len(parsed_df)}")
    print(f"Golden Reference Count: {len(ref_df)}")

    # Check Row 1 exact match
    if not parsed_df.empty:
        r1 = parsed_df.iloc[0]
        print("\n=== ROW 1 EXTRACTED ===")
        print(f"Date:       {r1['date']}")
        print(f"Narration:  {r1['narration']}")
        print(f"Withdrawal: {r1['withdrawal']}")
        print(f"Deposit:    {r1['deposit']}")
        print(f"Balance:    {r1['balance']}")

    # Spot checks
    ebank_rows = parsed_df[parsed_df['narration'].str.contains('EBANK:SELF', case=False, na=False)]
    if not ebank_rows.empty:
        print("\n=== SPOT CHECK: EBANK:SELF ROW ===")
        eb_sample = ebank_rows.iloc[0]
        print(f"Date: {eb_sample['date']} | Narration: {eb_sample['narration']} | Withdrawal: {eb_sample['withdrawal']} | Balance: {eb_sample['balance']}")

    concept_rows = parsed_df[parsed_df['narration'].str.contains('CONCEPT STUDIO', case=False, na=False)]
    if not concept_rows.empty:
        print("\n=== SPOT CHECK: CONCEPT STUDIO LARGE ROW ===")
        cs_sample = concept_rows.iloc[0]
        print(f"Date: {cs_sample['date']} | Narration: {cs_sample['narration']} | Withdrawal: {cs_sample['withdrawal']} | Balance: {cs_sample['balance']}")

if __name__ == '__main__':
    import sys
    pdf_path = 'Current_Account.pdf'
    ref_csv_path = 'golden_reference.csv'
    test_pdf_parsing(pdf_path, ref_csv_path)
