"""Compute the display amounts for one transaction in a chosen currency.

Kept out of the API module because the same rules apply anywhere an amount is
shown, and out of `service.py` because it knows about `Transaction`.

Three rules, in order of priority:

1. If the caller asked for the row's own original currency, show the original
   figure verbatim. Re-deriving USD from the INR the bank booked, at a rate from
   a table, would produce a number that appears on no document - and it would
   differ from the advice by the bank's own margin every time.
2. Otherwise convert from the booked currency through INR.
3. A balance belongs to the account, not the transaction. In "actual" mode it
   stays in the account's own currency and says so, rather than being restated
   in whichever currency this particular line happened to be contracted in.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional

from app.currency.service import BASE_CURRENCY, convert_minor, format_minor

ACTUAL = "actual"

# Which day's rate a conversion should use.
#
#   txn     - the rate that applied on the transaction's own date. This is the
#             accounting answer: a payment made in June was worth what it was
#             worth in June, and restating it later would rewrite history every
#             time the market moved.
#   current - the newest rate on file, applied to every row regardless of date.
#             This answers a different and equally real question: "what is this
#             worth to me today", which is what you want when sizing an exposure
#             or deciding whether to hedge.
#
# Both are legitimate; they are not interchangeable, and a report that silently
# picked one would be wrong for half its readers. So the caller says which, and
# the UI labels which is on screen.
RATE_BASIS_TXN = "txn"
RATE_BASIS_CURRENT = "current"
RATE_BASES = (RATE_BASIS_TXN, RATE_BASIS_CURRENT)


@dataclass
class DisplayBlock:
    currency: str
    symbol: str
    decimals: int
    debit: Optional[float]
    credit: Optional[float]
    amount: Optional[float]
    balance: Optional[float]
    balance_currency: str
    # False when the figure came out of the rate table rather than off the
    # bank's own advice. The UI marks these, because an indicative rate applied
    # to a real amount is still an estimate.
    is_exact: bool
    # Which day's rate produced this figure, so the UI can say so rather than
    # leaving the reader to assume.
    rate_basis: str = RATE_BASIS_TXN


def resolve_target(requested: Optional[str], booked: str,
                   original: Optional[str]) -> str:
    """Which currency this row should actually be shown in."""
    if not requested or requested.lower() == ACTUAL:
        return (original or booked or BASE_CURRENCY).upper()
    return requested.upper()


def build_display(
    *,
    requested: Optional[str],
    booked_currency: str,
    original_currency: Optional[str],
    original_amount_minor: Optional[int],
    debit_minor: Optional[int],
    credit_minor: Optional[int],
    balance_minor: Optional[int],
    direction_is_debit: bool,
    meta: Dict[str, tuple],          # code -> (symbol, decimals)
    rate_of: Dict[str, Optional[Decimal]],   # code -> INR per unit, on the chosen basis
    rate_basis: str = RATE_BASIS_TXN,
) -> Optional[DisplayBlock]:
    booked = (booked_currency or BASE_CURRENCY).upper()
    target = resolve_target(requested, booked, original_currency)

    symbol, decimals = meta.get(target, ("", 2))
    booked_symbol, booked_decimals = meta.get(booked, ("", 2))
    from_rate = rate_of.get(booked)
    to_rate = rate_of.get(target)

    is_actual_mode = (not requested) or requested.lower() == ACTUAL

    # Rule 1: the row's own contracted currency, taken from the advice.
    #
    # Only on the transaction-date basis. On the current-date basis the reader
    # has explicitly asked "what is this worth now", and answering with the
    # figure the bank contracted months ago would ignore the question - so that
    # basis falls through to conversion even for the row's own currency.
    #
    # "Actual" mode always uses the advice regardless of basis: a transaction
    # that happened in dollars happened in dollars, and no rate changes that.
    if (original_currency and original_amount_minor is not None
            and target == original_currency.upper()
            and (is_actual_mode or rate_basis == RATE_BASIS_TXN)):
        amount = format_minor(original_amount_minor, decimals)
        if is_actual_mode:
            # Rule 3: in "actual" mode the balance is left in the account's own
            # currency. A running balance belongs to the account, and restating
            # it in whichever currency this one line was contracted in would put
            # two different currencies in the balance column of one statement.
            balance = format_minor(balance_minor, booked_decimals)
            balance_ccy = booked
        else:
            # The caller asked for one currency across the whole table, so the
            # balance is converted like everything else - even though the amount
            # above came off the advice rather than the rate table.
            balance = format_minor(
                convert_minor(balance_minor, booked, target, booked_decimals,
                              decimals, rate_of.get(booked), rate_of.get(target)),
                decimals,
            )
            balance_ccy = target
        return DisplayBlock(
            currency=target, symbol=symbol, decimals=decimals,
            debit=amount if direction_is_debit else None,
            credit=None if direction_is_debit else amount,
            amount=amount,
            balance=balance, balance_currency=balance_ccy,
            is_exact=True, rate_basis=rate_basis,
        )

    # Rule 2: convert through INR.
    def conv(v: Optional[int]) -> Optional[int]:
        return convert_minor(v, booked, target, booked_decimals, decimals,
                             from_rate, to_rate)

    debit = format_minor(conv(debit_minor), decimals)
    credit = format_minor(conv(credit_minor), decimals)
    balance = format_minor(conv(balance_minor), decimals)
    amount = debit if (debit or 0) else credit

    return DisplayBlock(
        currency=target, symbol=symbol, decimals=decimals,
        debit=debit, credit=credit, amount=amount,
        balance=balance, balance_currency=target,
        is_exact=(target == booked),
        rate_basis=rate_basis,
    )
