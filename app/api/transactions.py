from typing import List, Optional
from uuid import UUID
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.database.session import get_db
from app.models.transaction import Transaction, Direction, SourceType
from app.models.prediction import Prediction
from app.models.user import User
from app.models.account import Account
from app.models.statement import Statement
from app.api.schemas import TransactionCreate, TransactionResponse
from app.utils.security import get_current_user
from app.currency.display import (
    ACTUAL, RATE_BASES, RATE_BASIS_CURRENT, RATE_BASIS_TXN,
    DisplayBlock, build_display, resolve_target,
)
from app.api.scoping import entity_scope
from app.currency.service import (
    BASE_CURRENCY, DECIMALS_BY_CODE, currency_map, format_minor, rate_on,
    rates_for_dates,
)


router = APIRouter(prefix="/transactions", tags=["Transactions"])


def display_category_label(Category, Prediction):
    """SQL for the category label the Transactions table displays.

    Mirrors the table cell `t.category || t.purpose || t.final_category`:
    the tree category, else the flat (legacy) label, else the linked category
    name, else the prediction, else "Uncategorized" (final_category's default).
    Used by the ?category= filter and by /analytics/filters so the dropdown,
    the filter and the column are one vocabulary.
    """
    return func.coalesce(
        func.nullif(Transaction.category, ""),
        func.nullif(Transaction.legacy_category, ""),
        Category.name,
        Prediction.predicted_category,
        "Uncategorized",
    )


def build_transaction_response(
    tx: Transaction,
    display: "DisplayBlock | None" = None,
) -> TransactionResponse:
    deb_paise = tx.debit_paise or 0
    cred_paise = tx.credit_paise or 0
    deb_f = (deb_paise / 100.0) if tx.debit_paise is not None else 0.0
    cred_f = (cred_paise / 100.0) if tx.credit_paise is not None else 0.0
    amt_f = deb_f if deb_f > 0 else cred_f
    bal_f = (tx.balance_paise / 100.0) if tx.balance_paise is not None else None

    pred_cat = None
    conf_val = None
    rule_val = None
    model_ver = None
    reasoning_str = "Canonical Transaction record"

    # prediction_source describes HOW the row was categorised ("Rule Engine" /
    # "ML Model"), which is what the term means everywhere else in the codebase
    # (see app/ai/decision_engine.py and ProcessedTransaction.prediction_source).
    # It must not be filled with the ingestion channel — that is source_channel,
    # reported separately below.
    pred_src = None

    # The resolved Category is the authoritative label when present.
    if tx.category_node:
        pred_cat = tx.category_node.name

    if tx.prediction:
        # The Prediction row carries the real provenance, so it is consulted even
        # when a Category is attached: reading only the Category loses the rule /
        # model that produced it and reports a fabricated confidence of 1.0.
        pred_cat = pred_cat or tx.prediction.predicted_category
        raw_conf = tx.prediction.confidence
        model_conf = getattr(tx.prediction, 'model_confidence', None)
        if raw_conf is not None and raw_conf > 0:
            conf_val = raw_conf
        elif model_conf is not None and model_conf > 0:
            conf_val = model_conf
        elif pred_cat and pred_cat != "Uncategorized":
            conf_val = 0.95
        else:
            conf_val = 0.0
        rule_val = tx.prediction.rule_used
        model_ver = tx.prediction.model_version
        pred_src = "Rule Engine" if tx.prediction.rule_used else "ML Model"
        reasoning_str = f"Rule: {tx.prediction.rule_used}" if tx.prediction.rule_used else "ML Prediction"
    elif tx.category_node:
        # Category set with no Prediction row: assigned directly (manual edit or
        # import mapping) rather than inferred by a classifier.
        conf_val = 1.0
        pred_src = "Manual"
        reasoning_str = "Category assigned directly"
    elif pred_cat and pred_cat != "Uncategorized":
        conf_val = 0.95

    return TransactionResponse(
        id=tx.id,
        user_id=tx.user_id,
        account_id=tx.account_id,
        statement_id=tx.statement_id,
        entity_id=tx.entity_id,
        file_id=tx.statement_id,
        txn_date=tx.txn_date,
        value_date=tx.value_date,
        narration_raw=tx.narration_raw,
        original_raw_text=tx.narration_raw,
        narration_clean=tx.narration_clean,
        payment_method=tx.payment_method,
        counterparty=tx.counterparty,
        reference_no=tx.reference_no,
        debit_paise=tx.debit_paise,
        credit_paise=tx.credit_paise,
        balance_paise=tx.balance_paise,
        direction=tx.direction,
        category_id=tx.category_id,
        debit=deb_f,
        credit=cred_f,
        amount=amt_f,
        balance=bal_f,
        date=tx.txn_date.isoformat() if tx.txn_date else None,
        description=tx.narration_clean or tx.narration_raw,
        final_category=pred_cat or "Uncategorized",
        transaction_type=tx.payment_method or tx.direction.value,
        source_channel=tx.source_channel,
        purpose=tx.legacy_category,
        legacy_category=tx.legacy_category,
        category=tx.category,
        category_path=tx.category_path,
        category_confidence=(float(tx.category_confidence)
                             if tx.category_confidence is not None else None),
        flow_type=tx.flow_type,
        transaction_method=tx.transaction_method,
        merchant=tx.merchant,
        event_type=tx.event_type,
        booked_currency=getattr(tx, "booked_currency", None) or "INR",
        original_currency=tx.original_currency,
        original_amount=(
            format_minor(tx.original_amount_minor,
                         DECIMALS_BY_CODE.get((tx.original_currency or "").upper(), 2))
            if tx.original_amount_minor is not None else None
        ),
        fx_rate=float(tx.fx_rate) if tx.fx_rate is not None else None,
        display_currency=display.currency if display else None,
        display_symbol=display.symbol if display else None,
        display_decimals=display.decimals if display else None,
        display_debit=display.debit if display else None,
        display_credit=display.credit if display else None,
        display_amount=display.amount if display else None,
        display_balance=display.balance if display else None,
        display_balance_currency=display.balance_currency if display else None,
        display_is_exact=display.is_exact if display else None,
        display_rate_basis=display.rate_basis if display else None,
        predicted_category=pred_cat,
        confidence=conf_val,
        prediction_source=pred_src,
        reasoning=reasoning_str,
        rule_used=rule_val,
        model_version=model_ver or "v1.0.0",
        created_at=tx.created_at or datetime.utcnow(),
        updated_at=tx.updated_at or datetime.utcnow()
    )




@router.post("/", response_model=TransactionResponse, status_code=status.HTTP_201_CREATED)
def create_transaction(
    payload: TransactionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # 1. Account Ownership Verification
    if payload.account_id:
        acc = db.query(Account).filter(
            Account.id == payload.account_id,
            Account.user_id == current_user.id
        ).first()
        if not acc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Account not found or access denied"
            )

    # 2. Statement Ownership Verification
    if payload.statement_id:
        stmt = db.query(Statement).filter(
            Statement.id == payload.statement_id,
            Statement.user_id == current_user.id
        ).first()
        if not stmt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Statement not found or access denied"
            )

    # 3. Money & Direction Validation
    d_paise = payload.debit_paise or 0
    c_paise = payload.credit_paise or 0

    if (payload.debit_paise is not None and payload.debit_paise < 0) or (payload.credit_paise is not None and payload.credit_paise < 0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debit or credit paise cannot be negative"
        )

    if d_paise > 0 and c_paise == 0:
        calc_direction = Direction.DEBIT
    elif c_paise > 0 and d_paise == 0:
        calc_direction = Direction.CREDIT
    elif payload.direction is not None:
        calc_direction = payload.direction
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid transaction money values: exactly one of debit_paise or credit_paise must be > 0"
        )

    # 4. Create Canonical Transaction
    db_tx = Transaction(
        user_id=current_user.id,
        account_id=payload.account_id,
        statement_id=payload.statement_id,
        entity_id=payload.entity_id,
        txn_date=payload.txn_date,
        value_date=payload.value_date,
        narration_raw=payload.narration_raw,
        narration_clean=payload.narration_clean or payload.narration_raw,
        payment_method=payload.payment_method,
        counterparty=payload.counterparty,
        reference_no=payload.reference_no,
        debit_paise=payload.debit_paise if calc_direction == Direction.DEBIT else None,
        credit_paise=payload.credit_paise if calc_direction == Direction.CREDIT else None,
        balance_paise=payload.balance_paise,
        direction=calc_direction,
        category_id=payload.category_id,
        source_type=SourceType.STATEMENT
    )
    db.add(db_tx)
    db.flush()

    if payload.prediction:
        db_pred = Prediction(
            transaction_id=db_tx.id,
            predicted_category=payload.prediction.predicted_category,
            confidence=payload.prediction.confidence,
            rule_used=payload.prediction.rule_used,
            model_version=payload.prediction.model_version or "v1.0.0",
        )
        db.add(db_pred)

    db.commit()
    db.refresh(db_tx)

    return build_transaction_response(db_tx)


@router.get("", response_model=List[TransactionResponse])
@router.get("/", response_model=List[TransactionResponse])
def get_transactions_list(
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    statement_id: Optional[UUID] = Query(None),
    file_id: Optional[UUID] = Query(None),
    search: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    transaction_type: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    entity_id: Optional[UUID] = Query(None),
    bank_id: Optional[UUID] = Query(None),
    display_currency: Optional[str] = Query(
        None,
        description=("Currency to show amounts in: an ISO code (INR, USD, GBP...) "
                     "or 'actual' to show each row in the currency it was "
                     "contracted in. Omit to skip conversion entirely."),
    ),
    rate_basis: str = Query(
        RATE_BASIS_TXN,
        description=("Which day's rate to convert at: 'txn' uses the rate that "
                     "applied on each transaction's own date (the accounting "
                     "answer), 'current' values every row at today's rate (what "
                     "it is worth now). Ignored when display_currency is omitted "
                     "or set to 'actual'."),
    ),
    limit: int = Query(
        250, ge=1, le=50000,
        description=("Rows to return. The default is one page, not the whole "
                     "ledger: this used to default to 10,000, and the browser "
                     "took that literally on every mount — 3.6 MB and roughly 21 "
                     "seconds of JSON serialisation that also starved every "
                     "other request on the page. Read X-Total-Count to page."),
    ),
    offset: int = Query(0, ge=0)
):
    query = db.query(Transaction).filter(
        Transaction.user_id == current_user.id,
        Transaction.superseded_by_id == None
    )

    if entity_id:
        query = query.filter(entity_scope(entity_id))

    if bank_id and type(bank_id).__name__ != 'Depends':
        from app.models.account import Account
        import uuid as uuid_mod
        try:
            b_uuid = uuid_mod.UUID(str(bank_id)) if isinstance(bank_id, (str, uuid_mod.UUID)) else bank_id
        except (ValueError, TypeError, AttributeError):
            b_uuid = None

        if b_uuid:
            # Allow bank_id to be either an Account.id (specific account) or Bank.id (all accounts for a bank)
            acc_by_id = db.query(Account).filter(Account.id == b_uuid, Account.user_id == current_user.id).first()
            if acc_by_id:
                query = query.filter(Transaction.account_id == acc_by_id.id)
            else:
                bank_accs = db.query(Account).filter(
                    Account.user_id == current_user.id,
                    Account.bank_id == b_uuid
                ).all()
                bank_acc_ids = [a.id for a in bank_accs]
                if bank_acc_ids:
                    query = query.filter(Transaction.account_id.in_(bank_acc_ids))
                else:
                    query = query.filter(Transaction.account_id == b_uuid)

    stmt_filter_id = statement_id
    if file_id and not stmt_filter_id:
        import uuid as uuid_mod
        file_uuid = uuid_mod.UUID(str(file_id)) if isinstance(file_id, (str, uuid_mod.UUID)) else file_id
        # file_id is UploadedFile.id — resolve to Statement.id via uploaded_file_id or file_sha256
        stmts = db.query(Statement).filter(
            Statement.uploaded_file_id == file_uuid,
            Statement.user_id == current_user.id
        ).all()

        if not stmts:
            from app.models.uploaded_file import UploadedFile
            uf = db.query(UploadedFile).filter(
                UploadedFile.id == file_uuid,
                UploadedFile.user_id == current_user.id
            ).first()
            if uf and uf.file_sha256:
                stmts = db.query(Statement).filter(
                    Statement.file_sha256 == uf.file_sha256,
                    Statement.user_id == current_user.id
                ).all()

        if stmts:
            # Pick statement that contains transactions for this user, or default to first
            stmt_ids = [s.id for s in stmts]
            stmt_with_txns = db.query(Statement).filter(
                Statement.id.in_(stmt_ids),
                Statement.id.in_(
                    db.query(Transaction.statement_id).filter(Transaction.user_id == current_user.id).scalar_subquery()
                )
            ).first()
            stmt_filter_id = stmt_with_txns.id if stmt_with_txns else stmts[0].id

    if stmt_filter_id:
        query = query.filter(Transaction.statement_id == stmt_filter_id)

    if search:
        query = query.filter(
            (Transaction.narration_clean.ilike(f"%{search}%")) |
            (Transaction.narration_raw.ilike(f"%{search}%"))
        )

    # transaction_type was accepted, published in the OpenAPI schema and offered
    # by /analytics/filters as a Debit/Credit dropdown — but never reached a
    # .filter(). A request for debits returned debits AND credits, and every
    # total computed from that response was wrong while looking right.
    if transaction_type:
        tt = transaction_type.strip().lower()
        if tt in ("debit", "credit"):
            query = query.filter(Transaction.direction == Direction(tt))
        else:
            # Anything else is a payment instrument (UPI, NEFT, RTGS…).
            query = query.filter(func.lower(Transaction.payment_method) == tt)

    if start_date:
        try:
            s_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
            query = query.filter(Transaction.txn_date >= s_dt)
        except ValueError:
            pass

    if end_date:
        try:
            e_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
            query = query.filter(Transaction.txn_date <= e_dt)
        except ValueError:
            pass

    # Category filters on the label the Category column SHOWS
    # (`category || purpose || final_category` in the table), resolved in SQL by
    # display_category_label(). It used to match the flat Category.name /
    # predicted category instead, so choosing "Rent Payment" listed rows the
    # column labelled "Transfers". /analytics/filters offers exactly these
    # labels, so every option matches what it says.
    if category:
        from app.models.category import Category
        from app.models.prediction import Prediction
        query = (
            query.outerjoin(Category, Category.id == Transaction.category_id)
                 .outerjoin(Prediction, Prediction.transaction_id == Transaction.id)
                 .filter(func.lower(display_category_label(Category, Prediction)) == category.strip().lower())
        )

    # Counted on the fully-filtered query but BEFORE offset/limit, so it is the
    # size of the result set rather than of the page. Without it the client has
    # no way to know how many rows exist except to ask for all of them, which is
    # exactly the behaviour this replaces.
    #
    # order_by is stripped for the count: PostgreSQL cannot use an index-only
    # path while it is sorting rows it is only going to count.
    total = query.order_by(None).with_entities(func.count()).scalar() or 0
    response.headers["X-Total-Count"] = str(total)

    txns = query.order_by(Transaction.txn_date.desc()).offset(offset).limit(limit).all()

    if not display_currency:
        return [build_transaction_response(t) for t in txns]

    if rate_basis not in RATE_BASES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"rate_basis must be one of {', '.join(RATE_BASES)}",
        )

    return _with_display(db, txns, display_currency, rate_basis)


def _with_display(db: Session, txns: List[Transaction], requested: str,
                  rate_basis: str = RATE_BASIS_TXN):
    """Attach display amounts, resolving each currency's rate history once.

    Converting per row would issue a query per row. Here every currency that
    actually appears in the page is loaded once and the per-date lookup is a
    bisect, so a 50,000-row page costs a fixed handful of queries.
    """
    meta = {c.code: (c.symbol, c.decimals) for c in currency_map(db).values()}

    targets = {
        resolve_target(requested,
                       getattr(t, "booked_currency", None) or BASE_CURRENCY,
                       t.original_currency)
        for t in txns
    }
    needed = targets | {getattr(t, "booked_currency", None) or BASE_CURRENCY for t in txns}
    unknown = sorted(c for c in targets if c not in meta)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown display currency: {', '.join(unknown)}",
        )

    if rate_basis == RATE_BASIS_CURRENT:
        # One rate per currency, the newest on file, applied to every row. Asked
        # for once rather than per date, because the whole point of this basis is
        # that the date does not enter into it.
        today_rate = {code: rate_on(db, code) for code in needed}
        rate_tables = None
    else:
        dates = {t.txn_date for t in txns}
        rate_tables = {code: rates_for_dates(db, code, dates) for code in needed}

    out = []
    for t in txns:
        booked = getattr(t, "booked_currency", None) or BASE_CURRENCY
        rate_of = (dict(today_rate) if rate_tables is None
                   else {code: rate_tables[code].get(t.txn_date) for code in needed})
        block = build_display(
            requested=requested,
            booked_currency=booked,
            original_currency=t.original_currency,
            original_amount_minor=t.original_amount_minor,
            debit_minor=t.debit_paise,
            credit_minor=t.credit_paise,
            balance_minor=t.balance_paise,
            direction_is_debit=(t.direction == Direction.DEBIT),
            meta=meta,
            rate_of=rate_of,
            rate_basis=rate_basis,
        )
        out.append(build_transaction_response(t, display=block))
    return out


@router.get("/{transaction_id}", response_model=TransactionResponse)
def get_transaction(
    transaction_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    tx = db.query(Transaction).filter(
        Transaction.id == transaction_id,
        Transaction.user_id == current_user.id,
        Transaction.superseded_by_id == None
    ).first()

    if not tx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found"
        )

    return build_transaction_response(tx)


@router.delete("/clear")
def clear_all_transactions(
    account_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Clear all transactions, statements, book entries, imports, email history, reconciliation runs & matches for current user/account."""
    from app.models.processed_transaction import ProcessedTransaction
    from app.models.statement import Statement
    from app.models.uploaded_file import UploadedFile
    from app.models.duplicate_match import DuplicateMatch
    from app.email.models import ImportHistory, EmailAttachment
    from app.models.reconciliation import (
        ImportBatch, BookEntry, ReconciliationRun, ReconciliationMatch,
        ReconciliationMatchLine, ReconciliationItem
    )
    from app.models.compliance import AnomalyFinding, PolicyRule, PolicyViolation

    tx_q = db.query(Transaction.id).filter(Transaction.user_id == current_user.id)
    if account_id:
        tx_q = tx_q.filter(Transaction.account_id == account_id)
    tx_ids = [row[0] for row in tx_q.all()]

    # 1. Delete Predictions
    if tx_ids:
        db.query(Prediction).filter(Prediction.transaction_id.in_(tx_ids)).delete(synchronize_session=False)

    # 2. Delete ReconciliationMatchLines
    if tx_ids:
        db.query(ReconciliationMatchLine).filter(ReconciliationMatchLine.bank_txn_id.in_(tx_ids)).delete(synchronize_session=False)

    # 3. Delete ReconciliationMatches
    run_q = db.query(ReconciliationRun.id).filter(ReconciliationRun.user_id == current_user.id)
    if account_id:
        run_q = run_q.filter(ReconciliationRun.account_id == account_id)
    run_ids = [row[0] for row in run_q.all()]

    if run_ids:
        db.query(ReconciliationMatch).filter(ReconciliationMatch.run_id.in_(run_ids)).delete(synchronize_session=False)

    # 4. Delete ReconciliationItems
    item_q = db.query(ReconciliationItem).filter(ReconciliationItem.user_id == current_user.id)
    if account_id:
        # Guarding on `account_id and run_ids` meant that when an account had no
        # reconciliation runs, run_ids was empty, the condition was False, and
        # this deleted EVERY ReconciliationItem the user owned — an
        # account-scoped clear silently wiping other accounts' reconciliation.
        item_q = item_q.filter(ReconciliationItem.run_id.in_(run_ids)) if run_ids else item_q.filter(False)
    item_q.delete(synchronize_session=False)

    # 5. Delete ReconciliationRuns
    run_del_q = db.query(ReconciliationRun).filter(ReconciliationRun.user_id == current_user.id)
    if account_id:
        run_del_q = run_del_q.filter(ReconciliationRun.account_id == account_id)
    run_del_q.delete(synchronize_session=False)

    # 6. Delete BookEntries
    book_q = db.query(BookEntry).filter(BookEntry.user_id == current_user.id)
    if account_id:
        book_q = book_q.filter(BookEntry.account_id == account_id)
    book_q.delete(synchronize_session=False)

    # 7. Delete ImportBatches
    batch_q = db.query(ImportBatch).filter(ImportBatch.user_id == current_user.id)
    if account_id:
        batch_q = batch_q.filter(ImportBatch.account_id == account_id)
    batch_q.delete(synchronize_session=False)

    # 8. Delete DuplicateMatch pairs.
    # Scoped to the transactions actually being cleared: filtering only by user_id
    # would wipe the duplicate history of every other account the user owns.
    dup_q = db.query(DuplicateMatch).filter(DuplicateMatch.user_id == current_user.id)
    if account_id:
        if tx_ids:
            dup_q = dup_q.filter(
                or_(
                    DuplicateMatch.duplicate_txn_id.in_(tx_ids),
                    DuplicateMatch.kept_txn_id.in_(tx_ids),
                )
            )
        else:
            dup_q = dup_q.filter(False)
    dup_q.delete(synchronize_session=False)

    # 9. Delete ProcessedTransactions.
    # ProcessedTransaction has no account_id, so an account-scoped clear resolves
    # the owning account through the Statement that links the uploaded file.
    # Without this, clearing one account destroys the processed rows of all of them.
    pt_q = db.query(ProcessedTransaction).filter(ProcessedTransaction.user_id == current_user.id)
    if account_id:
        account_file_ids = [
            row[0]
            for row in db.query(Statement.uploaded_file_id).filter(
                Statement.user_id == current_user.id,
                Statement.account_id == account_id,
                Statement.uploaded_file_id != None,
            ).all()
        ]
        if account_file_ids:
            pt_q = pt_q.filter(ProcessedTransaction.file_id.in_(account_file_ids))
        else:
            pt_q = pt_q.filter(False)
    pt_q.delete(synchronize_session=False)

    # 10. Delete Transactions
    del_tx_q = db.query(Transaction).filter(Transaction.user_id == current_user.id)
    if account_id:
        del_tx_q = del_tx_q.filter(Transaction.account_id == account_id)
    deleted_count = del_tx_q.delete(synchronize_session=False)

    # 11. Delete Statements
    stmt_q = db.query(Statement).filter(Statement.user_id == current_user.id)
    if account_id:
        stmt_q = stmt_q.filter(Statement.account_id == account_id)
    stmt_q.delete(synchronize_session=False)

    # 12. Delete ImportHistory
    hist_q = db.query(ImportHistory).filter(ImportHistory.user_id == current_user.id)
    if account_id:
        hist_q = hist_q.filter(ImportHistory.account_id == account_id)
    hist_q.delete(synchronize_session=False)

    # 13. Delete EmailAttachments
    att_q = db.query(EmailAttachment).filter(EmailAttachment.user_id == current_user.id)
    if account_id:
        att_q = att_q.filter(EmailAttachment.account_id == account_id)
    att_q.delete(synchronize_session=False)

    # 14. Delete UploadedFiles.
    # Must honour account_id like every other step: deleting unconditionally made
    # an account-scoped clear remove the file history of all the user's other
    # accounts. UploadedFile has no account_id, so scope is resolved through the
    # Statement that links the file to an account.
    file_q = db.query(UploadedFile).filter(UploadedFile.user_id == current_user.id)
    if account_id:
        file_q = file_q.filter(UploadedFile.id.in_(account_file_ids)) if account_file_ids else file_q.filter(False)
    file_q.delete(synchronize_session=False)

    # 15. Delete compliance findings.
    #
    # Both tables declare ON DELETE CASCADE from transaction_id, so the findings
    # that name a specific transaction do go when it does. The ones that do not
    # name a transaction survive — and that is most of the interesting ones:
    # balance discontinuities, structuring patterns, unreconciled totals and
    # account-level policy breaches are properties of a *set* of rows, so they
    # carry no single transaction_id for the database to follow.
    #
    # Measured on a real clear: 121 transactions deleted, 13 anomalies and 3
    # violations left behind, still shown on the dashboard as live findings
    # about data that no longer exists.
    anomaly_q = db.query(AnomalyFinding).filter(AnomalyFinding.user_id == current_user.id)
    violation_q = db.query(PolicyViolation).filter(PolicyViolation.user_id == current_user.id)
    if account_id:
        # Scoped clear: only findings belonging to that account. Findings with no
        # account (user-wide patterns) are left alone, because they may well be
        # about the accounts that were not cleared.
        anomaly_q = anomaly_q.filter(AnomalyFinding.account_id == account_id)
        violation_q = violation_q.filter(PolicyViolation.account_id == account_id)
    anomaly_q.delete(synchronize_session=False)
    violation_q.delete(synchronize_session=False)

    # 16. Reset the cached per-rule scan statistics.
    #
    # The dashboard's compliance percentage is a plain read of these columns, so
    # leaving them populated reported a pass rate computed over transactions
    # that have just been deleted. Nulling them makes the dashboard say "not
    # evaluated" until the next scan, which is true, instead of showing a
    # confident figure that is not.
    db.query(PolicyRule).filter(PolicyRule.user_id == current_user.id).update(
        {
            PolicyRule.last_applicable: None,
            PolicyRule.last_violations: None,
            PolicyRule.last_evaluated_at: None,
        },
        synchronize_session=False,
    )

    db.commit()

    return {
        "status": "success",
        "message": "All transactions and reconciliation records cleared successfully.",
        "deleted_count": deleted_count
    }

