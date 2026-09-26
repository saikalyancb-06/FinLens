import os
import io
import pytest
from decimal import Decimal
from datetime import date

from layer1_extraction.extractor import extract_raw_document, extract_csv_raw
from layer2_normalization.models import Transaction, clean_indian_decimal, parse_date_canonical
from layer2_normalization.normalizer import NormalizationEngine
from layer3_classification.classifier import ClassificationEngine, CategorizedTransaction

def test_layer1_extraction():
    raw_csv = b"Date,Narration,Withdrawal,Deposit,Balance\n31/03/2025,NEFT CREDIT,0.0,30078.00,247934.98\n"
    res = extract_csv_raw(raw_csv, "test.csv")
    assert len(res.rows) == 2
    assert res.source_file == "test.csv"

def test_layer2_indian_decimal_parsing():
    assert clean_indian_decimal("2,47,934.98") == Decimal("247934.98")
    assert clean_indian_decimal("33,48,206.00") == Decimal("3348206.00")
    assert clean_indian_decimal("1,10,73,64,312.00") == Decimal("1107364312.00")
    assert clean_indian_decimal("2,47,934.98 Cr") == Decimal("247934.98")

def test_layer2_pydantic_invariants():
    # Exactly one of withdrawal or deposit must be non-null
    tx = Transaction(
        transaction_date=date(2025, 3, 31),
        narration="TEST CREDIT",
        withdrawal=None,
        deposit=Decimal("30078.00"),
        balance=Decimal("247934.98"),
        balance_dr_cr="Cr",
        source_file="test.csv"
    )
    assert tx.deposit == Decimal("30078.00")
    assert tx.withdrawal is None

    # Test failure when both withdrawal & deposit are populated
    with pytest.raises(ValueError):
        Transaction(
            transaction_date=date(2025, 3, 31),
            narration="INVALID DUAL POPULATION",
            withdrawal=Decimal("100"),
            deposit=Decimal("100"),
            balance=Decimal("247934.98"),
            balance_dr_cr="Cr",
            source_file="test.csv"
        )

def test_layer3_classification_confidence_bound():
    tx = Transaction(
        transaction_date=date(2025, 3, 31),
        narration="EBANK:SELF TRANSFER",
        withdrawal=Decimal("100000.00"),
        deposit=None,
        balance=Decimal("205396.98"),
        balance_dr_cr="Cr",
        source_file="test.csv"
    )

    engine = ClassificationEngine("configs/rules.yaml")
    cat_tx = engine.classify_transaction(tx)

    assert cat_tx.category == "Internal Fund Transfer"
    assert cat_tx.confidence == 1.0
    assert cat_tx.method == "Rule Engine"

    # Enforce confidence strictly in [0.0, 1.0]
    with pytest.raises(ValueError):
        CategorizedTransaction(
            transaction=tx,
            category="Self Transfer",
            confidence=100.0, # Must raise error because confidence > 1.0
            method="Invalid Scaling"
        )
