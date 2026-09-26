import unittest
import pandas as pd
from services.cleaner import clean_currency_str, clean_narration_str, clean_transaction_df
from services.normalizer import normalize_to_schema

class TestFinancialParserFixes(unittest.TestCase):

    def test_indian_format_currency_parsing(self):
        """
        Fix 1: Numeric parsing must preserve decimal points for Indian lakh/crore formats
        without x100 inflation.
        """
        self.assertEqual(clean_currency_str('2,47,934.98'), 247934.98)
        self.assertEqual(clean_currency_str('33,48,206.00'), 3348206.00)
        self.assertEqual(clean_currency_str('1,10,73,64,312.00'), 1107364312.00)
        self.assertEqual(clean_currency_str('2,47,934.98 Cr'), 247934.98)
        self.assertEqual(clean_currency_str('₹30,078.00'), 30078.00)
        self.assertEqual(clean_currency_str(''), 0.0)

    def test_golden_reference_row1(self):
        """
        Fix validation against Golden Reference Row 1:
        Date: 31/03/2025
        Narration: NEFT-YESAP50900722527-RESILIENT INNOVATIONS PVT LT
        Deposit: 30078.00
        Withdrawal: 0.0
        Balance: 247934.98
        """
        ref_df = pd.read_csv('golden_reference.csv')
        self.assertEqual(len(ref_df), 410)

        row1 = ref_df.iloc[0]
        self.assertEqual(row1['date'], '31/03/2025')
        self.assertEqual(row1['narration'], 'NEFT-YESAP50900722527-RESILIENT INNOVATIONS PVT LT')
        self.assertEqual(clean_currency_str(row1['deposit']), 30078.00)
        self.assertEqual(clean_currency_str(row1['withdrawal']), 0.00)
        self.assertEqual(clean_currency_str(row1['balance']), 247934.98)
        self.assertEqual(row1['balance_dr_cr'], 'Cr')

    def test_spot_check_ebank_self(self):
        """
        Fix validation: EBANK:SELF withdrawal spot check
        Date: 29/03/2025
        Withdrawal: 100000.00
        Balance: 205396.98
        """
        ref_df = pd.read_csv('golden_reference.csv')
        row_ebank = ref_df[ref_df['narration'].str.contains('EBANK:SELF/1448664605', case=False)].iloc[0]
        self.assertEqual(clean_currency_str(row_ebank['withdrawal']), 100000.00)
        self.assertEqual(clean_currency_str(row_ebank['deposit']), 0.00)
        self.assertEqual(clean_currency_str(row_ebank['balance']), 205396.98)

    def test_spot_check_large_concept_studio(self):
        """
        Fix validation: Large-amount row spot check
        Date: 26/03/2025
        Withdrawal: 3348206.00
        Balance: 244150.98
        """
        ref_df = pd.read_csv('golden_reference.csv')
        row_cs = ref_df[ref_df['narration'].str.contains('CONSEPT STUDIO FINAL PAYMENT', case=False)].iloc[0]
        self.assertEqual(clean_currency_str(row_cs['withdrawal']), 3348206.00)
        self.assertEqual(clean_currency_str(row_cs['balance']), 244150.98)

if __name__ == '__main__':
    unittest.main()
