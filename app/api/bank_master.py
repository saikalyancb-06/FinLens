from typing import List, Optional
from uuid import UUID
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.user import User
from app.models.entity import Entity, Bank
from app.models.account import Account
from app.utils.security import get_current_user

router = APIRouter(prefix="/v1/bank-master", tags=["Bank Master & Entities"])


# --- Schemas ---

class EntityCreate(BaseModel):
    name: str
    legal_name: Optional[str] = None
    tax_id: Optional[str] = None

class EntityUpdate(BaseModel):
    name: Optional[str] = None
    legal_name: Optional[str] = None
    tax_id: Optional[str] = None
    is_active: Optional[bool] = None

class EntityResponse(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    legal_name: Optional[str] = None
    tax_id: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BankResponse(BaseModel):
    id: UUID
    name: str
    code: str
    swift_code: Optional[str] = None
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class BankAccountResponse(BaseModel):
    id: UUID
    user_id: UUID
    entity_id: Optional[UUID] = None
    entity_name: Optional[str] = None
    bank_id: Optional[UUID] = None
    bank_name: Optional[str] = None
    bank_code: str
    account_number_masked: str
    account_label: Optional[str] = None
    account_type: Optional[str] = None
    currency: str
    balance: Optional[float] = None
    min_balance_paise: Optional[int] = None
    min_balance: Optional[float] = None
    current_balance: Optional[float] = None
    min_balance_status: Optional[str] = None
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Entity Endpoints ---

@router.get("/entities", response_model=List[EntityResponse])
def get_entities(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all business entities for current authenticated user."""
    return db.query(Entity).filter(Entity.user_id == current_user.id).all()


@router.post("/entities", response_model=EntityResponse, status_code=status.HTTP_201_CREATED)
def create_entity(
    payload: EntityCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new business entity for current user."""
    entity = Entity(
        user_id=current_user.id,
        name=payload.name.strip(),
        legal_name=payload.legal_name.strip() if payload.legal_name else None,
        tax_id=payload.tax_id.strip() if payload.tax_id else None
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity


@router.get("/entities/{entity_id}", response_model=EntityResponse)
def get_entity(
    entity_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a single entity by ID owned by current user."""
    entity = db.query(Entity).filter(
        Entity.id == entity_id,
        Entity.user_id == current_user.id
    ).first()
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


@router.patch("/entities/{entity_id}", response_model=EntityResponse)
def update_entity(
    entity_id: UUID,
    payload: EntityUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update entity details for current user."""
    entity = db.query(Entity).filter(
        Entity.id == entity_id,
        Entity.user_id == current_user.id
    ).first()
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    if payload.name is not None:
        entity.name = payload.name.strip()
    if payload.legal_name is not None:
        entity.legal_name = payload.legal_name.strip()
    if payload.tax_id is not None:
        entity.tax_id = payload.tax_id.strip()
    if payload.is_active is not None:
        entity.is_active = payload.is_active

    db.commit()
    db.refresh(entity)
    return entity


@router.delete("/entities/{entity_id}")
def delete_entity(
    entity_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Safely delete entity if no accounts reference it."""
    entity = db.query(Entity).filter(
        Entity.id == entity_id,
        Entity.user_id == current_user.id
    ).first()
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    # Safety check: prevent orphan active accounts
    linked_accounts_count = db.query(Account).filter(
        Account.entity_id == entity_id,
        Account.deleted_at == None
    ).count()
    if linked_accounts_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete entity: {linked_accounts_count} bank account(s) are linked to it. Reassign or un-link accounts first."
        )

    db.delete(entity)
    db.commit()
    return {"status": "success", "message": "Entity deleted successfully"}


class BankAccountCreate(BaseModel):
    entity_id: Optional[UUID] = None
    bank_id: Optional[UUID] = None
    bank_code: Optional[str] = None
    account_number: str
    account_type: Optional[str] = "CURRENT"
    currency: Optional[str] = "INR"
    account_label: Optional[str] = None


class BankAccountUpdate(BaseModel):
    entity_id: Optional[UUID] = None
    bank_id: Optional[UUID] = None
    bank_code: Optional[str] = None
    account_type: Optional[str] = None
    account_label: Optional[str] = None
    min_balance: Optional[float] = None
    min_balance_paise: Optional[int] = None


# --- Bank Master Endpoints ---

@router.get("/banks", response_model=List[BankResponse])
def get_banks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get list of supported banks in Bank Master."""
    return db.query(Bank).filter(Bank.is_active == True).all()


@router.get("/accounts", response_model=List[BankAccountResponse])
def get_user_bank_accounts(
    entity_id: Optional[UUID] = Query(None),
    bank_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get bank accounts belonging ONLY to authenticated current user."""
    from app.models.transaction import Transaction

    query = db.query(Account).filter(
        Account.user_id == current_user.id,
        Account.deleted_at == None
    )

    if entity_id:
        query = query.filter(Account.entity_id == entity_id)
    if bank_id:
        query = query.filter(Account.bank_id == bank_id)

    accounts = query.all()

    output = []
    for a in accounts:
        ent_name = a.entity.name if a.entity else None
        bank_name = a.bank.name if a.bank else a.bank_code

        # Authoritative latest balance from statements
        latest_bal = db.query(Transaction.balance_paise).filter(
            Transaction.account_id == a.id,
            Transaction.user_id == current_user.id,
            Transaction.balance_paise.isnot(None),
            Transaction.superseded_by_id.is_(None)
        ).order_by(Transaction.txn_date.desc(), Transaction.row_index.desc()).first()

        curr_bal_f = (latest_bal[0] / 100.0) if latest_bal and latest_bal[0] is not None else None
        min_bal_f = (a.min_balance_paise / 100.0) if a.min_balance_paise is not None else None
        status = "not_set" if min_bal_f is None else ("compliant" if (curr_bal_f is not None and curr_bal_f >= min_bal_f) else "below_minimum")

        output.append(BankAccountResponse(
            id=a.id,
            user_id=a.user_id,
            entity_id=a.entity_id,
            entity_name=ent_name,
            bank_id=a.bank_id,
            bank_name=bank_name,
            bank_code=a.bank_code,
            account_number_masked=a.account_number_masked,
            account_label=a.account_label,
            account_type=a.account_type or "CURRENT",
            currency=a.currency or "INR",
            balance=curr_bal_f,
            min_balance_paise=a.min_balance_paise,
            min_balance=min_bal_f,
            current_balance=curr_bal_f,
            min_balance_status=status,
            updated_at=a.updated_at
        ))

    return output


@router.post("/accounts", response_model=BankAccountResponse, status_code=status.HTTP_201_CREATED)
def create_bank_account(
    payload: BankAccountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new Bank Account for the current authenticated user."""
    # 1. Verify entity ownership if provided
    entity = None
    if payload.entity_id:
        entity = db.query(Entity).filter(
            Entity.id == payload.entity_id,
            Entity.user_id == current_user.id
        ).first()
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found or access denied")

    # 2. Verify bank if bank_id provided
    bank = None
    bank_code = payload.bank_code or "BANK"
    if payload.bank_id:
        bank = db.query(Bank).filter(Bank.id == payload.bank_id).first()
        if not bank:
            raise HTTPException(status_code=404, detail="Bank not found")
        bank_code = bank.code

    # Mask account number (9 to 18 digits)
    import re
    raw_num = payload.account_number.strip()
    if not re.match(r'^\d{9,18}$', raw_num):
        raise HTTPException(
            status_code=400,
            detail="Account number must be between 9 and 18 digits (e.g. 502000123456)."
        )
    masked = f"****{raw_num[-4:]}"

    account = Account(
        user_id=current_user.id,
        entity_id=entity.id if entity else None,
        bank_id=bank.id if bank else None,
        bank_code=bank_code,
        account_number_masked=masked,
        account_type=payload.account_type.upper() if payload.account_type else "CURRENT",
        currency=payload.currency.upper() if payload.currency else "INR",
        account_label=payload.account_label.strip() if payload.account_label else None
    )
    db.add(account)
    db.commit()
    db.refresh(account)

    ent_name = account.entity.name if account.entity else None
    bank_name = account.bank.name if account.bank else account.bank_code

    return BankAccountResponse(
        id=account.id,
        user_id=account.user_id,
        entity_id=account.entity_id,
        entity_name=ent_name,
        bank_id=account.bank_id,
        bank_name=bank_name,
        bank_code=account.bank_code,
        account_number_masked=account.account_number_masked,
        account_label=account.account_label,
        account_type=account.account_type or "CURRENT",
        currency=account.currency or "INR",
        balance=None,
        updated_at=account.updated_at
    )


@router.patch("/accounts/{account_id}", response_model=BankAccountResponse)
def update_bank_account(
    account_id: UUID,
    payload: BankAccountUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Assign or update entity / metadata on an existing owned Bank Account."""
    account = db.query(Account).filter(
        Account.id == account_id,
        Account.user_id == current_user.id,
        Account.deleted_at == None
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found or access denied")

    if payload.entity_id is not None:
        if payload.entity_id:
            entity = db.query(Entity).filter(
                Entity.id == payload.entity_id,
                Entity.user_id == current_user.id
            ).first()
            if not entity:
                raise HTTPException(status_code=404, detail="Entity not found or access denied")
            account.entity_id = entity.id
        else:
            account.entity_id = None

    if payload.bank_id is not None:
        if payload.bank_id:
            bank = db.query(Bank).filter(Bank.id == payload.bank_id).first()
            if not bank:
                raise HTTPException(status_code=404, detail="Bank not found")
            account.bank_id = bank.id
            account.bank_code = bank.code
        else:
            account.bank_id = None
            if payload.bank_code:
                account.bank_code = payload.bank_code.strip()

    if payload.account_type is not None:
        account.account_type = payload.account_type.upper()
    if payload.account_label is not None:
        account.account_label = payload.account_label.strip()
    if "min_balance" in payload.model_fields_set:
        if payload.min_balance is not None:
            account.min_balance_paise = round(payload.min_balance * 100)
        else:
            account.min_balance_paise = None
    elif "min_balance_paise" in payload.model_fields_set:
        account.min_balance_paise = payload.min_balance_paise

    db.commit()
    db.refresh(account)

    ent_name = account.entity.name if account.entity else None
    bank_name = account.bank.name if account.bank else account.bank_code

    from app.models.transaction import Transaction
    latest_bal = db.query(Transaction.balance_paise).filter(
        Transaction.account_id == account.id,
        Transaction.user_id == current_user.id,
        Transaction.balance_paise.isnot(None),
        Transaction.superseded_by_id.is_(None)
    ).order_by(Transaction.txn_date.desc(), Transaction.row_index.desc()).first()

    curr_bal_f = (latest_bal[0] / 100.0) if latest_bal and latest_bal[0] is not None else None
    min_bal_f = (account.min_balance_paise / 100.0) if account.min_balance_paise is not None else None
    status = "not_set" if min_bal_f is None else ("compliant" if (curr_bal_f is not None and curr_bal_f >= min_bal_f) else "below_minimum")

    return BankAccountResponse(
        id=account.id,
        user_id=account.user_id,
        entity_id=account.entity_id,
        entity_name=ent_name,
        bank_id=account.bank_id,
        bank_name=bank_name,
        bank_code=account.bank_code,
        account_number_masked=account.account_number_masked,
        account_label=account.account_label,
        account_type=account.account_type or "CURRENT",
        currency=account.currency or "INR",
        balance=curr_bal_f,
        min_balance_paise=account.min_balance_paise,
        min_balance=min_bal_f,
        current_balance=curr_bal_f,
        min_balance_status=status,
        updated_at=account.updated_at
    )


@router.delete("/accounts/{account_id}")
def delete_bank_account(
    account_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Safely and atomically delete a bank account owned by current user along with all exclusive dependent data."""
    from app.models.transaction import Transaction
    from app.models.prediction import Prediction
    from app.models.statement import Statement
    from app.models.uploaded_file import UploadedFile
    from app.models.duplicate_match import DuplicateMatch
    from app.models.reconciliation import (
        ImportBatch, BookEntry, ReconciliationRun, ReconciliationMatch,
        ReconciliationMatchLine, ReconciliationItem
    )

    # 1. Verify user ownership & entity ownership
    account = db.query(Account).filter(
        Account.id == account_id,
        Account.user_id == current_user.id
    ).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found or access denied")

    if account.entity_id:
        entity = db.query(Entity).filter(
            Entity.id == account.entity_id,
            Entity.user_id == current_user.id
        ).first()
        if not entity:
            raise HTTPException(status_code=403, detail="Access denied to entity associated with account")

    try:
        # 2. Identify all transactions for this account
        account_tx_ids = [
            t.id for t in db.query(Transaction.id).filter(
                Transaction.user_id == current_user.id,
                Transaction.account_id == account_id
            ).all()
        ]

        if account_tx_ids:
            # 2a. Reset superseded_by_id on ANY transaction outside this account that pointed to these deleted txs
            db.query(Transaction).filter(
                Transaction.user_id == current_user.id,
                Transaction.superseded_by_id.in_(account_tx_ids)
            ).update({Transaction.superseded_by_id: None}, synchronize_session=False)

            # 2b. Delete Predictions for these transactions
            db.query(Prediction).filter(
                Prediction.transaction_id.in_(account_tx_ids)
            ).delete(synchronize_session=False)

            # 2c. Delete DuplicateMatch records involving these transactions (either duplicate or kept)
            db.query(DuplicateMatch).filter(
                DuplicateMatch.user_id == current_user.id,
                (DuplicateMatch.duplicate_txn_id.in_(account_tx_ids)) | (DuplicateMatch.kept_txn_id.in_(account_tx_ids))
            ).delete(synchronize_session=False)

            # 2d. Delete ReconciliationMatchLines referencing these bank transactions
            db.query(ReconciliationMatchLine).filter(
                ReconciliationMatchLine.bank_txn_id.in_(account_tx_ids)
            ).delete(synchronize_session=False)

        # 3. Identify and delete Reconciliation runs & dependent matches/items
        run_ids = [
            r.id for r in db.query(ReconciliationRun.id).filter(
                ReconciliationRun.user_id == current_user.id,
                ReconciliationRun.account_id == account_id
            ).all()
        ]
        if run_ids:
            db.query(ReconciliationMatch).filter(ReconciliationMatch.run_id.in_(run_ids)).delete(synchronize_session=False)
            db.query(ReconciliationItem).filter(
                ReconciliationItem.user_id == current_user.id,
                ReconciliationItem.run_id.in_(run_ids)
            ).delete(synchronize_session=False)
            db.query(ReconciliationRun).filter(
                ReconciliationRun.user_id == current_user.id,
                ReconciliationRun.account_id == account_id
            ).delete(synchronize_session=False)

        # 4. Delete Book entries and Import batches for this account
        db.query(BookEntry).filter(
            BookEntry.user_id == current_user.id,
            BookEntry.account_id == account_id
        ).delete(synchronize_session=False)

        db.query(ImportBatch).filter(
            ImportBatch.user_id == current_user.id,
            ImportBatch.account_id == account_id
        ).delete(synchronize_session=False)

        # 5. Delete Transactions for this account
        db.query(Transaction).filter(
            Transaction.user_id == current_user.id,
            Transaction.account_id == account_id
        ).delete(synchronize_session=False)

        # 6. Identify statements and clean up account-specific uploaded files
        statements = db.query(Statement).filter(
            Statement.user_id == current_user.id,
            Statement.account_id == account_id
        ).all()
        uploaded_file_ids = [s.uploaded_file_id for s in statements if s.uploaded_file_id]

        db.query(Statement).filter(
            Statement.user_id == current_user.id,
            Statement.account_id == account_id
        ).delete(synchronize_session=False)

        # Delete UploadedFile if no other statements reference it
        for uf_id in set(uploaded_file_ids):
            other_refs = db.query(Statement.id).filter(
                Statement.uploaded_file_id == uf_id
            ).count()
            if other_refs == 0:
                db.query(UploadedFile).filter(
                    UploadedFile.id == uf_id,
                    UploadedFile.user_id == current_user.id
                ).delete(synchronize_session=False)

        # 7. Delete the Bank Account itself
        db.delete(account)
        db.commit()

        return {
            "status": "success",
            "message": "Bank account and all dependent transactions, statements, and reconciliation records deleted successfully."
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete bank account atomically: {str(e)}"
        )


