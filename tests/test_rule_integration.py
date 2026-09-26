import pytest
from app.rules.rule_classifier import RuleClassifier
from app.parsers.pipeline import TransactionParsingPipeline

def test_rule_classifier_match():
    classifier = RuleClassifier()
    txn = {
        "date": "2026-08-01",
        "description": "SWIGGY BANGALORE ORDER #12345",
        "debit": 350.0,
        "credit": 0.0,
        "amount": 350.0,
        "balance": 1500.0,
        "transaction_type": "debit",
        "reference_number": "12345",
        "raw_text": "SWIGGY BANGALORE"
    }

    res = classifier.classify_transaction(txn)

    assert res["category"] == "Food & Dining"
    assert res["confidence"] == 0.97
    assert "rule_9" in res["matched_rule"]
    assert "swiggy|zomato" in res["matched_rule"]
    assert "Matched rule pattern" in res["reason"]

def test_rule_classifier_no_match():
    classifier = RuleClassifier()
    txn = {
        "date": "2026-08-01",
        "description": "UNKNOWN VENDOR ABC",
        "debit": 100.0,
        "credit": 0.0,
        "amount": 100.0,
        "balance": 1400.0,
        "transaction_type": "debit",
        "reference_number": "",
        "raw_text": "UNKNOWN VENDOR ABC"
    }

    res = classifier.classify_transaction(txn)

    assert res["category"] == "Uncategorized"
    assert res["confidence"] == 0.0
    assert res["matched_rule"] == "none"
    assert res["reason"] == "No rule matched transaction description."

def test_pipeline_with_rule_integration(tmp_path):
    csv_file = tmp_path / "test_rule_statement.csv"
    csv_content = (
        "Date,Particulars,Debit,Credit,Balance\n"
        "01/08/2026,BHARATPE PAYOUTS 98765,0.00,2500.00,2500.00\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    pipeline = TransactionParsingPipeline()
    res = pipeline.process_file_with_validation(str(csv_file))

    txns = res["transactions"]
    assert len(txns) == 1
    decision = txns[0]["decision"]
    assert decision["category"] == "Merchant Settlement"
    assert decision["rule_confidence"] == 0.97
    assert "bharatpe" in decision["matched_rule"]
