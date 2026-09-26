from typing import List, Optional
from pydantic import BaseModel, Field, model_validator

class UniversalTransaction(BaseModel):
    date: str
    description: str
    debit: float = Field(default=0.0, ge=0.0)
    credit: float = Field(default=0.0, ge=0.0)
    balance: float = Field(default=0.0)
    currency: str = "INR"
    reference: str = ""
    transaction_id: str = ""
    page_number: int = 1
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_transaction_fields(self):
        if not self.date:
            raise ValueError("Transaction date cannot be empty.")
        if not self.description:
            raise ValueError("Transaction description cannot be empty.")
        if self.debit < 0 or self.credit < 0:
            raise ValueError("Debit and credit amounts cannot be negative.")
        return self

class ExtractionResult(BaseModel):
    document_type: str
    file_type: str
    header_confidence: float
    total_transactions: int
    failed_rows: int
    validation_errors: List[str]
    transactions: List[UniversalTransaction]
