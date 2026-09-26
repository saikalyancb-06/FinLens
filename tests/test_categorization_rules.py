"""Unit tests for the deterministic rule engine and hybrid decision layer.

The negative cases matter as much as the positive ones. Most of them encode a
specific bug the previous system had:

  * "SWIGGY CHARGE" must not become Bank Charges just because it contains
    "CHARGE" — the old keyword rule did exactly that.
  * "UPI SWIGGY" must not become Transfers just because it rides the UPI rail.
  * "UPI TRANSFER TO SWIGGY" must be Transfers despite naming a merchant,
    because the transaction's stated intent outranks merchant identity.
"""

import pytest

from app.categorization.config import CategorizationConfig
from app.categorization.hybrid import classify_transaction
from app.categorization.rule_engine import rule_engine
from app.categorization.normalizer import normalize_narration
from app.categorization.taxonomy import (
    CATEGORIES, UNCATEGORIZED, is_valid_category, normalize_category,
)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("upi-swIGgy-ORDER-12345", "UPI SWIGGY ORDER"),
    ("  UPI--SWIGGY   ORDER  ", "UPI SWIGGY ORDER"),
    ("RELIANCE FRESH REF 536883 RTN", "RELIANCE FRESH"),
])
def test_normalization(raw, expected):
    assert normalize_narration(raw) == expected


def test_normalization_preserves_semantic_words():
    """REFUND/REVERSAL/FEE change meaning and must never be stripped."""
    for word in ["REFUND", "REVERSAL", "FEE", "SALARY", "RENT", "TRANSFER"]:
        assert word in normalize_narration(f"UPI-SOMETHING-{word}-99887766")


# ---------------------------------------------------------------------------
# Positive rule cases (Part 14)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("narration,expected", [
    ("SWIGGY", "Food & Dining"),
    ("ZOMATO", "Food & Dining"),
    ("UBER", "Transportation"),
    ("AMAZON", "Shopping"),
    ("NETFLIX", "Entertainment"),
    ("JIO RECHARGE", "Utilities & Bills"),
    ("APOLLO HOSPITAL", "Healthcare"),
    ("COURSERA", "Education"),
    ("INDIGO", "Travel"),
    ("HOUSE RENT", "Rent & Housing"),
    ("BANK CHARGES", "Bank Charges"),
    ("SALARY CREDIT", "Salary / Income"),
    ("NEFT TRANSFER", "Transfers"),
    ("ZERODHA", "Investments"),
])
def test_rule_positive_cases(narration, expected):
    result = rule_engine.classify(narration)
    assert result.category == expected, f"{narration}: got {result.category}"
    assert result.rule_score > 0
    assert result.matched_rule
    assert result.explanation


# ---------------------------------------------------------------------------
# Negative / ambiguous cases (Part 14)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("narration,expected", [
    ("UPI SWIGGY", "Food & Dining"),
    ("AMAZON REFUND", "Shopping"),
    ("BANK CHARGE REVERSAL", "Bank Charges"),
    ("UPI TRANSFER TO SELF", "Transfers"),
    ("UPI TRANSFER TO SWIGGY", "Transfers"),
    ("SWIGGY REFUND", "Food & Dining"),
])
def test_rule_negative_cases(narration, expected):
    assert rule_engine.classify(narration).category == expected


def test_upi_swiggy_is_not_bank_charges():
    assert rule_engine.classify("UPI SWIGGY").category != "Bank Charges"


def test_amazon_refund_is_not_bank_charges():
    assert rule_engine.classify("AMAZON REFUND").category != "Bank Charges"


def test_bare_charge_word_does_not_imply_bank_charges():
    """The single word CHARGE must not outrank a merchant match."""
    assert rule_engine.classify("SWIGGY CHARGE").category == "Food & Dining"
    assert rule_engine.classify("ZOMATO SERVICE").category == "Food & Dining"


def test_bank_charges_requires_fee_context():
    for narration in ["BANK CHARGES", "SERVICE CHARGE", "ATM FEE",
                      "CASH WITHDRAWAL FEE", "SMS CHARGES"]:
        assert rule_engine.classify(narration).category == "Bank Charges", narration


def test_payment_rail_alone_does_not_imply_transfer():
    """A bare UPI/NEFT prefix is weak evidence and must lose to a merchant."""
    for narration in ["UPI SWIGGY", "NEFT AMAZON", "IMPS NETFLIX"]:
        assert rule_engine.classify(narration).category != "Transfers", narration


@pytest.mark.parametrize("narration,expected", [
    ("JIO RECHARGE REFUND", "Utilities & Bills"),
    ("NETFLIX REFUND", "Entertainment"),
    ("RENT REFUND", "Rent & Housing"),
    ("SALARY REVERSAL", "Salary / Income"),
    ("UBER REFUND", "Transportation"),
    ("ATM CASH WITHDRAWAL FEE", "Bank Charges"),
])
def test_refund_does_not_decide_category(narration, expected):
    """REFUND/REVERSAL never determines the category; the merchant does."""
    assert rule_engine.classify(narration).category == expected


# ---------------------------------------------------------------------------
# Scoring & priority
# ---------------------------------------------------------------------------

def test_no_hardcoded_uniform_confidence():
    """Different tiers must produce different scores.

    Regression guard for the old classifier, which returned 0.97 for every
    match regardless of evidence strength.
    """
    scores = {
        rule_engine.classify(n).rule_score
        for n in ["SWIGGY", "HOUSE RENT", "RENT", "UPI TRANSFER"]
    }
    assert len(scores) > 1, "rule scores are uniform — evidence strength is not differentiated"


def test_strong_signal_beats_weak_keyword():
    """An exact merchant must outrank a weak rail keyword."""
    result = rule_engine.classify("UPI SWIGGY")
    assert result.category == "Food & Dining"
    assert result.rule_score >= 90


def test_intent_phrase_outranks_merchant():
    result = rule_engine.classify("UPI TRANSFER TO SWIGGY")
    assert result.category == "Transfers"
    assert result.rule_type == "intent_phrase"


def test_embedded_merchant_is_detected():
    """Banks strip separators; the merchant must still be found."""
    for narration, expected in [
        ("UPI/MCDONALDSINDIA/432548", "Food & Dining"),
        ("POS-MYNTRADESIGNS-836595", "Shopping"),
        ("UPI-NAMMAYATRI", "Transportation"),
    ]:
        assert rule_engine.classify(narration).category == expected, narration


def test_result_is_explainable():
    result = rule_engine.classify("SWIGGY ORDER")
    d = result.to_dict()
    for key in ["category", "rule_score", "matched_rule", "matched_terms",
                "rule_type", "explanation"]:
        assert key in d and d[key] is not None
    assert "SWIGGY" in d["matched_terms"]


def test_unmatched_narration_returns_no_category():
    result = rule_engine.classify("XKCD9931 QQQ")
    assert result.category is None
    assert result.rule_score == 0


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------

def test_taxonomy_has_exactly_15_categories():
    assert len(CATEGORIES) == 15
    assert len(set(CATEGORIES)) == 15


def test_category_aliases_map_to_canonical():
    assert normalize_category("food") == "Food & Dining"
    assert normalize_category("SALARY") == "Salary / Income"
    assert normalize_category("transfer") == "Transfers"
    assert normalize_category("nonsense-category") is None


def test_uncategorized_is_not_a_taxonomy_member():
    """Uncategorized means 'no decision', distinct from the Other category."""
    assert not is_valid_category(UNCATEGORIZED)
    assert is_valid_category("Other")


def test_rule_engine_never_asserts_other():
    """Other must be reached by absence of evidence, never claimed by a rule."""
    from app.categorization import rules_config as rc
    for mapping in (rc.EXACT_MERCHANTS, rc.STRONG_PHRASES,
                    rc.STRONG_KEYWORDS, rc.WEAK_KEYWORDS):
        assert "Other" not in mapping


# ---------------------------------------------------------------------------
# Hybrid decision layer
# ---------------------------------------------------------------------------

def test_low_confidence_becomes_uncategorized_and_flagged():
    result = classify_transaction("ZZQQ8891 XKQP")
    assert result.category == UNCATEGORIZED
    assert result.requires_review is True
    assert result.classification_confidence == 0.0


def test_high_confidence_rule_is_accepted_without_review():
    result = classify_transaction("SWIGGY ORDER", amount=450, direction="DEBIT")
    assert result.category == "Food & Dining"
    assert result.requires_review is False
    assert result.classification_method in ("rule", "hybrid")


def test_provenance_fields_are_separate():
    """classification_method must describe HOW, never the ingestion channel."""
    result = classify_transaction("SWIGGY ORDER")
    assert result.classification_method in ("rule", "ml", "hybrid", "manual", "none")
    # These are channel values and must never appear as a classification method.
    assert result.classification_method not in ("upload", "gmail", "rpa", "aa",
                                                "MANUAL_UPLOAD", "STATEMENT")


def test_confidence_never_reports_certainty():
    """A rule match is strong evidence, not proof; confidence must stay < 1.0."""
    result = classify_transaction("SWIGGY ORDER")
    assert 0.0 < result.classification_confidence < 1.0


def test_thresholds_are_configurable():
    cfg = CategorizationConfig()
    assert cfg.rule_confidence(110) > cfg.rule_confidence(80) > cfg.rule_confidence(20)
    assert cfg.rule_confidence(0) == 0.0


def test_explanation_is_present_for_every_outcome():
    for narration in ["SWIGGY ORDER", "ZZQQ8891 XKQP", "UPI TRANSFER TO SELF"]:
        assert classify_transaction(narration).explanation.strip()


# ---------------------------------------------------------------------------
# Out-of-distribution guard
# ---------------------------------------------------------------------------
# Regression tests for a real failure found by running unseen narrations through
# the system: the model returned 0.98 confidence for "DOCTOR CONSULTATION FEE
# PAID" -> Bank Charges on the strength of the single token "fee", and mapped
# unknown local merchants to Transfers from "upi"/"neft" alone. A calibrated
# probability is only meaningful for input resembling the training data.

ml_required = pytest.mark.skipif(
    not __import__("app.categorization.ml_service", fromlist=["ml_service"]).ml_service.is_available,
    reason="no trained model artifact present",
)


@ml_required
def test_unknown_merchant_is_not_auto_classified():
    """An unseen local merchant must not be booked from the payment rail alone."""
    for narration in [
        "UPI-SRI LAKSHMI TRADERS-8871",
        "NEFT-M/S RAGHAV AND SONS",
        "UPI/VENKATESHWARA ENTERPRISES/5521",
    ]:
        result = classify_transaction(narration, amount=2450, direction="DEBIT")
        assert result.requires_review is True, f"{narration} was auto-classified as {result.category}"
        assert result.category == UNCATEGORIZED


@ml_required
def test_fee_wording_outside_banking_context_is_not_bank_charges():
    """The word FEE alone must never book a non-bank charge as Bank Charges."""
    for narration in ["DOCTOR CONSULTATION FEE PAID", "COURIER HANDLING CHARGE"]:
        result = classify_transaction(narration, amount=800, direction="DEBIT")
        assert result.category != "Bank Charges", (
            f"{narration} was classified as Bank Charges via {result.classification_method}"
        )


@ml_required
def test_known_narrations_still_auto_classify():
    """The guard must not block genuinely familiar input."""
    for narration, expected in [
        ("UPI-SWIGGY ORDER-88213", "Food & Dining"),
        ("NETFLIX SUBSCRIPTION MONTHLY", "Entertainment"),
        ("SALARY CREDIT MAR 2026", "Salary / Income"),
        ("APOLLO PHARMACY BANGALORE", "Healthcare"),
    ]:
        result = classify_transaction(narration, amount=500, direction="DEBIT")
        assert result.requires_review is False, f"{narration} was sent to review"
        assert result.category == expected


@ml_required
def test_vocabulary_coverage_is_reported():
    from app.categorization.ml_service import ml_service
    familiar = ml_service.predict("UPI SWIGGY ORDER")
    unfamiliar = ml_service.predict("UPI SRI LAKSHMI TRADERS")
    assert familiar.vocab_coverage > unfamiliar.vocab_coverage
    assert familiar.known_token_count >= 2


@ml_required
def test_abstain_explanation_names_the_reason():
    result = classify_transaction("UPI-VENKATESHWARA ENTERPRISES-5521")
    assert "recognised" in result.explanation or "confidence" in result.explanation
    assert "review" in result.explanation.lower()
