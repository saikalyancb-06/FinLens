import re
from typing import Optional, Dict, Any

class UniversalRuleEngine:
    """
    Executes Rule Engine before ML.
    If matched -> returns category, skips ML.
    """
    def __init__(self):
        self.rules = [
            (r'bharatpe', 'BharatPe Payout'),
            (r'ebank\s*:\s*self', 'Self Transfer'),
            (r'loan\s*recovery', 'Loan Recovery'),
            (r'cgtmse', 'Bank Charges'),
            (r'sms\s*alert', 'Bank Charges'),
            (r'salary', 'Salary'),
            (r'interest\s*credit', 'Interest'),
            (r'swiggy', 'Food'),
            (r'zomato', 'Food'),
            (r'indian\s*oil', 'Fuel'),
            (r'hpcl', 'Fuel'),
            (r'amazon', 'Shopping'),
            (r'flipkart', 'Shopping'),
            (r'irctc', 'Travel'),
            (r'bescom', 'Utilities'),
            (r'bwssb', 'Utilities'),
            (r'concept\s*studio', 'Vendor Payment'),
            (r'resilient\s*innovations', 'Settlement')
        ]

    def evaluate(self, narration: str) -> Optional[Dict[str, Any]]:
        if not narration or not isinstance(narration, str):
            return None
            
        narr_clean = narration.lower()
        for pattern, category in self.rules:
            if re.search(pattern, narr_clean):
                return {
                    'category': category,
                    'confidence': 1.0,
                    'rule_matched': True,
                    'pattern': pattern
                }
        return None
