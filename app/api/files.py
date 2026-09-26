import logging
import os
from typing import List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, UploadFile, File, Form, BackgroundTasks, HTTPException, status
from sqlalchemy.orm import Session
from app.database.session import get_db
from app.models.user import User
from app.models.uploaded_file import UploadedFile
from app.api.schemas import UploadedFileResponse
from app.services.file_service import save_uploaded_file
from app.services.parsing_queue import process_file_parsing_task
from app.utils.security import get_current_user
from app.utils.metrics import metrics_collector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["File Upload & Management"])


def _has_ledger_rows(db: Session, file_id: UUID) -> bool:
    """Did this uploaded file actually put transactions in the canonical ledger?

    'COMPLETED' on the UploadedFile row records how far the parsing job got,
    which is not the same claim as 'the user has their statement'. The
    Transactions tab, the dashboard and every report read the transactions
    table, so that is the table this asks about.
    """
    from app.models.statement import Statement
    from app.models.transaction import Transaction

    return db.query(Transaction.id).join(
        Statement, Transaction.statement_id == Statement.id
    ).filter(Statement.uploaded_file_id == file_id).first() is not None


@router.post("/upload", response_model=UploadedFileResponse, status_code=status.HTTP_202_ACCEPTED)
def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    account_id: Optional[UUID] = None,
    entity_id: Optional[UUID] = None,
    pdf_password: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        # Verify user has at least 1 active registered bank account before accepting uploads
        from app.models.account import Account
        active_account_count = db.query(Account).filter(
            Account.user_id == current_user.id,
            Account.deleted_at == None
        ).count()
        if active_account_count == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="NO_BANK_ACCOUNT: You must add at least one registered bank account in Bank Master before uploading bank statements."
            )

        # 1. Save uploaded file to disk, compute SHA-256 & validate extension
        target_path, stored_filename, file_size, file_sha256 = save_uploaded_file(file, str(current_user.id))
        metrics_collector.record_upload(success=True)
    except Exception as e:
        metrics_collector.record_upload(success=False)
        raise e

    # Check if uploaded file is a password-protected PDF
    _, ext = os.path.splitext((file.filename or stored_filename).lower())
    clean_pwd = pdf_password.strip() if pdf_password and pdf_password.strip() else pdf_password

    if ext == ".pdf":
        try:
            import fitz
            doc = fitz.open(target_path)
            if doc.is_encrypted:
                authenticated = False
                if clean_pwd:
                    auth_res = doc.authenticate(clean_pwd)
                    if auth_res > 0:
                        authenticated = True
                    else:
                        try:
                            import pdfplumber
                            with pdfplumber.open(target_path, password=clean_pwd) as pl_pdf:
                                authenticated = True
                        except Exception:
                            authenticated = False

                    if not authenticated:
                        doc.close()
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="PASSWORD_INVALID: Invalid PDF document password."
                        )
                else:
                    auth_res = doc.authenticate("")
                    if auth_res == 0:
                        doc.close()
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="PDF_PASSWORD_REQUIRED: PDF attachment is password-protected and requires a document password to unlock."
                        )
            doc.close()
        except HTTPException:
            raise
        except Exception as pe:
            logger.warning(f"[Upload Inspection] PyMuPDF inspection check warning: {pe}")

    # 2. Same bytes, same user: reuse the existing record rather than making a
    #    second one (Idempotency) — but only skip the parse if that record
    #    actually produced a ledger.
    #
    #    The version of this check that only asked "have I seen these bytes?"
    #    turned a one-off fault into a permanent one. A parse that died midway
    #    left its row in PROCESSING; PROCESSING counted as already-ingested; so
    #    every re-upload of that statement returned the dead file id, queued
    #    nothing, and reported 202. Re-uploading is the one recovery action a
    #    user has, and it had become a no-op — the Transactions tab, dashboard
    #    and reports stayed empty with no error anywhere to explain it.
    #
    #    Idempotency should mean "you already have this data", not "I already
    #    have this filename on disk". So the question asked here is whether the
    #    ledger has rows for this file. If it does, nothing to do. If it does
    #    not, re-queue it — re-parsing is safe, because the ledger write is
    #    keyed by statement and replaces that statement's rows rather than
    #    appending to them.
    existing_file = db.query(UploadedFile).filter(
        UploadedFile.user_id == current_user.id,
        UploadedFile.file_sha256 == file_sha256,
    ).order_by(UploadedFile.uploaded_at.desc()).first()

    if existing_file:
        already_ingested = (
            existing_file.status == "COMPLETED"
            and _has_ledger_rows(db, existing_file.id)
        )

        # The stored copy can be gone — cleared cache, moved data directory, a
        # failed save. The freshly written copy is the same bytes, so point the
        # record at it instead of re-queueing a parse of a path that no longer
        # resolves.
        if not os.path.exists(existing_file.file_path):
            existing_file.file_path = target_path
            db.commit()

        if already_ingested and not clean_pwd:
            logger.info(
                f"[Duplicate Upload] User '{current_user.id}' re-uploaded file "
                f"'{file.filename}' (SHA256: {file_sha256[:10]}...). Already ingested; "
                f"returning existing File ID: {existing_file.id}"
            )
        else:
            reason = "password supplied" if (clean_pwd and already_ingested) else (
                f"previous attempt left status={existing_file.status} with no "
                f"transactions in the ledger"
            )
            logger.info(
                f"[Duplicate Upload] User '{current_user.id}' re-uploaded file "
                f"'{file.filename}' (SHA256: {file_sha256[:10]}...) — re-queueing parse "
                f"for File ID {existing_file.id} ({reason})."
            )
            existing_file.status = "QUEUED"
            db.commit()
            db.refresh(existing_file)
            background_tasks.add_task(
                process_file_parsing_task,
                existing_file.id,
                existing_file.file_path,
                current_user.id,
                account_id,
                pdf_password=clean_pwd,
            )

        return UploadedFileResponse(
            file_id=existing_file.id,
            user_id=existing_file.user_id,
            filename=existing_file.filename,
            file_size=existing_file.file_size,
            mime_type=existing_file.mime_type,
            status=existing_file.status,
            uploaded_at=existing_file.uploaded_at
        )

    # 3. Store metadata record in PostgreSQL (Status: QUEUED)
    db_file = UploadedFile(
        user_id=current_user.id,
        filename=file.filename or stored_filename,
        file_path=target_path,
        file_size=file_size,
        mime_type=file.content_type,
        file_sha256=file_sha256,
        status="QUEUED"
    )
    db.add(db_file)
    db.commit()
    db.refresh(db_file)

    logger.info(f"[File Upload] User '{current_user.id}' uploaded file '{db_file.filename}' (Size: {file_size} bytes, SHA256: {file_sha256[:10]}..., File ID: {db_file.id})")

    # 4. Queue for parsing asynchronously with pdf_password
    background_tasks.add_task(process_file_parsing_task, db_file.id, target_path, current_user.id, account_id, pdf_password=clean_pwd)

    return UploadedFileResponse(
        file_id=db_file.id,
        user_id=db_file.user_id,
        filename=db_file.filename,
        file_size=db_file.file_size,
        mime_type=db_file.mime_type,
        status=db_file.status,
        uploaded_at=db_file.uploaded_at
    )


@router.get("/{file_id}", response_model=UploadedFileResponse)
def get_file_status(
    file_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    db_file = db.query(UploadedFile).filter(
        UploadedFile.id == file_id,
        UploadedFile.user_id == current_user.id
    ).first()

    if not db_file:
        raise HTTPException(status_code=404, detail="Uploaded file not found")

    return UploadedFileResponse(
        file_id=db_file.id,
        user_id=db_file.user_id,
        filename=db_file.filename,
        file_size=db_file.file_size,
        mime_type=db_file.mime_type,
        status=db_file.status,
        uploaded_at=db_file.uploaded_at
    )

@router.get("/", response_model=List[UploadedFileResponse])
def list_user_files(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    files = db.query(UploadedFile).filter(UploadedFile.user_id == current_user.id).all()
    return [
        UploadedFileResponse(
            file_id=f.id,
            user_id=f.user_id,
            filename=f.filename,
            file_size=f.file_size,
            mime_type=f.mime_type,
            status=f.status,
            uploaded_at=f.uploaded_at
        ) for f in files
    ]
