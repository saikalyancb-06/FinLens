import os
import joblib
import numpy as np
import pandas as pd

from services.rules import UniversalRuleEngine
from preprocessing import clean_text

class TransactionClassifier:
    def __init__(self, models_dir: str = 'models'):
        self.rule_engine = UniversalRuleEngine()
        self.models_dir = models_dir
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

    def classify(self, narration: str, withdrawal: float = 0.0, deposit: float = 0.0) -> dict:
        # Rule Engine Check
        rule_res = self.rule_engine.evaluate(narration)
        if rule_res:
            return {
                'category': rule_res['category'],
                'confidence': 1.0,
                'is_low_confidence': False,
                'method': 'Rule Engine'
            }

        # ML Model Evaluation
        if not self.model or not self.tfidf or not self.label_encoder:
            return {
                'category': 'Unknown',
                'confidence': 0.0,
                'is_low_confidence': True,
                'method': 'Model Not Loaded'
            }

        cleaned_narr = clean_text(narration)
        amount = max(float(withdrawal), float(deposit))
        is_credit = 1 if float(deposit) > 0 else 0
        is_debit = 1 if float(withdrawal) > 0 else 0

        X_tfidf = self.tfidf.transform([cleaned_narr]).toarray()
        X_num = np.array([[amount, is_credit, is_debit]])
        X_input = np.hstack([X_tfidf, X_num])

        probs = self.model.predict_proba(X_input)[0]
        max_idx = np.argmax(probs)
        confidence = float(probs[max_idx])
        predicted_category = self.label_encoder.inverse_transform([max_idx])[0]

        # Low Confidence Threshold Guardrail (< 0.80 -> Unknown)
        if confidence < 0.80:
            return {
                'category': 'Unknown',
                'confidence': round(confidence, 4),
                'is_low_confidence': True,
                'method': 'ML Model (Low Confidence < 0.80)'
            }

        return {
            'category': predicted_category,
            'confidence': round(confidence, 4),
            'is_low_confidence': False,
            'method': 'ML Model'
        }
