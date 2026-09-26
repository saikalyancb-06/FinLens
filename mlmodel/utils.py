import os
import pandas as pd
from typing import List, Dict, Any
from predict import TransactionCategorizer

def batch_predict(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Utility function for batch processing structured banking transactions.
    """
    categorizer = TransactionCategorizer()
    results = []
    for tx in transactions:
        narr = tx.get('narration', '')
        w = tx.get('withdrawal', 0.0)
        d = tx.get('deposit', 0.0)
        res = categorizer.predict(narr, w, d)
        results.append(res)
    return results

def format_prediction_output(result: Dict[str, Any]) -> str:
    """Formats output for logging or API response."""
    return f"Category: {result['category']} | Confidence: {result['confidence']} | Method: {result['method']}"
