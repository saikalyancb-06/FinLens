from typing import Dict, Any, List, Optional
from app.config import settings
from app.rules.rule_classifier import RuleClassifier
from app.ai.ml_classifier import MLClassifier

class HybridDecisionEngine:
    def __init__(
        self,
        confidence_threshold: float = None,
        artifact_dir: Optional[str] = None,
        models_dir: Optional[str] = None,
    ):
        self.confidence_threshold = confidence_threshold if confidence_threshold is not None else settings.CONFIDENCE_THRESHOLD
        self.rule_classifier = RuleClassifier()
        # `models_dir` is a deprecated alias kept for existing callers; MLClassifier
        # warns if it is the only one supplied. Passing None lets the classifier
        # fall back to settings.CATEGORIZER_ARTIFACT_DIR and share the loaded model.
        self.ml_classifier = MLClassifier(artifact_dir=artifact_dir, models_dir=models_dir)

    def evaluate(self, txn: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluates a single normalized transaction dict and returns full decision payload.
        """
        description = txn.get("description", "")
        amount = txn.get("amount", 0.0)
        debit = txn.get("debit", 0.0)
        credit = txn.get("credit", 0.0)

        # Step 1: Rule Engine Evaluation
        rule_res = self.rule_classifier.classify_transaction(txn)
        rule_conf = rule_res.get("confidence", 0.0)
        rule_cat = rule_res.get("category", "")
        matched_rule = rule_res.get("matched_rule", "")

        # Step 2: Decision Logic Threshold Check
        if rule_conf >= self.confidence_threshold:
            # Rule Engine match meets threshold -> Skip ML Model execution
            reasoning = (
                f"Rule confidence ({rule_conf:.2f}) meets or exceeds threshold ({self.confidence_threshold:.2f}). "
                f"Selected Rule Engine prediction for rule '{matched_rule}'."
            )
            return {
                "category": rule_cat,
                "rule_confidence": round(rule_conf, 4),
                "ml_confidence": 0.0,
                "final_confidence": round(rule_conf, 4),
                "prediction_source": "Rule Engine",
                "reasoning": reasoning,
                "top_three_ml": [],
                "matched_rule": matched_rule
            }
        else:
            # Rule Engine match below threshold -> Execute ML Model
            ml_res = self.ml_classifier.predict(
                description=description,
                amount=amount,
                debit=debit,
                credit=credit
            )
            ml_conf = ml_res.get("confidence", 0.0)
            ml_cat = ml_res.get("category", "Uncategorized")
            top_three = ml_res.get("top_three", [])

            # The threshold has to gate the ML branch as well, not just the rule
            # branch. Emitting a sub-threshold guess as the final category books a
            # confidently-wrong label into the ledger; leaving it Uncategorized
            # routes the row to the review queue instead. The raw model output is
            # still reported in ml_confidence / top_three_ml for transparency.
            meets_threshold = ml_conf >= self.confidence_threshold
            final_cat = ml_cat if meets_threshold else "Uncategorized"
            requires_review = not meets_threshold

            if meets_threshold:
                reasoning = (
                    f"Rule confidence ({rule_conf:.2f}) is below threshold ({self.confidence_threshold:.2f}). "
                    f"Fallback to ML Model prediction (category: '{ml_cat}', confidence: {ml_conf:.4f})."
                )
            else:
                reasoning = (
                    f"Rule confidence ({rule_conf:.2f}) is below threshold ({self.confidence_threshold:.2f}). "
                    f"Fallback to ML Model prediction (category: '{ml_cat}', confidence: {ml_conf:.4f}), "
                    f"which is also below threshold — left Uncategorized for manual review."
                )

            return {
                "category": final_cat,
                "ml_category": ml_cat,
                "rule_confidence": round(rule_conf, 4),
                "ml_confidence": round(ml_conf, 4),
                "final_confidence": round(ml_conf, 4),
                "prediction_source": "ML Model",
                "requires_review": requires_review,
                "reasoning": reasoning,
                "top_three_ml": top_three,
                "matched_rule": matched_rule
            }

    def evaluate_transactions(self, txns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Evaluates a list of normalized transactions and attaches decision object to each.
        """
        results = []
        for txn in txns:
            c_txn = dict(txn)
            decision = self.evaluate(c_txn)
            c_txn["decision"] = decision
            results.append(c_txn)
        return results
