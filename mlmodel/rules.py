import re
from typing import Optional, Dict, Any

class RuleEngine:
    """
    Executes business rules prior to ML classification.
    Easily extensible with additional rules.
    """
    def __init__(self):
        # Priority-ordered rule definitions
        # Each rule: (pattern, category, rule_id)
        self.rules = [
            (r'ebank\s*:\s*self|ac\s*xfr\s*from\s*sol', 'Internal Fund Transfer', 1),
            (r'resilient\s*innovations', 'Customer Payment / NEFT Transfer', 2),
            (r'bharatpeppg|bharatpe\s*payouts|bharatpe', 'Merchant Settlement', 3),
            (r'disbursement\s*credit', 'Loan Disbursement', 4),
            (r'loan\s*recovery', 'Loan Repayment / EMI', 5),
            (r'concept\s*studio|final\s*payment', 'Vendor Payment', 6),
            (r'ledger\s*folio\s*charges|sms\s*alert', 'Bank Charges', 7),
            (r'cgtmse\s*fee|cgtmse', 'Government Fee', 8),
            (r'swiggy|zomato', 'Food & Dining', 9),
            (r'indian\s*oil|hpcl', 'Fuel', 10),
            (r'amazon|flipkart', 'POS/Card Purchase', 11),
            (r'irctc', 'Travel', 12),
            (r'bescom|bwssb', 'Utility Payment', 13),
            (r'imps/', 'IMPS Transfer', 14),
            (r'neft-', 'NEFT Transfer', 15),
            (r'rtgs-', 'RTGS Transfer', 16),
            (r'upi/', 'UPI Transfer', 17),
            (r'\bgst\b|\btds\b', 'GST/Tax Payment', 18),
            (r'salary', 'Salary Payment', 19),
            (r'cash\s*deposit', 'Cash Deposit', 20),
            (r'cash\s*withdrawal|\batm\b', 'ATM Withdrawal', 21),
            (r'interest\s*credit', 'Interest Credit', 22),
            (r'interest\s*debit', 'Interest Debit', 23),
            (r'dividend', 'Dividend', 24),
            (r'insurance', 'Insurance Premium', 25),
            (r'rent', 'Rent Payment', 26),
            (r'refund', 'Refund', 27),
            (r'reversal', 'Reversal', 28)
        ]

    def match(self, narration: str) -> Optional[Dict[str, Any]]:
        """
        Evaluates rules against narration string.
        Returns a dict with category and matched rule ID if matched, else None.
        """
        if not narration or not isinstance(narration, str):
            return None
            
        narration_clean = narration.lower()
        for pattern, category, rule_id in self.rules:
            if re.search(pattern, narration_clean):
                return {
                    'category': category,
                    'rule_id': rule_id,
                    'rule_pattern': pattern
                }
        return None

    def add_rule(self, pattern: str, category: str, priority: Optional[int] = None):
        """Extensible method to add new rules dynamically."""
        rule_tuple = (pattern, category, len(self.rules) + 1)
        if priority is not None and 0 <= priority < len(self.rules):
            self.rules.insert(priority, rule_tuple)
        else:
            self.rules.append(rule_tuple)
