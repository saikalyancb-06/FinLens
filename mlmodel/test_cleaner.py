import unittest
from services.cleaner import clean_currency_str, clean_narration_str

class TestCleanerAndParser(unittest.TestCase):
    
    def test_indian_format_currency_parsing(self):
        """Test Indian Lakh/Crore comma-grouping format and decimal preservation."""
        self.assertEqual(clean_currency_str('2,47,934.98'), 247934.98)
        self.assertEqual(clean_currency_str('33,48,206.00'), 3348206.00)
        self.assertEqual(clean_currency_str('1,10,73,64,312.00'), 1107364312.00)
        self.assertEqual(clean_currency_str('2,47,934.98 Cr'), 247934.98)
        self.assertEqual(clean_currency_str('₹30,078.00'), 30078.00)
        self.assertEqual(clean_currency_str(''), 0.0)

    def test_narration_cleaning(self):
        """Test narration trimming and Cr/Dr suffix removal."""
        self.assertEqual(
            clean_narration_str('NEFT-YESAP50900722527-RESILIENT INNOVATIONS PVT LT'),
            'NEFT-YESAP50900722527-RESILIENT INNOVATIONS PVT LT'
        )
        self.assertEqual(
            clean_narration_str('NEFT-YESAP50900722527-RESILIENT \n INNOVATIONS PVT LT Cr'),
            'NEFT-YESAP50900722527-RESILIENT INNOVATIONS PVT LT'
        )

if __name__ == '__main__':
    unittest.main()
