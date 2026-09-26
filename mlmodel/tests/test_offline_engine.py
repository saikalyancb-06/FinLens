import pytest
import pandas as pd
from financial_parser.engine import OfflineFinancialParserEngine
from financial_parser.models.transaction import UniversalTransaction

def test_universal_transaction_schema():
    tx = UniversalTransaction(
        date="2025-03-31",
        description="NEFT CREDIT FROM RESILIENT INNOVATIONS",
        debit=0.0,
        credit=30078.00,
        balance=247934.98,
        currency="INR",
        reference="",
        transaction_id="",
        page_number=1,
        confidence=1.0
    )
    
    dump = tx.model_dump()
    assert dump["date"] == "2025-03-31"
    assert dump["debit"] == 0.0
    assert dump["credit"] == 30078.00
    assert dump["balance"] == 247934.98
    assert dump["confidence"] == 1.0

def test_parser_engine_golden_reference():
    engine = OfflineFinancialParserEngine()
    with open('golden_reference.csv', 'rb') as f:
        content = f.read()

    res = engine.process_document(content, 'golden_reference.csv')
    assert res.total_transactions == 410
    assert len(res.transactions) == 410
    tx0 = res.transactions[0]
    assert hasattr(tx0, "date")
    assert hasattr(tx0, "description")
    assert hasattr(tx0, "debit")
    assert hasattr(tx0, "credit")
    assert hasattr(tx0, "balance")
