from datetime import datetime, date as date_type
from decimal import Decimal
from typing import Optional, List, Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from app.models.transaction import Direction


class PredictionBase(BaseModel):
    predicted_category: str
    confidence: float
    rule_used: Optional[str] = None
    model_version: Optional[str] = None

class PredictionCreate(PredictionBase):
    transaction_id: Optional[UUID] = None
    category_id: Optional[UUID] = None

class PredictionRead(PredictionBase):
    id: UUID
    transaction_id: UUID
    category_id: Optional[UUID] = None
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)

class TransactionBase(BaseModel):
    account_id: Optional[UUID] = None
    statement_id: Optional[UUID] = None
    entity_id: Optional[UUID] = None
    txn_date: date_type
    value_date: Optional[date_type] = None
    narration_raw: str
    narration_clean: Optional[str] = None
    payment_method: Optional[str] = None
    counterparty: Optional[str] = None
    reference_no: Optional[str] = None
    debit_paise: Optional[int] = Field(None, ge=0)
    credit_paise: Optional[int] = Field(None, ge=0)
    balance_paise: Optional[int] = None
    direction: Optional[Direction] = None
    category_id: Optional[UUID] = None

class TransactionCreate(TransactionBase):
    prediction: Optional[PredictionBase] = None

class TransactionResponse(BaseModel):
    id: UUID
    user_id: UUID
    account_id: Optional[UUID] = None
    statement_id: Optional[UUID] = None
    entity_id: Optional[UUID] = None
    txn_date: date_type
    value_date: Optional[date_type] = None

    narration_raw: str
    narration_clean: Optional[str] = None
    payment_method: Optional[str] = None
    counterparty: Optional[str] = None
    reference_no: Optional[str] = None
    debit_paise: Optional[int] = None
    credit_paise: Optional[int] = None
    balance_paise: Optional[int] = None
    direction: Direction
    category_id: Optional[UUID] = None
    
    # Boundary display & test compatibility helpers
    debit: Optional[float] = None
    credit: Optional[float] = None
    amount: Optional[float] = None
    balance: Optional[float] = None
    date: Optional[str] = None
    description: Optional[str] = None
    file_id: Optional[UUID] = None
    original_raw_text: Optional[str] = None
    final_category: Optional[str] = None
    transaction_type: Optional[str] = None


    # Ingestion channel the row arrived through (upload / gmail / rpa / aa).
    # Distinct from prediction_source, which describes how it was categorised.
    source_channel: Optional[str] = None

    # The category tree. `category` is level 1, `category_path` the whole path
    # as far as the evidence supported — "Food & Dining > Food Delivery >
    # Zomato". Depth VARIES BY ROW; a two-level path is a complete answer, not a
    # truncated one, so a client must not pad it.
    category: Optional[str] = None
    category_path: Optional[str] = None
    category_confidence: Optional[float] = None

    # How the money moved, and by which rail. Kept out of the category on
    # purpose: naming the rail as a category is what once produced a single
    # "NEFT Transfer" bucket spanning 43 unrelated counterparties.
    flow_type: Optional[str] = None
    transaction_method: Optional[str] = None

    # Who was paid, as distinct from what for. Amazon is a merchant; the
    # category is Shopping.
    merchant: Optional[str] = None

    # The pre-hierarchy label, still what the review queue and the P&L reports
    # speak. `purpose` is its long-standing alias on this response.
    legacy_category: Optional[str] = None
    purpose: Optional[str] = None
    event_type: Optional[str] = None

    # ---- Currency -----------------------------------------------------------
    # booked_* is what the bank posted; original_* is the foreign leg from the
    # advice, present only on cross-border rows. display_* is computed per
    # request from the currency the caller selected and is never stored.
    booked_currency: Optional[str] = "INR"
    original_currency: Optional[str] = None
    original_amount: Optional[float] = None
    fx_rate: Optional[float] = None

    display_currency: Optional[str] = None
    display_symbol: Optional[str] = None
    display_decimals: Optional[int] = None
    display_debit: Optional[float] = None
    display_credit: Optional[float] = None
    display_amount: Optional[float] = None
    display_balance: Optional[float] = None
    display_balance_currency: Optional[str] = None
    # False when the figure was derived from the rate table rather than read off
    # the bank's advice. The UI marks these as approximate.
    display_is_exact: Optional[bool] = None
    # 'txn'     = converted at the rate for this transaction's own date
    # 'current' = converted at today's rate, to show present-day worth
    display_rate_basis: Optional[str] = None

    # Prediction details
    predicted_category: Optional[str] = None
    confidence: Optional[float] = None
    prediction_source: Optional[str] = None
    reasoning: Optional[str] = None
    rule_used: Optional[str] = None
    model_version: Optional[str] = None


    
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

class TransactionRead(TransactionResponse):
    pass


class UserRegister(BaseModel):
    email: str
    password: str
    full_name: Optional[str] = None

class UserLogin(BaseModel):
    email: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class RefreshTokenRequest(BaseModel):
    refresh_token: str

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    reset_token: str
    new_password: str

class UserResponse(BaseModel):
    id: UUID
    email: str
    full_name: Optional[str] = None
    is_active: bool

    model_config = ConfigDict(from_attributes=True)

class UploadedFileResponse(BaseModel):
    file_id: UUID
    user_id: UUID
    filename: str
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    status: str
    uploaded_at: datetime

    model_config = ConfigDict(from_attributes=True)


