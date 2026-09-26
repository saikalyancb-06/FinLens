"""Currency reference data and the rate editor.

The shipped rates are indicative placeholders, not a market feed. Every response
here carries the rate's `source` and `as_of` so a caller can tell a number
somebody vouched for from one that shipped in a seed list, and the UI can say so
rather than presenting a placeholder as fact.
"""

from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.currency.service import BASE_CURRENCY, rate_on, seed_currencies
from app.database.session import get_db
from app.models.currency import Currency, CurrencyRate
from app.models.user import User
from app.utils.security import get_current_user

router = APIRouter(prefix="/currencies", tags=["Currencies"])


class RateOut(BaseModel):
    as_of: Optional[date_type] = None
    inr_per_unit: Optional[float] = None
    source: Optional[str] = None
    note: Optional[str] = None


class CurrencyOut(BaseModel):
    code: str
    name: str
    symbol: str
    decimals: int
    is_active: bool
    display_order: int
    is_base: bool
    latest_rate: RateOut


class RateIn(BaseModel):
    inr_per_unit: float = Field(..., gt=0, description="INR per one unit of this currency")
    as_of: Optional[date_type] = None
    note: Optional[str] = Field(None, max_length=255)

    @field_validator("inr_per_unit")
    @classmethod
    def _sane(cls, v: float) -> float:
        # A rate outside this band is a typo far more often than it is a real
        # currency, and a mistyped rate silently rewrites every converted figure
        # on the screen. Rejecting it is cheaper than explaining it later.
        if not (0.000001 <= v <= 100000):
            raise ValueError("Rate must be between 0.000001 and 100000 INR per unit")
        return v


def _latest_rate(db: Session, code: str) -> RateOut:
    if code == BASE_CURRENCY:
        return RateOut(as_of=None, inr_per_unit=1.0, source="base",
                       note="Ledger base currency")
    row = db.execute(
        select(CurrencyRate).where(CurrencyRate.code == code)
        .order_by(CurrencyRate.as_of.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return RateOut()
    return RateOut(as_of=row.as_of, inr_per_unit=float(row.inr_per_unit),
                   source=row.source, note=row.note)


def _to_out(db: Session, c: Currency) -> CurrencyOut:
    return CurrencyOut(
        code=c.code, name=c.name, symbol=c.symbol, decimals=c.decimals,
        is_active=c.is_active, display_order=c.display_order,
        is_base=(c.code == BASE_CURRENCY), latest_rate=_latest_rate(db, c.code),
    )


@router.get("", response_model=List[CurrencyOut])
@router.get("/", response_model=List[CurrencyOut])
def list_currencies(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    include_inactive: bool = Query(False),
):
    """All display currencies, seeding the table on first call.

    Seeding here rather than only in a setup script means the selector is never
    empty on a fresh database, which is the state a new install is in.
    """
    q = select(Currency)
    if not include_inactive:
        q = q.where(Currency.is_active.is_(True))
    rows = db.execute(q.order_by(Currency.display_order, Currency.code)).scalars().all()

    if not rows:
        seed_currencies(db)
        db.commit()
        rows = db.execute(q.order_by(Currency.display_order, Currency.code)).scalars().all()

    return [_to_out(db, c) for c in rows]


@router.put("/{code}/rate", response_model=CurrencyOut)
def set_rate(
    code: str,
    payload: RateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set (or correct) the rate for a currency on a date.

    Upserts on (code, as_of) so re-entering a date fixes the existing rate
    instead of stacking a second row that the "latest on or before" lookup would
    have to break a tie on.
    """
    code = code.upper()
    if code == BASE_CURRENCY:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            detail=f"{BASE_CURRENCY} is the base currency; its rate is 1 by definition")

    ccy = db.get(Currency, code)
    if ccy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown currency '{code}'")

    as_of = payload.as_of or date_type.today()
    row = db.execute(
        select(CurrencyRate).where(CurrencyRate.code == code, CurrencyRate.as_of == as_of)
    ).scalar_one_or_none()

    try:
        value = Decimal(str(payload.inr_per_unit))
    except InvalidOperation:  # pragma: no cover - guarded by the validator
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Rate is not a number")

    if row is None:
        db.add(CurrencyRate(code=code, as_of=as_of, inr_per_unit=value,
                            source="manual", note=payload.note))
    else:
        row.inr_per_unit = value
        row.source = "manual"
        row.note = payload.note

    db.commit()
    return _to_out(db, db.get(Currency, code))


@router.get("/{code}/rates")
def rate_history(
    code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    limit: int = Query(200, ge=1, le=2000),
):
    """Every rate recorded for a currency, newest first."""
    code = code.upper()
    if db.get(Currency, code) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown currency '{code}'")
    rows = db.execute(
        select(CurrencyRate).where(CurrencyRate.code == code)
        .order_by(CurrencyRate.as_of.desc()).limit(limit)
    ).scalars().all()
    return [
        {"as_of": r.as_of, "inr_per_unit": float(r.inr_per_unit),
         "source": r.source, "note": r.note}
        for r in rows
    ]


@router.delete("/{code}/rates/{as_of}")
def delete_rate(
    code: str,
    as_of: date_type,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove one dated rate, so a mistyped entry can be undone."""
    code = code.upper()
    row = db.execute(
        select(CurrencyRate).where(CurrencyRate.code == code, CurrencyRate.as_of == as_of)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No rate for that date")
    db.delete(row)
    db.commit()
    return {"deleted": True, "code": code, "as_of": as_of}


@router.get("/refresh/status")
def refresh_status(
    current_user: User = Depends(get_current_user),
):
    """What the last automatic refresh did, including what it refused to store."""
    from app.config import settings as app_settings
    from app.currency.refresher import last_report, source_health

    return {
        "enabled": app_settings.FX_REFRESH_ENABLED,
        "interval_minutes": app_settings.FX_REFRESH_MINUTES,
        "max_step_change": app_settings.FX_MAX_STEP_CHANGE,
        "last_run": last_report(),
        # Sources currently being skipped after repeated refusals, so a silent
        # gap in provenance has a visible reason attached to it.
        "source_health": source_health(),
    }


@router.post("/refresh")
def refresh_rates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    overwrite_manual: Optional[str] = Query(
        None,
        description=("Comma-separated currency codes whose manually entered rate "
                     "should be replaced by the fetched one, or 'all'. Omit to "
                     "keep every manual rate and have the differences reported "
                     "under `conflicts` instead."),
    ),
):
    """Fetch rates now instead of waiting for the next poll.

    Returns the same report the background loop produces, including rejections,
    so a scrape that has started returning nonsense is visible from the UI
    rather than only in the server log.

    Manual rates are never replaced by the first call. Where a fetched rate
    differs from one somebody typed in, the pair comes back under `conflicts`
    and the caller decides; calling again with `overwrite_manual` applies that
    decision. The two-step matters because the person who typed the rate may
    have copied it off a bank advice, which beats a market mid-rate for that
    transaction - the application cannot know, so it asks.

    The rate itself is always re-fetched from the source rather than accepted
    from the client, so a row tagged `rbi` was genuinely published by RBI no
    matter which button produced it.
    """
    from app.currency.refresher import refresh_now

    codes: Optional[List[str]] = None
    if overwrite_manual:
        if overwrite_manual.strip().lower() == "all":
            codes = [c.code for c in db.execute(select(Currency)).scalars().all()]
        else:
            codes = [c.strip().upper() for c in overwrite_manual.split(",") if c.strip()]

    return refresh_now(db, overwrite_manual=codes).as_dict()


@router.post("/reseed")
def reseed(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    overwrite_seed_rates: bool = Query(
        False, description="Also reset placeholder rates that nobody has edited"),
):
    """Re-insert any missing currency. Never overwrites a manually entered rate."""
    result = seed_currencies(db, overwrite_rates=overwrite_seed_rates)
    db.commit()
    return result
