import os
import joblib
import numpy as np
from typing import Dict, Any, Tuple

from rules import RuleEngine
from preprocessing import clean_text, extract_derived_features

class TransactionCategorizer:
    """
    Inference Engine implementing Rule Engine -> TF-IDF + Feature Engineering -> ML Model
    with confidence threshold handling.
    """
    def __init__(self, models_dir: str = 'models', confidence_threshold: float = 0.80):
        self.models_dir = models_dir
        self.confidence_threshold = confidence_threshold
        self.rule_engine = RuleEngine()
        
        self.tfidf = None
        self.label_encoder = None
        self.model = None
        
        self._load_artifacts()

    def _load_artifacts(self):
        tfidf_path = os.path.join(self.models_dir, 'tfidf.pkl')
        encoder_path = os.path.join(self.models_dir, 'label_encoder.pkl')
        model_path = os.path.join(self.models_dir, 'model.pkl')

        if os.path.exists(tfidf_path) and os.path.exists(encoder_path) and os.path.exists(model_path):
            self.tfidf = joblib.load(tfidf_path)
            self.label_encoder = joblib.load(encoder_path)
            self.model = joblib.load(model_path)
        else:
            print("Warning: ML model artifacts not fully loaded. Train model first.")

    def predict(self, narration: str, withdrawal: float = 0.0, deposit: float = 0.0) -> Dict[str, Any]:
        """
        Inference Pipeline:
        Input transaction -> Clean text -> Apply rules -> ML Model -> Confidence threshold check
        """
        # Step 1: Clean & Derived features
        extracted = extract_derived_features(narration, withdrawal, deposit)
        cleaned_narration = extracted['cleaned_narration']

        # Step 2: Apply Rules
        rule_match = self.rule_engine.match(narration)
        if rule_match:
            return {
                'category': rule_match['category'],
                'confidence': 1.0,
                'method': 'Rule Engine',
                'rule_id': rule_match['rule_id'],
                'extracted_features': extracted
            }

        # Step 3: ML Model Fallback
        if not self.model or not self.tfidf or not self.label_encoder:
            return {
                'category': 'Unknown',
                'confidence': 0.0,
                'method': 'Model Not Loaded',
                'extracted_features': extracted
            }

        # Vectorize text + append numerical features
        X_tfidf = self.tfidf.transform([cleaned_narration]).toarray()
        X_num = np.array([[extracted['amount'], extracted['is_credit'], extracted['is_debit']]])
        X_input = np.hstack([X_tfidf, X_num])

        # Predict probability
        probs = self.model.predict_proba(X_input)[0]
        max_prob_idx = np.argmax(probs)
        confidence = float(probs[max_prob_idx])
        predicted_class = self.label_encoder.inverse_transform([max_prob_idx])[0]

        # Confidence Threshold Validation (>= 0.80)
        if confidence >= self.confidence_threshold:
            final_category = predicted_class
            method = 'ML Model'
        else:
            final_category = 'Unknown'
            method = 'LLM Fallback Required (Low Confidence)'

        return {
            'category': final_category,
            'confidence': round(confidence, 4),
            'predicted_class_raw': predicted_class,
            'method': method,
            'extracted_features': extracted
        }


def predict_transaction(narration: str, withdrawal: float = 0.0, deposit: float = 0.0) -> Dict[str, Any]:
    categorizer = TransactionCategorizer()
    return categorizer.predict(narration, withdrawal, deposit)


if __name__ == '__main__':
    # Test cases
    test_cases = [
        ("NEFT-YESAP50900722527-RESILIENT INNOVATIONS PVT LTD", 0, 30078),
        ("EBANK:SELF-1481486200-coastal 24 to coastal 108", 357500, 0),
        ("ZOMATO ORDER #4029104", 450, 0),
        ("SWIGGY BANGALORE IN", 320, 0),
        ("AMAZON PAY INDIA", 1200, 0),
        ("UNKNOWN VENDOR TRANSFER FOR SUPPLIES", 5500, 0)
    ]

    categorizer = TransactionCategorizer()
    print("\n--- SAMPLE INFERENCE RUNS ---")
    for narr, w, d in test_cases:
        res = categorizer.predict(narr, w, d)
        print(f"\nNarration: {narr}")
        print(f"Amount: max({w}, {d}) = {max(w, d)}")
        print(f"Predicted Category: {res['category']} | Confidence: {res['confidence']} | Method: {res['method']}")
