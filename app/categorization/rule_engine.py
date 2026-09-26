"""Deterministic, explainable rule engine for transaction categorisation.

Design constraints this implementation satisfies:

* No hardcoded blanket confidence. Every result carries a `rule_score` derived
  from which tier matched, so two different matches never claim equal certainty
  the way the old `default_confidence = 0.97` did.
* Ranked, not first-match. All rules are evaluated and candidates ranked by
  score, so a weak keyword can never override a contradictory strong signal.
* Explainable. Every result names the rule, the matched terms and a
  human-readable explanation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.categorization import rules_config as rc
from app.categorization.normalizer import normalize_narration, tokenize_narration
from app.categorization.taxonomy import UNCATEGORIZED

logger = logging.getLogger(__name__)


@dataclass
class RuleCandidate:
    """One rule that fired, before ranking."""
    category: str
    rule_score: int
    rule_type: str
    matched_rule: str
    matched_terms: List[str] = field(default_factory=list)

    def explain(self) -> str:
        terms = ", ".join(self.matched_terms)
        readable = {
            "intent_phrase": f"Explicit transaction-intent phrase '{terms}'",
            "exact_merchant": f"Exact merchant match for {terms}",
            "strong_phrase": f"Strong phrase match '{terms}'",
            "strong_keyword": f"Strong keyword match '{terms}'",
            "contextual": f"Contextual combination [{terms}]",
            "weak_keyword": f"Weak keyword '{terms}'",
        }.get(self.rule_type, f"Matched {terms}")
        return f"{readable} → {self.category}"


@dataclass
class RuleResult:
    """Final rule-engine verdict for one transaction."""
    category: Optional[str]
    rule_score: int
    matched_rule: Optional[str]
    matched_terms: List[str]
    rule_type: Optional[str]
    explanation: str
    candidates: List[RuleCandidate] = field(default_factory=list)
    is_ambiguous: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "rule_score": self.rule_score,
            "matched_rule": self.matched_rule,
            "matched_terms": self.matched_terms,
            "rule_type": self.rule_type,
            "explanation": self.explanation,
            "is_ambiguous": self.is_ambiguous,
            "runner_up": (
                {
                    "category": self.candidates[1].category,
                    "rule_score": self.candidates[1].rule_score,
                }
                if len(self.candidates) > 1 else None
            ),
        }


class RuleEngine:
    """Scores a narration against the configured rule tiers."""

    def __init__(self, config=rc):
        self.cfg = config

    # -- matching helpers ---------------------------------------------------

    @staticmethod
    def _phrase_in(phrase: str, normalized: str) -> bool:
        """Word-boundary-aware containment check on the normalised narration.

        Padding with spaces means "VI" cannot match inside "VIDEO", while
        multi-word phrases like "BANK CHARGES" still match as a unit.
        """
        return f" {phrase} " in f" {normalized} "

    @staticmethod
    def _embedded_in_token(phrase: str, tokens: List[str], min_len: int = 5) -> bool:
        """Detect a phrase concatenated inside a single token.

        Banks routinely strip separators: "UPI-NAMMAYATRI",
        "TATAPOWERBILLPAYMENT", "NEFT-TRANSACTIONCHARGE". Word-boundary matching
        misses all of these, and the narration then looks like a bare payment
        rail — which is how real merchant purchases get booked as Transfers.

        The min_len guard keeps short names ("VI", "KFC", "NSE") from producing
        spurious substring hits inside unrelated words.
        """
        compact = phrase.replace(" ", "")
        if len(compact) < min_len:
            return False
        return any(len(tok) >= len(compact) and compact in tok for tok in tokens)

    def _collect(self, normalized: str, tokens: List[str]) -> List[RuleCandidate]:
        found: List[RuleCandidate] = []
        token_set = set(tokens)

        # Tier 0 — intent phrases
        for category, phrases in self.cfg.INTENT_PHRASES.items():
            for phrase in phrases:
                if self._phrase_in(phrase, normalized):
                    found.append(RuleCandidate(
                        category=category,
                        rule_score=self.cfg.SCORE_INTENT_PHRASE,
                        rule_type="intent_phrase",
                        matched_rule=f"intent:{phrase}",
                        matched_terms=[phrase],
                    ))

        # Tier 1 — exact merchants
        for category, merchants in self.cfg.EXACT_MERCHANTS.items():
            for merchant in merchants:
                if self._phrase_in(merchant, normalized):
                    found.append(RuleCandidate(
                        category=category,
                        rule_score=self.cfg.SCORE_EXACT_MERCHANT,
                        rule_type="exact_merchant",
                        matched_rule=f"merchant:{merchant}",
                        matched_terms=[merchant],
                    ))
                elif self._embedded_in_token(merchant, tokens):
                    found.append(RuleCandidate(
                        category=category,
                        rule_score=self.cfg.SCORE_MERCHANT_IN_TOKEN,
                        rule_type="merchant_in_token",
                        matched_rule=f"merchant_embedded:{merchant}",
                        matched_terms=[merchant],
                    ))

        # Tier 2 — strong phrases
        for category, phrases in self.cfg.STRONG_PHRASES.items():
            for phrase in phrases:
                if self._phrase_in(phrase, normalized):
                    found.append(RuleCandidate(
                        category=category,
                        rule_score=self.cfg.SCORE_STRONG_PHRASE,
                        rule_type="strong_phrase",
                        matched_rule=f"phrase:{phrase}",
                        matched_terms=[phrase],
                    ))
                elif self._embedded_in_token(phrase, tokens, min_len=8):
                    # Same separator-stripping problem as merchants, but with a
                    # longer minimum: short phrases are far likelier to appear
                    # inside an unrelated word by coincidence.
                    found.append(RuleCandidate(
                        category=category,
                        rule_score=self.cfg.SCORE_PHRASE_IN_TOKEN,
                        rule_type="phrase_in_token",
                        matched_rule=f"phrase_embedded:{phrase}",
                        matched_terms=[phrase],
                    ))

        # Tier 3 — strong keywords
        for category, keywords in self.cfg.STRONG_KEYWORDS.items():
            for kw in keywords:
                if kw in token_set:
                    found.append(RuleCandidate(
                        category=category,
                        rule_score=self.cfg.SCORE_STRONG_KEYWORD,
                        rule_type="strong_keyword",
                        matched_rule=f"keyword:{kw}",
                        matched_terms=[kw],
                    ))

        # Tier 4 — contextual combinations
        for category, required in self.cfg.CONTEXTUAL_COMBINATIONS:
            if all(tok in token_set for tok in required):
                found.append(RuleCandidate(
                    category=category,
                    rule_score=self.cfg.SCORE_CONTEXTUAL,
                    rule_type="contextual",
                    matched_rule=f"context:{'+'.join(required)}",
                    matched_terms=list(required),
                ))

        # Tier 5 — weak keywords
        for category, keywords in self.cfg.WEAK_KEYWORDS.items():
            for kw in keywords:
                if kw in token_set:
                    found.append(RuleCandidate(
                        category=category,
                        rule_score=self.cfg.SCORE_WEAK_KEYWORD,
                        rule_type="weak_keyword",
                        matched_rule=f"weak:{kw}",
                        matched_terms=[kw],
                    ))

        return found

    def _apply_amount_signal(
        self,
        candidates: List[RuleCandidate],
        amount: Optional[float],
        direction: Optional[str],
    ) -> None:
        """Nudge existing candidates using amount/direction context.

        Only ever boosts a candidate that another tier already produced — an
        amount alone must not invent a category.
        """
        if amount is None and not direction:
            return

        dir_norm = (direction or "").strip().upper()
        for cand in candidates:
            signal = self.cfg.AMOUNT_SIGNALS.get(cand.category)
            if not signal:
                continue
            if signal.get("direction") and dir_norm != signal["direction"]:
                continue
            if signal.get("min_amount") is not None:
                if amount is None or float(amount) < signal["min_amount"]:
                    continue
            cand.rule_score += self.cfg.SCORE_AMOUNT_SIGNAL
            cand.matched_terms.append(f"{dir_norm or 'AMOUNT'}_SIGNAL")

    # -- public API ---------------------------------------------------------

    def classify(
        self,
        narration: str,
        amount: Optional[float] = None,
        direction: Optional[str] = None,
    ) -> RuleResult:
        """Return the best-supported category, or None when rules can't decide."""
        normalized = normalize_narration(narration)
        tokens = tokenize_narration(narration)

        if not normalized:
            return RuleResult(
                category=None, rule_score=0, matched_rule=None, matched_terms=[],
                rule_type=None,
                explanation="Empty narration after normalisation; no rule could be applied.",
            )

        candidates = self._collect(normalized, tokens)
        if not candidates:
            return RuleResult(
                category=None, rule_score=0, matched_rule=None, matched_terms=[],
                rule_type=None,
                explanation=f"No rule matched normalised narration '{normalized}'.",
            )

        self._apply_amount_signal(candidates, amount, direction)

        # Collapse to the best candidate per category, then rank. Ranking rather
        # than taking the first match is what stops a weak keyword from
        # overriding a strong contradictory signal.
        best_per_category: Dict[str, RuleCandidate] = {}
        for cand in candidates:
            existing = best_per_category.get(cand.category)
            if existing is None or cand.rule_score > existing.rule_score:
                best_per_category[cand.category] = cand

        ranked = sorted(
            best_per_category.values(),
            key=lambda c: (c.rule_score, c.category),
            reverse=True,
        )
        top = ranked[0]

        # Contradiction check: two different categories tied at the top means the
        # rules genuinely disagree, so the result is marked ambiguous and the
        # hybrid layer will weigh ML evidence rather than trusting the tie-break.
        is_ambiguous = len(ranked) > 1 and ranked[1].rule_score == top.rule_score

        explanation = top.explain()
        if is_ambiguous:
            explanation += (
                f" (ambiguous: {ranked[1].category} matched at the same score "
                f"{ranked[1].rule_score})"
            )

        return RuleResult(
            category=top.category,
            rule_score=top.rule_score,
            matched_rule=top.matched_rule,
            matched_terms=top.matched_terms,
            rule_type=top.rule_type,
            explanation=explanation,
            candidates=ranked,
            is_ambiguous=is_ambiguous,
        )


# Module-level singleton; the engine is stateless and cheap to share.
rule_engine = RuleEngine()
