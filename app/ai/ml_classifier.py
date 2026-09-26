"""ML classifier used by the parsing pipeline's decision engine.

This is a thin adapter over `MLCategorizerService`, the same service the hybrid
categorisation layer uses. Both paths therefore load one artifact, apply one
preprocessing function, and emit one label space.

Previously this class loaded a separate `tfidf.pkl` / `label_encoder.pkl` /
`model.pkl` triple from `mlmodel/models`, preprocessed with `clean_text` while
that model had been fitted through a different transform, and predicted 23
rail-named labels ("NEFT Transfer", "UPI Received", plus a literal "nan" class in
the v2 encoder) that describe how money moved rather than what it was for. It now
predicts the 18 PURPOSE categories from `app.categorization.dual_taxonomy`.

The public surface — `predict(description, amount, debit, credit)` returning
`{category, confidence, top_three}` — is unchanged, so `HybridDecisionEngine` and
everything downstream of it keep working.
"""

import logging
import time
from typing import Any, Dict, Optional

from app.categorization.ml_service import MLCategorizerService, ml_service
from app.config import settings
from app.utils.metrics import metrics_collector

logger = logging.getLogger(__name__)


class MLClassifier:
    """Adapter exposing the purpose classifier in the decision engine's schema."""

    def __init__(self, artifact_dir: Optional[str] = None, models_dir: Optional[str] = None):
        # `models_dir` is the historical parameter name, kept so existing callers
        # do not break. It named the directory of the retired pkl triple, which is
        # not the same thing as an artifact directory, so passing it is a bug
        # worth surfacing rather than silently honouring.
        if models_dir and not artifact_dir:
            logger.warning(
                "[MLClassifier] 'models_dir' is deprecated and points at the retired "
                f"pkl model layout; ignoring '{models_dir}' and loading the categorizer "
                "artifact instead. Pass 'artifact_dir' to override."
            )

        # Reuse the process-wide service unless a specific directory is requested,
        # so the artifact is deserialised once rather than once per pipeline.
        if artifact_dir is None:
            self._service = ml_service
        else:
            self._service = MLCategorizerService(artifact_dir=artifact_dir)

        # Report the directory actually in use, not the one that was asked for.
        self.artifact_dir = self._service.artifact_dir

        if not self._service.is_available:
            logger.warning(
                f"[ML Inference Warning] No categorizer artifact under '{self.artifact_dir}'. "
                "Predictions will return Uncategorized. Run: "
                "python -m mlmodel.train_purpose_classifier --data <csv>"
            )

    @property
    def model_name(self) -> str:
        return self._service.model_name

    @property
    def is_available(self) -> bool:
        return self._service.is_available

    def predict(
        self,
        description: str,
        amount: float = 0.0,
        debit: float = 0.0,
        credit: float = 0.0,
    ) -> Dict[str, Any]:
        """Predict a purpose for one transaction description.

        `amount`, `debit` and `credit` are accepted because callers pass them, but
        the model is narration-only: its two consumers (this engine and the hybrid
        layer) must agree on the feature set, and the hybrid layer has only the
        narration. Direction is still used downstream — the decision engine and
        the review queue both see it — it is simply not a model input.

        Returns:
            {
                "category": str,
                "confidence": float,
                "top_three": [{"category": str, "confidence": float}, ...],
            }
        """
        start_time = time.time()
        prediction = self._service.predict(description or "")

        if not prediction.available or prediction.predicted_category is None:
            if prediction.error:
                logger.debug(f"[ML Inference] No prediction for '{description}': {prediction.error}")
            return {"category": "Uncategorized", "confidence": 0.0, "top_three": []}

        top_three = [
            {"category": str(category), "confidence": round(float(probability), 4)}
            for category, probability in prediction.top_3
        ]

        metrics_collector.record_ml_inference(time.time() - start_time)

        return {
            "category": top_three[0]["category"] if top_three else "Uncategorized",
            "confidence": top_three[0]["confidence"] if top_three else 0.0,
            "top_three": top_three,
            # Out-of-distribution evidence, so a caller that wants to gate on more
            # than a probability can. A calibrated probability describes confidence
            # given the training distribution and says nothing about a narration
            # the model has never seen.
            "known_token_count": prediction.known_token_count,
            "total_token_count": prediction.total_token_count,
            "vocab_coverage": round(prediction.vocab_coverage, 4),
        }
