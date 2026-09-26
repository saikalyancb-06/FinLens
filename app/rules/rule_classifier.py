from typing import Dict, Any, List, Optional
from mlmodel.rules import RuleEngine

class RuleClassifier:
    """
    Wrapper integrating the existing RuleEngine for transaction classification.
    Executes rules prior to any ML model evaluation.
    """
    def __init__(self, default_confidence: float = 0.97):
        self.engine = RuleEngine()
        self.default_confidence = default_confidence

    def classify_transaction(self, txn: Dict[str, Any]) -> Dict[str, Any]:
        """
        Classifies a single normalized transaction dict.
        Returns target output schema:
        {
            "category": str,
            "confidence": float,
            "matched_rule": str,
            "reason": str
        }
        """
        description = str(txn.get("description", ""))
        raw_text = str(txn.get("raw_text", ""))

        # Try matching description first, then raw_text fallback
        match_res = self.engine.match(description)
        if not match_res and raw_text:
            match_res = self.engine.match(raw_text)

        if match_res:
            category = match_res.get("category", "")
            rule_id = match_res.get("rule_id", "")
            pattern = match_res.get("rule_pattern", "")
            
            return {
                "category": category,
                "confidence": self.default_confidence,
                "matched_rule": f"rule_{rule_id}: {pattern}",
                "reason": f"Matched rule pattern '{pattern}' for category '{category}'."
            }

        return {
            "category": "Uncategorized",
            "confidence": 0.0,
            "matched_rule": "none",
            "reason": "No rule matched transaction description."
        }

    def classify_transactions(self, txns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Classifies a list of normalized transactions.
        Attaches 'rule_match' dictionary to each transaction record.
        """
        classified = []
        for txn in txns:
            c_txn = dict(txn)
            rule_result = self.classify_transaction(c_txn)
            c_txn["rule_match"] = rule_result
            classified.append(c_txn)
        return classified
