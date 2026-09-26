import unittest
import pandas as pd
from services.cleaner import clean_currency_str, normalize_date_str, clean_narration_str
from services.transaction_builder import validate_and_build_transaction_df

class TestProductionFinancialParser(unittest.TestCase):

    def test_indian_format_currency_cleaning(self):
        """Verify Indian lakh/crore formatting without x100 inflation."""
        self.assertEqual(clean_currency_str('2,47,934.98'), 247934.98)
        self.assertEqual(clean_currency_str('33,48,206.00'), 3348206.00)
        self.assertEqual(clean_currency_str('1,10,73,64,312.00'), 1107364312.00)
        self.assertEqual(clean_currency_str('2,47,934.98 Cr'), 247934.98)
        self.assertEqual(clean_currency_str('₹30,078.00'), 30078.00)

    def test_date_normalization(self):
        """Verify DD/MM/YYYY, DD-MM-YYYY, YYYY-MM-DD -> YYYY-MM-DD."""
        self.assertEqual(normalize_date_str('31/03/2025'), '2025-03-31')
        self.assertEqual(normalize_date_str('31-03-2025'), '2025-03-31')
        self.assertEqual(normalize_date_str('2025-03-31'), '2025-03-31')

    def test_transaction_builder_validation(self):
        """Verify row validation and standard output schema."""
        raw_data = pd.DataFrame([
            {'date': '31/03/2025', 'narration': 'NEFT CREDIT', 'withdrawal': 0, 'deposit': '30,078.00', 'balance': '2,47,934.98'},
            {'date': '30/03/2025', 'narration': 'INVALID ROW NO MONETARY VAL', 'withdrawal': 0, 'deposit': 0, 'balance': 0},
            {'date': '', 'narration': 'MISSING DATE', 'withdrawal': 100, 'deposit': 0, 'balance': 100}
        ])

        clean_df = validate_and_build_transaction_df(raw_data)
        
        # Validates exact output schema
        self.assertEqual(list(clean_df.columns), ['date', 'narration', 'withdrawal', 'deposit', 'balance'])
        
        # Only row 1 should pass validation
        self.assertEqual(len(clean_df), 1)
        r1 = clean_df.iloc[0]
        self.assertEqual(r1['date'], '2025-03-31')
        self.assertEqual(r1['narration'], 'NEFT CREDIT')
        self.assertEqual(r1['withdrawal'], 0.0)
        self.assertEqual(r1['deposit'], 30078.00)
        self.assertEqual(r1['balance'], 247934.98)

if __name__ == '__main__':
    unittest.main()
