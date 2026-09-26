import uuid
import datetime
import logging
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status, Body, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.database.session import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.aa.models import AaConsent, AaDataSession
import os
from fastapi.responses import RedirectResponse
from app.aa.provider import RebitAAProvider, BaseAAProvider
from app.aa.service import AccountAggregatorService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/aa", tags=["Account Aggregator"])

provider: BaseAAProvider = RebitAAProvider()
aa_service = AccountAggregatorService()

# --- Pydantic Schemas ---

class InitiateConsentRequest(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    purpose_code: str = "101"
    fi_type: str = "DEPOSIT"


class InitiateConsentResponse(BaseModel):
    consent_handle: str
    approval_url: str
    status: str
    message: str


class FetchFiDataRequest(BaseModel):
    consent_handle: str
    date_from: Optional[str] = None
    date_to: Optional[str] = None


class FetchFiDataResponse(BaseModel):
    session_id: str
    status: str
    fetched_count: int
    message: str
    records: List[Dict[str, Any]] = []


# --- Endpoints ---

@router.post("/consent/initiate", response_model=InitiateConsentResponse, summary="Initiate AA Consent Request")
def initiate_consent(
    req: InitiateConsentRequest = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Creates an Account Aggregator consent request and returns the approval URL."""
    try:
        consent_res = provider.create_consent(
            user_id=str(current_user.id),
            date_from=req.date_from,
            date_to=req.date_to,
            purpose_code=req.purpose_code
        )

        handle = consent_res["consent_handle"]
        approval_url = consent_res["approval_url"]

        df = datetime.datetime.strptime(req.date_from, "%Y-%m-%d") if req.date_from else datetime.datetime.utcnow() - datetime.timedelta(days=90)
        dt = datetime.datetime.strptime(req.date_to, "%Y-%m-%d") if req.date_to else datetime.datetime.utcnow()

        db_consent = AaConsent(
            user_id=current_user.id,
            consent_handle=handle,
            status="PENDING",
            purpose_code=req.purpose_code,
            fi_type=req.fi_type,
            date_from=df,
            date_to=dt,
            approval_url=approval_url
        )
        db.add(db_consent)
        db.commit()
        db.refresh(db_consent)

        return InitiateConsentResponse(
            consent_handle=handle,
            approval_url=approval_url,
            status="PENDING",
            message="Consent handle created successfully. Redirect user to approval_url."
        )
    except Exception as e:
        logger.error(f"[AA Routes] Error initiating consent for user '{current_user.id}': {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to initiate AA consent: {str(e)}")


@router.get("/consent/status/{consent_handle}", summary="Check AA Consent Status")
def check_consent_status(
    consent_handle: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Polls or updates consent status for a given consent handle."""
    consent = db.query(AaConsent).filter(
        AaConsent.consent_handle == consent_handle,
        AaConsent.user_id == current_user.id
    ).first()

    if not consent:
        raise HTTPException(status_code=404, detail="Consent handle not found.")

    res = provider.get_consent_status(consent_handle)
    new_status = res.get("status", consent.status)
    consent_id = res.get("consent_id", consent.consent_id or f"CONSENT-ID-{consent_handle[-6:]}")

    consent.status = new_status
    consent.consent_id = consent_id
    db.commit()

    return {
        "consent_handle": consent_handle,
        "consent_id": consent_id,
        "status": new_status,
        "updated_at": consent.updated_at.isoformat() if consent.updated_at else None
    }


@router.post("/consent/webhook", summary="AA Notification Webhook Endpoint")
async def consent_webhook(
    req: Request,
    payload: Optional[Dict[str, Any]] = Body(None),
    db: Session = Depends(get_db)
):
    """Receives async notifications from Account Aggregator (Consent status / FI data readiness).
    Authenticates requests via HMAC SHA-256 signature or shared secret header before database mutation.
    """
    import hmac
    import hashlib

    if payload is None:
        try:
            payload = await req.json()
        except Exception:
            payload = {}

    logger.info(f"[AA Webhook] Received payload: {payload}")

    from app.config import settings

    is_production = settings.ENVIRONMENT == "production"
    sandbox_mode = os.getenv("AA_SANDBOX_MODE", "true").lower() in ("true", "1", "yes")

    # Production must supply a real shared secret; the well-known sandbox value is
    # only a stand-in for local demo runs where no Setu credentials exist.
    webhook_secret = os.getenv("SETU_WEBHOOK_SECRET") or os.getenv("AA_WEBHOOK_SECRET")
    if not webhook_secret and not is_production:
        webhook_secret = "sandbox_webhook_secret"

    sig_header = req.headers.get("x-setu-signature") or req.headers.get("x-aa-signature") or req.headers.get("x-webhook-signature")

    # 1. Signature Verification (HMAC SHA-256 over the exact bytes received)
    verified = False

    if sig_header and webhook_secret:
        try:
            # Sign the raw request body, not a re-serialised copy: key ordering and
            # separator choices in json.dumps() would otherwise change the digest.
            raw_body = await req.body()
            expected_sig = hmac.new(webhook_secret.encode('utf-8'), raw_body, hashlib.sha256).hexdigest()
            verified = hmac.compare_digest(sig_header.strip().lower(), expected_sig.lower())
        except Exception as e:
            logger.warning(f"[AA Webhook] HMAC signature verification failed: {e}")

    # Production always requires a real signature. The sandbox bypass below exists
    # only so the AA demo can run without Setu-issued credentials, and it is
    # deliberately unreachable once ENVIRONMENT=production.
    #
    # It also requires that NO signature was offered: a caller who presents a
    # signature is claiming authenticity, so a failed verification is a forgery
    # attempt and must be rejected rather than waved through as "demo traffic".
    if not verified and not sig_header and not is_production and sandbox_mode:
        handle_check = payload.get("consentHandle") or payload.get("ConsentHandle")
        if handle_check:
            logger.warning(
                "[AA Webhook] SANDBOX BYPASS: accepting unsigned webhook for consent handle "
                f"'{handle_check}'. This path is disabled in production."
            )
            verified = True

    if not verified:
        if is_production and not webhook_secret:
            logger.error(
                "[AA Webhook] Rejected: no SETU_WEBHOOK_SECRET/AA_WEBHOOK_SECRET configured in production."
            )
        else:
            logger.error("[AA Webhook] Unauthorized webhook request: Invalid or missing signature header.")
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing webhook signature")

    handle = payload.get("consentHandle") or payload.get("ConsentHandle")
    status_str = payload.get("consentStatus") or payload.get("status") or "ACTIVE"

    if handle:
        consent = db.query(AaConsent).filter(AaConsent.consent_handle == handle).first()
        if consent:
            # Replay Protection: idempotent update
            if consent.status != status_str:
                consent.status = status_str
                db.commit()
                logger.info(f"[AA Webhook] Consent handle '{handle}' status updated to '{status_str}'.")
            else:
                logger.info(f"[AA Webhook] Idempotent event: Consent handle '{handle}' already in status '{status_str}'.")

    return {"status": "SUCCESS", "message": "Webhook processed"}

@router.get("/consent/approve/{consent_handle}", summary="Redirect to Setu AA approval page")
def approve_consent(
    consent_handle: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    consent = db.query(AaConsent).filter(
        AaConsent.consent_handle == consent_handle,
        AaConsent.user_id == current_user.id
    ).first()
    if not consent:
        raise HTTPException(status_code=404, detail="Consent not found")

    # Ensure the approval URL contains the sandbox API key if required
    approval_url = consent.approval_url
    api_key = os.getenv("SETU_SANDBOX_API_KEY")
    if api_key and "api_key=" not in approval_url:
        separator = "&" if "?" in approval_url else "?"
        approval_url = f"{approval_url}{separator}api_key={api_key}"

    return RedirectResponse(url=approval_url)


@router.post("/fetch", response_model=FetchFiDataResponse, summary="Trigger FI Data Fetch & Categorize")
def fetch_and_ingest_fi_data(
    req: FetchFiDataRequest = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Fetches FI financial data via AA, decrypts payload, categorizes via Rule+ML engine, and stores in database."""
    consent = db.query(AaConsent).filter(
        AaConsent.consent_handle == req.consent_handle,
        AaConsent.user_id == current_user.id
    ).first()

    if not consent:
        raise HTTPException(status_code=404, detail="Consent record not found.")

    # Update consent status to ACTIVE for sandbox
    consent.status = "ACTIVE"
    consent_id = consent.consent_id or f"CONSENT-ID-{consent.consent_handle[-6:]}"
    consent.consent_id = consent_id
    db.commit()

    try:
        # 1. Request FI Session
        fi_req_res = provider.create_fi_data_request(
            consent_id=consent_id,
            date_from=req.date_from or (consent.date_from.strftime("%Y-%m-%d") if consent.date_from else None),
            date_to=req.date_to or (consent.date_to.strftime("%Y-%m-%d") if consent.date_to else None)
        )

        session_id = fi_req_res["session_id"]

        # Save Session DB record
        db_session = AaDataSession(
            user_id=current_user.id,
            consent_id=consent_id,
            session_id=session_id,
            status="PENDING",
            fetched_count=0
        )
        db.add(db_session)
        db.commit()

        # 2. Fetch FI Data
        fi_payload = provider.fetch_fi_data(session_id)

        # 3. Decrypt FI Payload
        decrypted_payload = provider.decrypt_fi_data(fi_payload)

        # 4. Normalize, Categorize via Rule+ML Engine & Persist into ProcessedTransaction
        stored_records = aa_service.process_and_store_fi_data(
            db=db,
            user_id=str(current_user.id),
            fi_payload=decrypted_payload
        )

        # 5. Update Session status
        db_session.status = "COMPLETED"
        db_session.fetched_count = len(stored_records)
        db.commit()

        record_summaries = [
            {
                "id": str(rec.id),
                "date": rec.date.strftime("%Y-%m-%d") if rec.date else "",
                "description": rec.description,
                "debit": rec.debit,
                "credit": rec.credit,
                "balance": rec.balance,
                "category": rec.final_category,
                "method": rec.prediction_source,
                "source": "account_aggregator"
            }
            for rec in stored_records
        ]

        return FetchFiDataResponse(
            session_id=session_id,
            status="COMPLETED",
            fetched_count=len(stored_records),
            message=f"Successfully fetched, decrypted, categorized and stored {len(stored_records)} Account Aggregator transactions.",
            records=record_summaries
        )
    except Exception as e:
        logger.error(f"[AA Routes] Error fetching/ingesting FI data for user '{current_user.id}': {e}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to fetch FI data: {str(e)}")


@router.get("/consents", summary="List User AA Consents")
def list_consents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Lists all AA consent handles for the current user."""
    consents = db.query(AaConsent).filter(
        AaConsent.user_id == current_user.id
    ).order_by(AaConsent.created_at.desc()).all()

    return [
        {
            "id": str(c.id),
            "consent_handle": c.consent_handle,
            "consent_id": c.consent_id,
            "status": c.status,
            "purpose_code": c.purpose_code,
            "fi_type": c.fi_type,
            "date_from": c.date_from.strftime("%Y-%m-%d") if c.date_from else None,
            "date_to": c.date_to.strftime("%Y-%m-%d") if c.date_to else None,
            "approval_url": c.approval_url,
            "created_at": c.created_at.isoformat() if c.created_at else None
        }
        for c in consents
    ]
