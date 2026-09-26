import pytest
from app.parsers.validator import TransactionValidator

def test_validator_multiline_description_and_clean_fields():
    raw_txns = [
        {
            "date": "2026-08-01",
            "description": "TRANSFER TO\nJOHN DOE \r\nREF 12345",
            "debit": 100.0,
            "credit": 0.0,
            "amount": 100.0,
            "balance": 900.0,
            "transaction_type": "debit",
            "reference_number": "12345",
            "raw_text": "TRANSFER TO JOHN DOE"
        }
    ]

    validator = TransactionValidator()
    res = validator.validate(raw_txns)

    assert len(res.errors) == 0
    assert res.transactions[0]["description"] == "TRANSFER TO JOHN DOE REF 12345"

def test_validator_missing_dates_and_amounts():
    raw_txns = [
        {
            "date": "",
            "description": "UNKNOWN FEE",
            "debit": 0.0,
            "credit": 0.0,
            "amount": 0.0,
            "balance": 500.0,
            "raw_text": "UNKNOWN FEE"
        }
    ]

    validator = TransactionValidator()
    res = validator.validate(raw_txns)

    error_types = [e["error_type"] for e in res.errors]
    assert "missing_date" in error_types
    assert "missing_amount" in error_types

def test_validator_duplicate_detection():
    txn = {
        "date": "2026-08-01",
        "description": "COFFEE SHOP",
        "debit": 5.50,
        "credit": 0.0,
        "amount": 5.50,
        "balance": 494.50,
        "reference_number": "POS99",
        "raw_text": "COFFEE SHOP"
    }

    validator = TransactionValidator()
    res = validator.validate([txn, txn])

    error_types = [e["error_type"] for e in res.errors]
    assert "duplicate_transaction" in error_types

def test_validator_invalid_balance_continuity():
    raw_txns = [
        {"date": "2026-08-01", "description": "T1", "debit": 100.0, "credit": 0.0, "amount": 100.0, "balance": 1000.0},
        {"date": "2026-08-02", "description": "T2", "debit": 200.0, "credit": 0.0, "amount": 200.0, "balance": 500.0} # Expected 800
    ]

    validator = TransactionValidator()
    res = validator.validate(raw_txns)

    error_types = [e["error_type"] for e in res.errors]
    assert "balance_discontinuity" in error_types or "invalid_balance" in error_types

def test_validator_incorrect_debit_credit_values():
    raw_txns = [
        {"date": "2026-08-01", "description": "T1", "debit": 100.0, "credit": 50.0, "amount": 100.0, "balance": 1000.0}
    ]

    validator = TransactionValidator()
    res = validator.validate(raw_txns)

    error_types = [e["error_type"] for e in res.errors]
    assert "both_debit_credit" in error_types or "incorrect_debit_credit_values" in error_types
