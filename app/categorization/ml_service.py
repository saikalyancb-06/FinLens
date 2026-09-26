"""ML prediction service for transaction categorisation.

Loads the artifact produced by `mlmodel/train_categorizer.py` and exposes
predictions with calibrated probabilities and top-3 alternatives. Absence of a
model is a supported state: the service reports unavailability and the hybrid
layer falls back to rules rather than crashing or silently guessing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.categorization.normalizer import normalize_for_ml

logger = logging.getLogger(__name__)

DEFAULT_ARTIFACT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "mlmodel", "artifacts",
)


@dataclass
class MLPrediction:
    predicted_category: Optional[str]
    model_confidence: float
    model_name: str
    top_3: List[Tuple[str, float]] = field(default_factory=list)
    available: bool = True
    error: Optional[str] = None

    # Out-of-distribution signals. A calibrated probability describes confidence
    # *given the training distribution*; it says nothing about an input the model
    # has never seen. These fields let the decision layer tell the two apart.
    known_token_count: int = 0
    total_token_count: int = 0

    @property
    def vocab_coverage(self) -> float:
        if self.total_token_count == 0:
            return 0.0
        return self.known_token_count / self.total_token_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "predicted_category": self.predicted_category,
            "model_confidence": round(self.model_confidence, 4),
            "model_name": self.model_name,
            "top_3": [[c, round(p, 4)] for c, p in self.top_3],
            "top_3_predictions": [c for c, _ in self.top_3],
            "top_3_probabilities": [round(p, 4) for _, p in self.top_3],
            "available": self.available,
            "known_token_count": self.known_token_count,
            "total_token_count": self.total_token_count,
            "vocab_coverage": round(self.vocab_coverage, 4),
        }


class MLCategorizerService:
    def __init__(self, artifact_dir: str = None):
        self.artifact_dir = artifact_dir or os.getenv("CATEGORIZER_ARTIFACT_DIR", DEFAULT_ARTIFACT_DIR)
        self._model = None
        self._metadata: Dict[str, Any] = {}
        self._load_attempted = False
        self._vocab_cache = None

    # -- loading ------------------------------------------------------------

    def _load(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True

        model_path = os.path.join(self.artifact_dir, "categorizer_model.joblib")
        meta_path = os.path.join(self.artifact_dir, "model_metadata.json")

        if not os.path.exists(model_path):
            logger.warning(
                f"[MLCategorizer] No model artifact at '{model_path}'. "
                "Classification will fall back to rules only. "
                "Run: python -m mlmodel.train_categorizer --data <csv>"
            )
            return

        try:
            import joblib
            self._model = joblib.load(model_path)
            if os.path.exists(meta_path):
                import json
                with open(meta_path, encoding="utf-8") as f:
                    self._metadata = json.load(f)
            logger.info(
                f"[MLCategorizer] Loaded '{self.model_name}' "
                f"(test macro F1 = {self._metadata.get('test_metrics', {}).get('macro_f1')})"
            )
        except Exception as exc:
            logger.error(f"[MLCategorizer] Failed to load model: {exc}", exc_info=True)
            self._model = None

    @property
    def model_name(self) -> str:
        return self._metadata.get("model_name", "unknown")

    @property
    def is_available(self) -> bool:
        self._load()
        return self._model is not None

    @property
    def metadata(self) -> Dict[str, Any]:
        self._load()
        return dict(self._metadata)

    # -- out-of-distribution detection --------------------------------------

    @property
    def _vocabulary(self) -> set:
        if self._vocab_cache is None:
            try:
                self._vocab_cache = set(self._model.named_steps["tfidf"].vocabulary_.keys())
            except Exception:
                self._vocab_cache = set()
        return self._vocab_cache

    # Tokens that appear across every category and therefore identify nothing.
    # They are excluded from the coverage count: a narration whose only
    # recognised words are a payment rail and a corporate suffix gives the model
    # no basis to name a counterparty, however confident it reports itself.
    #
    # Note this cannot be derived from IDF on the current training set — "pvt"
    # and "ltd" score idf 6.31 there, ABOVE "swiggy" at 5.31, because the
    # synthetic corpus rarely uses corporate suffixes. The list is domain
    # knowledge about Indian bank narrations, not a statistic.
    NON_DISCRIMINATIVE_TOKENS = {
        # payment rails / instruments
        "neft", "upi", "rtgs", "imps", "ach", "ecs", "pos", "atm", "chq",
        "cheque", "clg", "mmt", "inw", "otw", "dr", "cr",
        # corporate suffixes
        "pvt", "pvtltd", "ltd", "limited", "llp", "inc", "corp", "co",
        "company", "enterprises", "industries", "holdings", "ventures",
        # generic filler
        "india", "indian", "online", "transaction", "account", "bank", "branch",
        "the", "and", "for", "from", "to",
    }
    # Deliberately NOT excluded: "transfer", "payment", "rent", "salary", "fee".
    # Those look generic but each is strongly associated with one category, so
    # discounting them costs real accuracy — measured at ~10 points of
    # auto-decide rate on the benchmark with no accuracy gain.

    def _vocabulary_coverage(self, text: str) -> Tuple[int, int]:
        """How much of this narration the model has actually seen before.

        A TF-IDF model given a narration whose only recognised token is a generic
        one — "upi", "fee" — will still emit a high calibrated probability, because
        calibration is fitted on the training distribution and says nothing about
        unfamiliar input. Measuring coverage is what lets the decision layer
        distinguish "confident because the evidence is strong" from "confident
        because it is extrapolating from one word".
        """
        vocab = self._vocabulary
        if not vocab:
            return 0, 0
        tokens = [t for t in text.lower().split() if t]
        if not tokens:
            return 0, 0
        # Both sides exclude non-discriminative tokens, so a narration made
        # entirely of boilerplate scores 0/0 rather than a misleading 1.0.
        informative = [t for t in tokens if t not in self.NON_DISCRIMINATIVE_TOKENS]
        if not informative:
            return 0, len(tokens)
        known = sum(1 for t in informative if t in vocab)
        return known, len(informative)

    # -- prediction ---------------------------------------------------------

    def predict(self, narration: str) -> MLPrediction:
        self._load()

        if self._model is None:
            return MLPrediction(
                predicted_category=None, model_confidence=0.0,
                model_name="unavailable", available=False,
                error="No trained model artifact",
            )

        text = normalize_for_ml(narration or "")
        if not text.strip():
            return MLPrediction(
                predicted_category=None, model_confidence=0.0,
                model_name=self.model_name, available=True,
                error="Empty narration after normalisation",
            )

        try:
            probabilities = self._model.predict_proba([text])[0]
            classes = list(self._model.classes_)
            ranked = sorted(zip(classes, probabilities), key=lambda kv: kv[1], reverse=True)
            top_3 = [(c, float(p)) for c, p in ranked[:3]]
            known, total = self._vocabulary_coverage(text)
            return MLPrediction(
                predicted_category=top_3[0][0],
                model_confidence=top_3[0][1],
                model_name=self.model_name,
                top_3=top_3,
                known_token_count=known,
                total_token_count=total,
            )
        except Exception as exc:
            logger.error(f"[MLCategorizer] Prediction failed: {exc}", exc_info=True)
            return MLPrediction(
                predicted_category=None, model_confidence=0.0,
                model_name=self.model_name, available=False, error=str(exc),
            )


ml_service = MLCategorizerService()
