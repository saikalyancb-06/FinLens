"""Tunable thresholds for the hybrid categorisation decision layer.

Everything here is environment-overridable. None of these values are baked into
the decision logic, which is the point: the previous system hardcoded
`default_confidence = 0.97` and an unreachable ML gate, so behaviour could not
be tuned without editing code.
"""

from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class CategorizationConfig:
    # ---- Rule engine thresholds ------------------------------------------
    # >= HIGH           : rule is trusted outright
    # MEDIUM .. HIGH-1  : rule is evidence, but ML is consulted
    # < MEDIUM          : rule is ignored as a decision, ML decides
    RULE_SCORE_HIGH: float = _f("RULE_SCORE_HIGH", 80)
    RULE_SCORE_MEDIUM: float = _f("RULE_SCORE_MEDIUM", 50)

    # ---- ML thresholds ----------------------------------------------------
    # Minimum calibrated probability for an ML prediction to be accepted.
    ML_CONFIDENCE_ACCEPT: float = _f("ML_CONFIDENCE_ACCEPT", 0.70)
    # Probability above which ML is considered strong enough to outweigh a
    # merely-medium rule match on disagreement.
    ML_CONFIDENCE_STRONG: float = _f("ML_CONFIDENCE_STRONG", 0.85)

    # ---- Out-of-distribution guard ---------------------------------------
    # A calibrated probability is only meaningful for inputs resembling the
    # training data. Measured on a real out-of-distribution set, this model
    # returned 0.98 for "DOCTOR CONSULTATION FEE PAID" -> Bank Charges on the
    # strength of the single token "fee", and mapped five unseen local merchants
    # to Transfers from "upi"/"neft" alone. Requiring a minimum amount of
    # recognised evidence separates genuine confidence from extrapolation.
    ML_MIN_KNOWN_TOKENS: int = int(_f("ML_MIN_KNOWN_TOKENS", 2))
    ML_MIN_VOCAB_COVERAGE: float = _f("ML_MIN_VOCAB_COVERAGE", 0.5)

    # ---- Conflict resolution ---------------------------------------------
    # When a high-scoring rule and a confident ML prediction disagree, the rule
    # wins unless ML clears ML_CONFIDENCE_STRONG. Deterministic rules are more
    # auditable than a model, so they get the benefit of the doubt.
    PREFER_RULE_ON_TIE: bool = os.getenv("PREFER_RULE_ON_TIE", "true").lower() == "true"

    # Confidence reported for a high-confidence rule match. Derived from the
    # rule score rather than fixed, so a tier-1 merchant match and a tier-3
    # keyword match do not claim identical certainty.
    RULE_SCORE_TO_CONFIDENCE_DIVISOR: float = _f("RULE_SCORE_TO_CONFIDENCE_DIVISOR", 110.0)

    @classmethod
    def rule_confidence(cls, rule_score: float) -> float:
        """Map a rule score onto a 0-1 confidence, capped at 0.99.

        Never returns 1.0: a rule match is strong evidence, not proof.
        """
        if rule_score <= 0:
            return 0.0
        return round(min(rule_score / cls.RULE_SCORE_TO_CONFIDENCE_DIVISOR, 0.99), 4)

    @classmethod
    def as_dict(cls) -> dict:
        return {
            "RULE_SCORE_HIGH": cls.RULE_SCORE_HIGH,
            "RULE_SCORE_MEDIUM": cls.RULE_SCORE_MEDIUM,
            "ML_CONFIDENCE_ACCEPT": cls.ML_CONFIDENCE_ACCEPT,
            "ML_CONFIDENCE_STRONG": cls.ML_CONFIDENCE_STRONG,
            "PREFER_RULE_ON_TIE": cls.PREFER_RULE_ON_TIE,
        }


config = CategorizationConfig()
