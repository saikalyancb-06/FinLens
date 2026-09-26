"""Finding, classifying and ingesting financial statements found in a mailbox.

    provider (any) ──► discovery ──► classifier ──► ingest ──► existing pipeline

None of this knows or cares which provider the mail came from, and none of it
requires the sending institution to be known in advance.
"""
from app.statements.classifier import (
    BANK_STATEMENT,
    BROKER_STATEMENT,
    CREDIT_CARD_STATEMENT,
    INVESTMENT_STATEMENT,
    LOAN_STATEMENT,
    NOT_FINANCIAL,
    OTHER_FINANCIAL_DOCUMENT,
    TRANSACTIONAL_TYPES,
    WALLET_STATEMENT,
    DocumentClassification,
    classify_content,
    classify_file,
)
from app.statements.institutions import UNKNOWN, identify_institution

__all__ = [
    "BANK_STATEMENT",
    "CREDIT_CARD_STATEMENT",
    "LOAN_STATEMENT",
    "BROKER_STATEMENT",
    "INVESTMENT_STATEMENT",
    "WALLET_STATEMENT",
    "OTHER_FINANCIAL_DOCUMENT",
    "NOT_FINANCIAL",
    "TRANSACTIONAL_TYPES",
    "DocumentClassification",
    "classify_file",
    "classify_content",
    "identify_institution",
    "UNKNOWN",
]
