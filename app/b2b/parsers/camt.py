"""CAMT.053 (ISO 20022 bank-to-customer statement), parsed defensively.

Three decisions worth stating up front.

**Elements are matched by local name.** A camt.053 document declares a
versioned namespace — `urn:iso:std:iso:20022:tech:xsd:camt.053.001.02`,
`...001.08`, and so on — and banks are spread across at least five of them. A
parser that hardcodes one namespace works for one bank. Stripping the namespace
and matching `Ntry`, `Amt`, `CdtDbtInd` by local name works for every version,
because ISO renumbers the schema far more often than it renames an element.

**One transaction per `Ntry`, not per `TxDtls`.** An entry is what the bank
booked and what appears on the customer's statement line; `TxDtls` under it are
the individual payments inside a batch, and their amounts frequently do not sum
to the entry amount (charges, FX). Emitting one row per `TxDtls` would inflate
both the transaction count and the totals against the account's own booked
figures. The exception, which is handled: when an entry holds several `TxDtls`
that *each* carry their own `Amt`, the detail is genuinely per-payment and the
entry is expanded, since the sum is then reconcilable.

**`defusedxml`, not `xml.etree`.** The XML here is supplied by whoever calls
the API. `xml.etree.ElementTree` resolves internal entities, which is a
billion-laughs amplification and, with an external system entity, a file
disclosure — `<!ENTITY xxe SYSTEM "file:///etc/passwd">` ends up in a narration
field of the response. `defusedxml` refuses the document instead, and this
parser turns that refusal into a clean 422 rather than a stack trace.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError as DefusedParseError
from defusedxml.ElementTree import parse as defused_parse

from app.b2b.canonical import CREDIT, DEBIT, CanonicalTxn, to_minor
from app.b2b.errors import ApiError, FILE_CORRUPT, NO_TRANSACTIONS_FOUND
from app.b2b.metrics import W_CONTINUITY_UNVERIFIABLE, W_MULTI_CURRENCY, W_ROWS_REJECTED
from app.b2b.parsers.base import (
    ParseOutput,
    mask_account,
    observed_period,
    require_readable_file,
    set_balance,
)

logger = logging.getLogger(__name__)

#: `Tp/CdOrPrtry/Cd` values that identify the statement's bookends.
#: OPBD is the opening booked balance; PRCD (previously closed booked) is what
#: several banks send instead and means the same thing for our purposes.
OPENING_CODES = {"OPBD", "PRCD", "OPAV"}
CLOSING_CODES = {"CLBD", "CLAV"}


def local(tag: str) -> str:
    """`{urn:...camt.053.001.08}Ntry` -> `Ntry`."""
    return tag.rsplit("}", 1)[-1]


def child(element, name: str):
    """First direct child with this local name."""
    for candidate in element:
        if local(candidate.tag) == name:
            return candidate
    return None


def descend(element, *names):
    """Follow a chain of direct children; None if any link is missing."""
    node = element
    for name in names:
        if node is None:
            return None
        node = child(node, name)
    return node


def find_all(element, name: str) -> List[Any]:
    """Every descendant with this local name, at any depth."""
    return [node for node in element.iter() if local(node.tag) == name]


def text_of(element) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def first_text(element, *names) -> str:
    return text_of(descend(element, *names))


def _parse_date(value: str) -> Optional[datetime.date]:
    """ISO date or dateTime -> date. CAMT uses `Dt` for one and `DtTm` for the other."""
    if not value:
        return None
    candidate = value.strip()
    try:
        return datetime.date.fromisoformat(candidate[:10])
    except ValueError:
        return None


def _entry_date(entry) -> Optional[datetime.date]:
    """Booking date preferred, value date as the fallback.

    Booking is when the bank recorded the movement, which is the date printed
    on the statement line and the one a reconciliation will be keyed on. Value
    date is when interest starts and can differ by days.
    """
    for parent in ("BookgDt", "ValDt"):
        node = child(entry, parent)
        if node is None:
            continue
        for field_name in ("Dt", "DtTm"):
            parsed = _parse_date(first_text(node, field_name))
            if parsed:
                return parsed
    return None


def _entry_description(entry) -> str:
    """`AddtlNtryInf` first, then the remittance text inside the detail."""
    additional = first_text(entry, "AddtlNtryInf")
    if additional:
        return additional

    parts: List[str] = []
    details = child(entry, "NtryDtls")
    if details is not None:
        for txdtl in [n for n in details if local(n.tag) == "TxDtls"]:
            ustrd = [text_of(n) for n in find_all(txdtl, "Ustrd") if text_of(n)]
            if ustrd:
                parts.extend(ustrd)
                continue
            # No remittance text: the counterparty name is the next best thing
            # a human would read on the line.
            for party in ("Cdtr", "Dbtr"):
                name = first_text(txdtl, "RltdPties", party, "Nm")
                if name:
                    parts.append(name)
                    break
    return " ".join(parts).strip()


def _txdtl_description(txdtl) -> str:
    ustrd = [text_of(n) for n in find_all(txdtl, "Ustrd") if text_of(n)]
    if ustrd:
        return " ".join(ustrd)
    for party in ("Cdtr", "Dbtr"):
        name = first_text(txdtl, "RltdPties", party, "Nm")
        if name:
            return name
    return ""


def _reference(node) -> Optional[str]:
    """`NtryRef` is the bank's statement reference; `AcctSvcrRef` its servicing id."""
    for name in ("NtryRef", "AcctSvcrRef", "EndToEndId", "TxId", "InstrId", "MsgId"):
        value = ""
        direct = child(node, name)
        if direct is not None:
            value = text_of(direct)
        if not value:
            refs = descend(node, "Refs")
            if refs is not None:
                value = first_text(refs, name)
        if value:
            return value
    return None


def _amount(node) -> Tuple[Optional[float], Optional[str]]:
    """`<Amt Ccy="EUR">1234.56</Amt>` -> (1234.56, "EUR")."""
    amt = child(node, "Amt")
    if amt is None:
        return None, None
    raw = text_of(amt).replace(",", "")
    ccy = (amt.get("Ccy") or "").strip().upper() or None
    try:
        return float(raw), ccy
    except ValueError:
        return None, ccy


def _direction(node) -> Optional[str]:
    """CAMT states direction explicitly; there is nothing to infer here."""
    value = first_text(node, "CdtDbtInd").upper()
    if value == "DBIT":
        return DEBIT
    if value == "CRDT":
        return CREDIT
    return None


def _statement_balances(stmt, meta: Dict[str, Any]) -> None:
    """OPBD/CLBD from `Stmt/Bal`, with the debit indicator applied as a sign.

    A `Bal` marked DBIT is an overdrawn account. Recording it as a positive
    number would turn a -12,000 balance into +12,000 in every downstream
    figure, so the indicator is applied rather than dropped.
    """
    for bal in [n for n in stmt if local(n.tag) == "Bal"]:
        code = first_text(bal, "Tp", "CdOrPrtry", "Cd").upper()
        value, _ccy = _amount(bal)
        if value is None or not code:
            continue
        if _direction(bal) == DEBIT:
            value = -value
        if code in OPENING_CODES and "opening_balance" not in meta:
            set_balance(meta, "opening_balance", value, basis="reported")
        elif code in CLOSING_CODES and "closing_balance" not in meta:
            set_balance(meta, "closing_balance", value, basis="reported")


def _statement_period(stmt, meta: Dict[str, Any]) -> None:
    frto = child(stmt, "FrToDt")
    if frto is None:
        return
    start = _parse_date(first_text(frto, "FrDtTm") or first_text(frto, "FrDt"))
    end = _parse_date(first_text(frto, "ToDtTm") or first_text(frto, "ToDt"))
    if start and end:
        meta["period_start"] = start.isoformat()
        meta["period_end"] = end.isoformat()
        meta["period_source"] = "declared"


def _account(stmt, meta: Dict[str, Any]) -> None:
    acct = child(stmt, "Acct")
    if acct is None:
        return
    identifier = (first_text(acct, "Id", "IBAN")
                  or first_text(descend(acct, "Id", "Othr") or acct, "Id"))
    if identifier:
        meta["account_number_masked"] = mask_account(identifier)
    ccy = first_text(acct, "Ccy")
    if ccy:
        meta["account_currency"] = ccy.upper()


def parse(path: str, *, password: Optional[str] = None,
          currency: str = "INR") -> ParseOutput:
    """Parse a CAMT.053 XML statement. `password` is accepted and ignored."""
    require_readable_file(path)

    try:
        # forbid_dtd closes the door on the entity-expansion families before an
        # entity is even declared; forbid_entities and forbid_external are on by
        # default and are named here so the intent survives a defusedxml upgrade.
        tree = defused_parse(path, forbid_dtd=True, forbid_entities=True,
                             forbid_external=True)
    except DefusedXmlException as exc:
        # A DTD or entity declaration in a customer-supplied statement has no
        # legitimate use and is refused by name, not silently stripped.
        logger.warning("[b2b.camt] rejected unsafe XML construct: %s", type(exc).__name__)
        raise ApiError(
            FILE_CORRUPT,
            "the XML document declares a DTD or entities, which are not accepted; "
            "resend a plain CAMT.053 document with no DOCTYPE",
        )
    except DefusedParseError as exc:
        raise ApiError(FILE_CORRUPT, f"the XML document is not well-formed ({exc})")
    except OSError as exc:
        raise ApiError(FILE_CORRUPT, f"the XML document could not be read ({exc})")

    root = tree.getroot()
    statements = find_all(root, "Stmt")
    if not statements:
        raise ApiError(
            FILE_CORRUPT,
            "no <Stmt> element found; this does not look like a CAMT.053 "
            "bank-to-customer statement",
        )

    meta: Dict[str, Any] = {}
    transactions: List[CanonicalTxn] = []
    currencies: List[str] = []
    skipped = 0
    row_index = 0

    for stmt in statements:
        _account(stmt, meta)
        _statement_period(stmt, meta)
        _statement_balances(stmt, meta)

        for entry in [n for n in stmt if local(n.tag) == "Ntry"]:
            entry_date = _entry_date(entry)
            entry_direction = _direction(entry)
            entry_amount, entry_ccy = _amount(entry)
            if entry_date is None or entry_direction is None or entry_amount is None:
                skipped += 1
                continue

            value_date = _parse_date(first_text(child(entry, "ValDt") or entry, "Dt"))
            details = child(entry, "NtryDtls")
            txdtls = ([n for n in details if local(n.tag) == "TxDtls"]
                      if details is not None else [])
            priced_txdtls = [n for n in txdtls if _amount(n)[0] is not None]

            # The batch case: several detail records that each price themselves.
            # Only then is expanding the entry reconcilable against its own
            # booked amount; otherwise one row per Ntry, as documented above.
            if len(priced_txdtls) > 1:
                units = [(n, *_amount(n), _direction(n)) for n in priced_txdtls]
            else:
                units = [(entry, entry_amount, entry_ccy, entry_direction)]

            for node, amount, ccy, direction in units:
                direction = direction or entry_direction
                if amount is None:
                    skipped += 1
                    continue
                paise = to_minor(abs(amount))
                if paise is None:
                    skipped += 1
                    continue
                row_currency = (ccy or entry_ccy
                                or meta.get("account_currency") or currency)
                description = (_txdtl_description(node) if node is not entry
                               else _entry_description(entry))
                reference = _reference(node) or _reference(entry)

                transactions.append(CanonicalTxn(
                    txn_date=entry_date,
                    direction=direction,
                    debit_paise=paise if direction == DEBIT else None,
                    credit_paise=paise if direction == CREDIT else None,
                    # CAMT books a statement-level opening and closing balance
                    # but no running balance per entry, so this stays None.
                    balance_paise=None,
                    narration_raw=description,
                    narration_clean=description,
                    row_index=row_index,
                    currency=row_currency,
                    value_date=value_date,
                    reference_number=reference,
                    parse_confidence=1.0,
                    source_format="xml_camt",
                    transaction_method=(first_text(entry, "BkTxCd", "Prtry", "Cd")
                                        or None),
                ))
                currencies.append(row_currency)
                row_index += 1

    if not transactions:
        raise ApiError(NO_TRANSACTIONS_FOUND,
                       "the CAMT document contained no usable <Ntry> entries")

    output = ParseOutput(transactions=transactions)

    distinct = sorted(set(currencies))
    meta["currency"] = distinct[0] if len(distinct) == 1 else currency
    if len(distinct) > 1:
        meta["currencies"] = distinct
        output.warn(W_MULTI_CURRENCY,
                    "this statement mixes " + ", ".join(distinct) +
                    "; totals across different currencies are not comparable")

    if "period_start" not in meta:
        meta.update(observed_period(transactions))
    output.statement_meta = meta

    output.rows_checked_for_continuity = 0
    output.continuity_pass_rate = None
    output.continuity_passed = None
    output.warn(W_CONTINUITY_UNVERIFIABLE,
                "CAMT.053 carries opening and closing balances but no running "
                "balance per entry, so row-by-row continuity cannot be verified")

    if skipped:
        output.warn(W_ROWS_REJECTED,
                    f"{skipped} <Ntry> element(s) were skipped for a missing date, "
                    f"amount or CdtDbtInd")
    return output
