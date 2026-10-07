"""One shape for a transaction row on every Review Queue tab.

Manual Review shows each row as: who it is with (`counterparty`), a small
badge (`group_kind` / `group_channel`), the narration on demand, the date, and
the amount in a Paid out or Received column. Cross-Source Duplicates and BRS
Match Candidates show the same rows, so they are built here, by the same
`group_for` the Manual Review tab uses — two screens deriving the name two ways
is how they drift apart.
"""
from typing import Any, Dict, Optional

from app.categorization.counterparty import group_for


def _value(v):
    return v.value if hasattr(v, "value") else (str(v) if v is not None else None)


def _label(narration: str, stored: Optional[str] = None) -> Dict[str, Optional[str]]:
    try:
        grp = group_for(narration or "")
    except Exception:          # noqa: BLE001 - never break the queue over a label
        grp = None
    return {
        "counterparty": stored or (grp.display if grp else None),
        "group_kind": grp.kind if grp else None,
        "group_channel": grp.channel if grp else None,
    }


def bank_row(tx) -> Optional[Dict[str, Any]]:
    """A bank transaction, in the Manual Review row shape."""
    if tx is None:
        return None
    narration = tx.narration_clean or tx.narration_raw or ""
    paise = tx.debit_paise or tx.credit_paise or 0
    return {
        "side": "bank",
        "transaction_id": tx.id,
        "account_id": tx.account_id,
        "txn_date": tx.txn_date,
        "narration": narration,
        "amount": paise / 100.0,
        "direction": _value(tx.direction) or "debit",
        "source_type": _value(tx.source_type),
        "reference_no": tx.reference_no,
        **_label(narration, tx.counterparty),
    }


def book_row(entry) -> Optional[Dict[str, Any]]:
    """A ledger (books) entry, in the same shape.

    Money out of the books is a payment, so it sits in Paid out exactly as the
    bank debit it should match does.
    """
    if entry is None:
        return None
    narration = entry.narration or ""
    out = entry.money_out_paise or 0
    inn = entry.money_in_paise or 0
    label = _label(" ".join(x for x in (entry.party_name, narration) if x), entry.party_name)
    if not label["counterparty"]:
        label["counterparty"] = entry.ledger_name
    return {
        "side": "books",
        "transaction_id": entry.id,
        "account_id": entry.account_id,
        "txn_date": entry.entry_date,
        "narration": narration or entry.party_name or entry.ledger_name or "",
        "amount": (out or inn) / 100.0,
        "direction": "debit" if out else "credit",
        "source_type": "ledger",
        "reference_no": entry.instrument_no or entry.voucher_no,
        "voucher_type": entry.voucher_type,
        **label,
    }
