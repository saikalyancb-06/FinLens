import os
import uuid
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from app.config import settings
from app.database.session import get_db
from app.models.user import User
from app.models.refresh_token import RefreshToken
from app.api.schemas import (
    UserRegister,
    UserLogin,
    UserResponse,
    TokenResponse,
    RefreshTokenRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest
)
from app.utils.security import (
    hash_password,
    verify_password,
    hash_token,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    REFRESH_TOKEN_EXPIRE_DAYS
)

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(payload: UserRegister, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.email == payload.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user

@router.post("/login", response_model=TokenResponse)
def login(payload: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is inactive"
        )


    access_token = create_access_token(data={"sub": str(user.id)})
    refresh_token = create_refresh_token(data={"sub": str(user.id)})

    # Persist refresh token hash in DB
    ref_hash = hash_token(refresh_token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    
    db_refresh = RefreshToken(
        user_id=user.id,
        token_hash=ref_hash,
        expires_at=expires_at.replace(tzinfo=None)
    )
    db.add(db_refresh)
    db.commit()

    return TokenResponse(access_token=access_token, refresh_token=refresh_token)

@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshTokenRequest, db: Session = Depends(get_db)):
    token_data = decode_token(payload.refresh_token)
    if token_data.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token type")

    user_id_str = token_data.get("sub")
    ref_hash = hash_token(payload.refresh_token)

    db_token = db.query(RefreshToken).filter(
        RefreshToken.token_hash == ref_hash,
        RefreshToken.revoked == False
    ).first()

    if not db_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token revoked or invalid")

    try:
        user_uuid = uuid.UUID(user_id_str)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid user ID format")

    user = db.query(User).filter(User.id == user_uuid).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User inactive or not found")

    # Revoke old refresh token & issue new pair
    db_token.revoked = True
    
    new_access_token = create_access_token(data={"sub": str(user.id)})
    new_refresh_token = create_refresh_token(data={"sub": str(user.id)})

    new_ref_hash = hash_token(new_refresh_token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

    new_db_refresh = RefreshToken(
        user_id=user.id,
        token_hash=new_ref_hash,
        expires_at=expires_at.replace(tzinfo=None)
    )
    db.add(new_db_refresh)
    db.commit()

    return TokenResponse(access_token=new_access_token, refresh_token=new_refresh_token)

@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user:
        # Return success even if email not found to prevent user enumeration
        return {"message": "If the email is registered, a password reset link has been sent."}

    raw_reset_token = secrets.token_urlsafe(32)
    user.reset_token_hash = hash_token(raw_reset_token)
    user.reset_token_expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    db.commit()

    # TODO: dispatch the reset email carrying raw_reset_token.
    generic_response = {"message": "If the email is registered, a password reset link has been sent."}

    # The raw token is echoed back only outside production, where no mail transport
    # is wired up yet. Returning it in production would let anyone who knows a
    # registered address take over that account without inbox access.
    if settings.ENVIRONMENT == "production":
        return generic_response

    return {
        "message": "Password reset token generated successfully.",
        "reset_token": raw_reset_token  # Non-production only: no mail transport configured
    }

@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    hashed_req_token = hash_token(payload.reset_token)
    user = db.query(User).filter(User.reset_token_hash == hashed_req_token).first()

    if not user or not user.reset_token_expires:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid reset token")

    # reset_token_expires is stored naive-UTC (see forgot_password), so compare
    # against a naive-UTC now rather than mixing aware/naive datetimes.
    if user.reset_token_expires < datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reset token has expired")

    user.hashed_password = hash_password(payload.new_password)
    user.reset_token_hash = None
    user.reset_token_expires = None

    # Cut every session that was established with the OLD password. Without this
    # a reset changed nothing for whoever already held a refresh token: they kept
    # minting fresh access tokens indefinitely, which is precisely the case a
    # reset exists to close ("someone else is in my account").
    revoked = db.query(RefreshToken).filter(
        RefreshToken.user_id == user.id,
        RefreshToken.revoked == False  # noqa: E712 - SQL boolean column, not Python
    ).update({RefreshToken.revoked: True}, synchronize_session=False)

    db.commit()

    return {"message": "Password reset successfully.", "sessions_revoked": revoked}

@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user
