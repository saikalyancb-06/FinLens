import pytest
from app.ai.ml_classifier import MLClassifier
from app.parsers.pipeline import TransactionParsingPipeline

def test_ml_classifier_standalone():
    classifier = MLClassifier()
    res = classifier.predict(
        description="SWIGGY BANGALORE IN",
        amount=350.0,
        debit=350.0,
        credit=0.0
    )

    assert "category" in res
    assert "confidence" in res
    assert "top_three" in res
    assert isinstance(res["top_three"], list)
    if res["top_three"]:
        assert len(res["top_three"]) <= 3
        assert "category" in res["top_three"][0]
        assert "confidence" in res["top_three"][0]

def test_rule_threshold_skips_ml(tmp_path):
    csv_file = tmp_path / "test_rule_skip.csv"
    csv_content = (
        "Date,Particulars,Debit,Credit,Balance\n"
        "01/08/2026,SWIGGY BANGALORE ORDER #123,350.00,0.00,1000.00\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    pipeline = TransactionParsingPipeline(confidence_threshold=0.80)
    txns = pipeline.process_file(str(csv_file))

    assert len(txns) == 1
    dec = txns[0]["decision"]
    assert dec["prediction_source"] == "Rule Engine"
    assert dec["category"] == "Food & Dining"
    assert dec["final_confidence"] == 0.97

def test_unmatched_rule_uses_ml(tmp_path):
    csv_file = tmp_path / "test_ml_fallback.csv"
    csv_content = (
        "Date,Particulars,Debit,Credit,Balance\n"
        "01/08/2026,UNKNOWN HARDWARE STORE PURCHASE,150.00,0.00,850.00\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    pipeline = TransactionParsingPipeline(confidence_threshold=0.80)
    txns = pipeline.process_file(str(csv_file))

    assert len(txns) == 1
    dec = txns[0]["decision"]
    assert dec["prediction_source"] == "ML Model"
    assert "category" in dec
    assert "final_confidence" in dec
    assert "top_three_ml" in dec
