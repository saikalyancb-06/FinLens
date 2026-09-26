import pytest
from app.ai.decision_engine import HybridDecisionEngine
from app.parsers.pipeline import TransactionParsingPipeline

def test_hybrid_decision_engine_rule_branch():
    engine = HybridDecisionEngine(confidence_threshold=0.80)
    txn = {
        "date": "2026-08-01",
        "description": "ZOMATO ORDER #998877",
        "debit": 250.0,
        "credit": 0.0,
        "amount": 250.0,
        "balance": 1000.0,
        "transaction_type": "debit"
    }

    res = engine.evaluate(txn)

    assert res["prediction_source"] == "Rule Engine"
    assert res["category"] == "Food & Dining"
    assert res["rule_confidence"] == 0.97
    assert res["final_confidence"] == 0.97
    assert "reasoning" in res
    assert "meets or exceeds threshold" in res["reasoning"]

def test_hybrid_decision_engine_ml_branch():
    engine = HybridDecisionEngine(confidence_threshold=0.80)
    txn = {
        "date": "2026-08-01",
        "description": "UNCLASSIFIED HARDWARE VENDOR DEPOSIT",
        "debit": 0.0,
        "credit": 5000.0,
        "amount": 5000.0,
        "balance": 6000.0,
        "transaction_type": "credit"
    }

    res = engine.evaluate(txn)

    assert res["prediction_source"] == "ML Model"
    assert res["rule_confidence"] == 0.0
    assert res["ml_confidence"] > 0.0
    assert res["final_confidence"] == res["ml_confidence"]
    assert "reasoning" in res
    assert "Fallback to ML Model" in res["reasoning"]

def test_pipeline_hybrid_integration(tmp_path):
    csv_file = tmp_path / "hybrid_test.csv"
    csv_content = (
        "Date,Particulars,Debit,Credit,Balance\n"
        "01/08/2026,SWIGGY BANGALORE ORDER,300.00,0.00,700.00\n"
        "02/08/2026,UNKNOWN CORP SERVICES,500.00,0.00,200.00\n"
    )
    csv_file.write_text(csv_content, encoding="utf-8")

    pipeline = TransactionParsingPipeline(confidence_threshold=0.80)
    txns = pipeline.process_file(str(csv_file))

    assert len(txns) == 2
    
    # Check rule branch
    assert txns[0]["decision"]["prediction_source"] == "Rule Engine"
    assert txns[0]["decision"]["rule_confidence"] == 0.97
    assert txns[0]["decision"]["final_confidence"] == 0.97

    # Check ML branch
    assert txns[1]["decision"]["prediction_source"] == "ML Model"
    assert txns[1]["decision"]["rule_confidence"] == 0.0
    assert txns[1]["decision"]["ml_confidence"] > 0.0
    assert txns[1]["decision"]["final_confidence"] == txns[1]["decision"]["ml_confidence"]
