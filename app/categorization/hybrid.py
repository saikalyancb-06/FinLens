"""Hybrid rule + ML decision layer.

Combines the deterministic rule engine with the ML classifier and decides which
to trust, refusing to decide when neither is convincing.

Provenance is the other job of this module. The result keeps two questions
strictly apart:

    source_channel        WHERE did the transaction come from? (upload/gmail/…)
    classification_method HOW was the category decided?        (rule/ml/hybrid/manual)

The old code conflated these — `prediction_source` was filled with the ingestion
channel — which made it impossible to answer either question reliably.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.categorization.config import config
from app.categorization.purpose_rules import derive_result as derive_purpose
from app.categorization import trades
from app.categorization.ml_service import MLPrediction, ml_service
from app.categorization.rule_engine import RuleResult, rule_engine
from app.categorization.taxonomy import UNCATEGORIZED


# Classification methods (the HOW).
METHOD_RULE = "rule"
METHOD_ML = "ml"
METHOD_HYBRID = "hybrid"
METHOD_MANUAL = "manual"
METHOD_NONE = "none"


# Rules whose answer a saved counterparty decision is allowed to replace.
# Anything else — a bank charge, a tax challan, a recognised merchant — is a
# fact about the TRANSACTION rather than about who was paid, and a counterparty
# decision must not reach it.
_OVERRIDABLE_RULE_PREFIXES = ("trade_name:",)


def memory_should_override(classification_rule, requires_review: bool) -> bool:
    """Should a saved counterparty decision replace this answer?

    Yes when the classifier abstained — that is what the memory is for.

    And yes when the answer came from the counterparty's TRADE NAME, which is
    the case this function exists for. Trade names answer confidently, so
    without this a user who taught the system that "KUMAR FISH" is Professional
    Fees would have had that silently overruled on the very next upload. A
    keyword must never outrank a person who already answered.

    No when a rule matched the narration itself. "GST PAYMENT" is a property of
    the transaction; who was paid does not change it.

    Lives here rather than in `counterparty_memory` because it is pure policy
    and that module is the one that opens a session — the separation those two
    modules already document.
    """
    if requires_review:
        return True
    return str(classification_rule or "").startswith(_OVERRIDABLE_RULE_PREFIXES)


@dataclass
class ClassificationResult:
    category: str
    requires_review: bool
    classification_method: str
    classification_confidence: float
    explanation: str

    classification_rule: Optional[str] = None
    rule_score: int = 0
    rule_category: Optional[str] = None
    model_name: Optional[str] = None
    model_confidence: float = 0.0
    ml_category: Optional[str] = None
    top_3: List[Tuple[str, float]] = field(default_factory=list)
    agreement: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "requires_review": self.requires_review,
            "classification_method": self.classification_method,
            "classification_rule": self.classification_rule,
            "classification_confidence": round(self.classification_confidence, 4),
            "model_name": self.model_name,
            "model_confidence": round(self.model_confidence, 4),
            "rule_score": self.rule_score,
            "rule_category": self.rule_category,
            "ml_category": self.ml_category,
            "top_3": [[c, round(p, 4)] for c, p in self.top_3],
            "agreement": self.agreement,
            "explanation": self.explanation,
        }


def _rule_phrase(rule: RuleResult) -> str:
    """Human-readable description of what the rule matched, preserving casing.

    Merchant names must keep their original casing — lowercasing the whole
    explanation turns "SWIGGY" into "swiggy" and reads like a typo to users.
    """
    terms = ", ".join(rule.matched_terms) if rule.matched_terms else "a configured pattern"
    label = {
        "intent_phrase": f"the transaction-intent phrase {terms}",
        "exact_merchant": f"the {terms} merchant rule",
        "strong_phrase": f"the phrase {terms}",
        "strong_keyword": f"the keyword {terms}",
        "contextual": f"the contextual combination {terms}",
        "weak_keyword": f"the weak keyword {terms}",
    }.get(rule.rule_type, f"rule {rule.matched_rule}")
    return label


def _needs_review(category: str) -> bool:
    return category == UNCATEGORIZED


def _unresolved(rule: RuleResult, ml: MLPrediction, reason: str) -> ClassificationResult:
    """Refuse to name a purpose.

    This is reached only after every source of evidence has been tried and none
    of them said what the money was FOR. The flat axis has one honest answer for
    that and it is `UNCATEGORIZED` — not the model's leftover guess.

    The distinction matters because the guard immediately above this is what
    sent most rows here: `ml_in_distribution` decided the narration is unlike
    anything the model was trained on, so its probability is not interpretable.
    Publishing `ml.predicted_category` anyway takes a number the code just
    declared meaningless and prints it as a decision — "QRTX SETTLEMENT BATCH
    1234 is Sales Income, confidence 0.98" off a single recognised token. That
    is precisely the out-of-distribution failure the guard exists to stop, and
    it violates the project's rule: classify only what the transaction data
    supports.

    It is also not the same thing as the residual placement. A residual
    placement (`hierarchy.residual_path`, applied in `deep.py` step 7) restates
    the RAIL and the DIRECTION, which are printed on the statement and are
    therefore facts. `Transportation` is not a fact about `ZZQQ8891 XKQP`; it is
    a guess wearing a fact's clothes. The tree gets the honest floor answer only
    when this function abstains — `deep.anchor_path` lists "uncategorized" in
    `UNMAPPABLE_ANCHORS` exactly so an abstention here falls through to the
    residual, and a fabricated category here would out-vote it.

    The model's opinion is not thrown away: it stays in `ml_category`, `top_3`
    and `model_confidence` for the reviewer to see. It just does not get to be
    the answer, and `classification_confidence` reports the confidence of the
    DECISION, which is zero, rather than the confidence of a rejected guess.
    """
    if ml.top_3:
        top_str = ", ".join(f"{c} ({p:.2f})" for c, p in ml.top_3)
        explanation = (
            f"Not categorized automatically. {reason} "
            f"Top predictions were {top_str}. Manual review required."
        )
    else:
        explanation = f"Not categorized automatically. {reason} Manual review required."

    return ClassificationResult(
        category=UNCATEGORIZED,
        requires_review=True,
        classification_method=METHOD_NONE,
        classification_confidence=0.0,
        explanation=explanation,
        classification_rule=rule.matched_rule,
        rule_score=rule.rule_score,
        rule_category=rule.category,
        model_name=ml.model_name if ml.available else None,
        model_confidence=ml.model_confidence,
        ml_category=ml.predicted_category,
        top_3=ml.top_3,
        agreement="unresolved",
    )


def classify_transaction(
    narration: str,
    amount: Optional[float] = None,
    direction: Optional[str] = None,
    cfg=config,
    account_type: Optional[str] = None,
) -> ClassificationResult:
    """Classify one transaction using rules first, then ML, then abstain."""
    rule = rule_engine.classify(narration, amount=amount, direction=direction)
    ml = ml_service.predict(narration)

    rule_conf = cfg.rule_confidence(rule.rule_score)
    has_rule = rule.category is not None
    has_ml = ml.available and ml.predicted_category is not None

    # Out-of-distribution guard. The model's probability is only interpretable
    # for input resembling its training data; on unfamiliar narrations it stays
    # confident while extrapolating from one generic token. Without enough
    # recognised evidence its opinion is not admitted at all, regardless of the
    # reported probability.
    ml_in_distribution = (
        has_ml
        and ml.known_token_count >= cfg.ML_MIN_KNOWN_TOKENS
        and ml.vocab_coverage >= cfg.ML_MIN_VOCAB_COVERAGE
    )

    rule_is_high = has_rule and rule.rule_score >= cfg.RULE_SCORE_HIGH and not rule.is_ambiguous
    rule_is_medium = has_rule and cfg.RULE_SCORE_MEDIUM <= rule.rule_score < cfg.RULE_SCORE_HIGH
    ml_accepted = ml_in_distribution and ml.model_confidence >= cfg.ML_CONFIDENCE_ACCEPT
    ml_strong = ml_in_distribution and ml.model_confidence >= cfg.ML_CONFIDENCE_STRONG

    # ---- Case 1: high-confidence rule ------------------------------------
    if rule_is_high:
        agrees = has_ml and ml.predicted_category == rule.category
        if agrees:
            # Both agree: highest confidence available, and the method reflects
            # that two independent signals concurred.
            confidence = max(rule_conf, ml.model_confidence)
            return ClassificationResult(
                category=rule.category,
                requires_review=False,
                classification_method=METHOD_HYBRID,
                classification_confidence=confidence,
                explanation=(
                    f"Categorized as {rule.category} because the narration matched "
                    f"{_rule_phrase(rule)}, confirmed by the {ml.model_name} model at "
                    f"{ml.model_confidence:.2f} confidence."
                ),
                classification_rule=rule.matched_rule,
                rule_score=rule.rule_score,
                rule_category=rule.category,
                model_name=ml.model_name,
                model_confidence=ml.model_confidence,
                ml_category=ml.predicted_category,
                top_3=ml.top_3,
                agreement="agree",
            )

        # Disagreement, or no ML available. A strong deterministic rule is more
        # auditable than a model, so it wins unless ML is very confident.
        if has_ml and ml_strong and not cfg.PREFER_RULE_ON_TIE:
            pass  # fall through to ML below
        elif has_ml and ml_strong and rule.rule_score < cfg.RULE_SCORE_HIGH + 20:
            # Medium-strength "high" rule vs a very confident model: not safe to
            # auto-accept either side.
            return _unresolved(
                rule, ml,
                f"Rule matched {rule.category} (score {rule.rule_score}) but the model "
                f"predicted {ml.predicted_category} at {ml.model_confidence:.2f}.",
            )

        return ClassificationResult(
            category=rule.category,
            requires_review=False,
            classification_method=METHOD_RULE,
            classification_confidence=rule_conf,
            explanation=f"Categorized as {rule.category}. {rule.explanation}",
            classification_rule=rule.matched_rule,
            rule_score=rule.rule_score,
            rule_category=rule.category,
            model_name=ml.model_name if ml.available else None,
            model_confidence=ml.model_confidence,
            ml_category=ml.predicted_category,
            top_3=ml.top_3,
            agreement="disagree" if has_ml else "rule_only",
        )

    # ---- Case 2: medium rule — ML is the tie-breaker ----------------------
    if rule_is_medium:
        if has_ml and ml.predicted_category == rule.category and ml_accepted:
            return ClassificationResult(
                category=rule.category,
                requires_review=False,
                classification_method=METHOD_HYBRID,
                classification_confidence=max(rule_conf, ml.model_confidence),
                explanation=(
                    f"Categorized as {rule.category}: a medium-strength rule "
                    f"({rule.matched_rule}, score {rule.rule_score}) was confirmed by the "
                    f"{ml.model_name} model at {ml.model_confidence:.2f} confidence."
                ),
                classification_rule=rule.matched_rule,
                rule_score=rule.rule_score,
                rule_category=rule.category,
                model_name=ml.model_name,
                model_confidence=ml.model_confidence,
                ml_category=ml.predicted_category,
                top_3=ml.top_3,
                agreement="agree",
            )

        if ml_strong:
            return ClassificationResult(
                category=ml.predicted_category,
                requires_review=False,
                classification_method=METHOD_ML,
                classification_confidence=ml.model_confidence,
                explanation=(
                    f"Categorized as {ml.predicted_category} by the {ml.model_name} model "
                    f"with {ml.model_confidence:.2f} confidence, outweighing a weaker rule "
                    f"match for {rule.category} (score {rule.rule_score})."
                ),
                classification_rule=rule.matched_rule,
                rule_score=rule.rule_score,
                rule_category=rule.category,
                model_name=ml.model_name,
                model_confidence=ml.model_confidence,
                ml_category=ml.predicted_category,
                top_3=ml.top_3,
                agreement="disagree",
            )

        return _last_resort(
            narration, direction, account_type, rule, ml,
            f"Rule evidence for {rule.category} was only medium (score {rule.rule_score}) "
            f"and model confidence was {ml.model_confidence:.2f}.",
        )

    # ---- Case 3: weak/no rule — ML decides alone -------------------------
    if ml_accepted:
        return ClassificationResult(
            category=ml.predicted_category,
            requires_review=False,
            classification_method=METHOD_ML,
            classification_confidence=ml.model_confidence,
            explanation=(
                f"Categorized as {ml.predicted_category} by the {ml.model_name} model "
                f"with {ml.model_confidence:.2f} confidence."
            ),
            classification_rule=rule.matched_rule,
            rule_score=rule.rule_score,
            rule_category=rule.category,
            model_name=ml.model_name,
            model_confidence=ml.model_confidence,
            ml_category=ml.predicted_category,
            top_3=ml.top_3,
            agreement="ml_only",
        )

    return _last_resort(narration, direction, account_type, rule, ml)


def _last_resort(narration: str, direction: Optional[str],
                 account_type: Optional[str],
                 rule: RuleResult, ml: MLPrediction,
                 reason: Optional[str] = None) -> ClassificationResult:
    """Everything left to try before admitting we cannot decide.

    Two fallbacks, in this order, and the order is the argument:

    1. NARRATION PATTERNS (`purpose_rules`). These read what the transaction
       says about ITSELF — a GST challan, a POS terminal rental, an aggregator
       settlement. Provisional, because "PHONEPE" tells you the rail and the
       collector, not that this particular row was revenue.

    2. THE COUNTERPARTY'S TRADE (`trades`). A business called "Kumar Fish"
       sells fish. Weaker than what the narration says about the transaction,
       which is why it is second: `GST PAYMENT ... FOODS` is a tax payment, and
       a trade rule running first would have called it a food purchase.

    Reached from two places — a medium rule the model would not confirm, and
    the end of the function — because a row that fell out of the first should
    get the same second chance as one that never had a rule at all. It did not,
    and `SRI LAKSHMI VEGETABLES` sat in the review queue as a result.
    """
    derived = derive_purpose(narration, direction=direction)
    if derived is not None:
        # CERTAIN vs PROVISIONAL, and this is where the distinction pays.
        #
        # "LEDGER FOLIO CHARGES - CC/OD is a bank charge" is a fact in the
        # bank's own words; there is nothing a person can add by confirming it,
        # and 350+ rows of exactly that were being queued. "PHONEPE settled
        # money in" is also a fact, but "so this row is revenue" is a
        # judgement — that one keeps its confirmation step.
        certain = derived.certain
        return ClassificationResult(
            category=derived.purpose,
            requires_review=not certain,
            classification_method=METHOD_RULE,
            classification_confidence=0.88 if certain else 0.62,
            explanation=(
                f"Categorized as {derived.purpose}: the narration states "
                f"{derived.note} outright."
                if certain else
                f"Categorized as {derived.purpose} from the narration pattern "
                f"({derived.note}). No categorisation rule matched and the model "
                f"was not confident, so this is a pattern match — worth a look."
            ),
            classification_rule=f"narration_pattern:{derived.note}",
            rule_score=rule.rule_score,
            rule_category=rule.category,
            model_name=ml.model_name if ml.available else None,
            model_confidence=ml.model_confidence,
            ml_category=ml.predicted_category,
            top_3=ml.top_3,
            agreement="narration_fact" if certain else "narration_pattern",
        )

    # The counterparty's name states its trade.
    #
    # NOT provisional, and that is the point. A provisional answer still asks a
    # person to confirm it, and confirming 73 trade names one at a time is
    # exactly the work this is meant to remove. The matched word is recorded in
    # the rule and the explanation, so a wrong answer reads as "matched FISH"
    # and can be corrected — it does not appear from nowhere.
    trade_hit = trades.match(narration, direction=direction, account_type=account_type)
    if trade_hit:
        return ClassificationResult(
            category=trade_hit.flat_purpose,
            requires_review=False,
            classification_method=METHOD_RULE,
            classification_confidence=0.72,
            explanation=(
                f"Categorized as {trade_hit.flat_purpose} because "
                f"{trade_hit.explanation}."
            ),
            classification_rule=f"trade_name:{trade_hit.matched_word}",
            rule_score=rule.rule_score,
            rule_category=rule.category,
            model_name=ml.model_name if ml.available else None,
            model_confidence=ml.model_confidence,
            ml_category=ml.predicted_category,
            top_3=ml.top_3,
            agreement="trade_name",
        )

    if reason is None:
        if not ml.available:
            reason = "No rule matched and no model is available."
        elif ml.predicted_category is not None and not (
                ml.known_token_count >= config.ML_MIN_KNOWN_TOKENS
                and ml.vocab_coverage >= config.ML_MIN_VOCAB_COVERAGE):
            reason = (
                f"No strong rule matched, and the narration is unlike anything the model was "
                f"trained on ({ml.known_token_count} of {ml.total_token_count} terms recognised), "
                f"so its {ml.model_confidence:.2f} confidence for {ml.predicted_category} "
                f"is not trustworthy."
            )
        else:
            reason = f"No strong rule matched and model confidence was only {ml.model_confidence:.2f}."
    return _unresolved(rule, ml, reason)


def classify_batch(transactions: List[Dict[str, Any]]) -> List[ClassificationResult]:
    """Classify a list of {narration, amount, direction} dicts."""
    return [
        classify_transaction(
            narration=t.get("narration") or t.get("description") or "",
            amount=t.get("amount"),
            direction=t.get("direction") or t.get("debit_credit"),
        )
        for t in transactions
    ]
