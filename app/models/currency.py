"""Currency reference data and the INR rate history used to re-denominate.

Two tables rather than one, deliberately:

`currencies` holds facts that do not change - what the code means, how it is
written, and how many minor units it has. `currency_rates` holds the one fact
that changes daily. Folding them together would force the name, symbol and
decimal count to be restated on every rate row, and the first typo in that
duplication becomes a currency that renders two different ways on two screens.

DECIMALS IS NOT COSMETIC. JPY has no minor unit at all, so 1000 JPY is stored
as 1000, not 100000; KWD has three, so 1.234 KWD is 1234. Assuming two
everywhere - the assumption baked into every `*_paise` column in this codebase -
would overstate a yen amount by 100x and understate a dinar by 10x. The
conversion helpers read this column instead of guessing.
"""

import uuid
from datetime import date as date_type

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, Numeric, String,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database.session import Base


class Currency(Base):
    """A currency the application can display amounts in.

    Deliberately NOT scoped to a user. An exchange rate is reference data about
    the world, not about an account: two users of the same deployment looking at
    the same date should see the same rate, and a per-user table would let one
    person's typo and another's correct entry coexist with no way to tell which
    a report used. The trade-off is that any user who can edit rates edits them
    for everyone, which is the right default for a single-organisation install
    and would need revisiting before this is offered to unrelated tenants.
    """

    __tablename__ = "currencies"

    code = Column(String(3), primary_key=True)          # ISO 4217, e.g. "USD"
    name = Column(String(64), nullable=False)
    symbol = Column(String(8), nullable=False, server_default="")
    decimals = Column(Integer, nullable=False, server_default="2")
    is_active = Column(Boolean, nullable=False, server_default="true")
    display_order = Column(Integer, nullable=False, server_default="100")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    rates = relationship("CurrencyRate", back_populates="currency",
                         cascade="all, delete-orphan", passive_deletes=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Currency {self.code}>"


class CurrencyRate(Base):
    """How many INR one unit of `code` was worth, on a given date.

    Stored as INR-per-unit rather than units-per-INR because that is the
    direction bank advices quote ("USD 1 = INR 88.71") and because it keeps the
    number greater than one for every currency here, which makes a wrong entry
    visible at a glance.

    Numeric(20, 8), never float: JPY sits near 0.58 and KWD near 290, so a
    binary float's rounding error is not uniform across the table, and a
    re-denominated total that does not tie out to the paise is a support ticket.
    """

    __tablename__ = "currency_rates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code = Column(String(3), ForeignKey("currencies.code", ondelete="CASCADE"),
                  nullable=False, index=True)
    as_of = Column(Date, nullable=False, index=True)
    inr_per_unit = Column(Numeric(20, 8), nullable=False)

    # "seed" for the shipped indicative table, "manual" once a user edits it.
    # Kept so a rate nobody has vouched for is distinguishable from one that
    # someone typed in from a bank advice.
    source = Column(String(32), nullable=False, server_default="seed")
    note = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    currency = relationship("Currency", back_populates="rates")

    __table_args__ = (
        UniqueConstraint("code", "as_of", name="uq_currency_rate_code_asof"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CurrencyRate {self.code} {self.as_of} {self.inr_per_unit}>"
