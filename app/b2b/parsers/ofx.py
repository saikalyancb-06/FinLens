"""OFX 1.x and 2.x (and Quicken's QFX), with no new dependency.

OFX 1.x is **not XML.** It is SGML with optional closing tags, an unquoted
header block, and no declaration an XML parser will accept:

    OFXHEADER:100
    DATA:OFXSGML
    ...
    <STMTTRN>
    <TRNTYPE>DEBIT
    <DTPOSTED>20240401120000.000[-5:EST]
    <TRNAMT>-1250.75

Feeding that to ElementTree produces a parse error, and the usual workaround —
"insert the missing closing tags first" — is a small SGML implementation in
disguise. Scanning for tag values directly is less code and cannot be tripped
by an unclosed element. It also handles OFX 2.x unchanged, because
`<TRNTYPE>DEBIT</TRNTYPE>` and `<TRNTYPE>DEBIT` both yield "the text after the
open tag up to the next `<`".

Two things about OFX that shape the output:

**Direction comes from the sign of TRNAMT, not from TRNTYPE.** `TRNTYPE` is a
free-ish enumeration — DEBIT, CREDIT, POS, ATM, XFER, FEE, INT, DIRECTDEP,
CHECK — and issuers disagree on which of those imply a direction. The sign of
the amount does not vary: negative is money out, positive is money in. TRNTYPE
is kept as a narration hint only.

**There is no running balance per transaction.** OFX carries one
`<LEDGERBAL>` for the statement and nothing per row, so balance continuity
cannot be checked at all — not "checked and passed". Every OFX parse therefore
reports `continuity_passed = None` and raises `CONTINUITY_UNVERIFIABLE`.
"""
from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Dict, List, Optional

from app.b2b.canonical import CREDIT, DEBIT, CanonicalTxn, to_minor
from app.b2b.errors import ApiError, FILE_CORRUPT, NO_TRANSACTIONS_FOUND, PARSE_FAILED
from app.b2b.metrics import W_CONTINUITY_UNVERIFIABLE, W_MULTI_CURRENCY
from app.b2b.parsers.base import (
    ParseOutput,
    mask_account,
    observed_period,
    require_readable_file,
    set_balance,
)

logger = logging.getLogger(__name__)

_RE_STMTTRN = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.IGNORECASE | re.DOTALL)
_RE_OFX_ROOT = re.compile(r"<\s*OFX\s*>", re.IGNORECASE)

#: `YYYYMMDD` then optional `HHMMSS`, optional `.XXX` milliseconds, optional
#: `[offset:TZ]`. Only the leading date is load-bearing here.
_RE_OFX_DATE = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})")


def _tag_value(block: str, tag: str) -> str:
    """Value of `<TAG>` inside `block`, for SGML and XML alike.

    The value runs to the next `<`, which is the closing tag in OFX 2.x and the
    next opening tag in OFX 1.x. Newlines are stripped, because an SGML value
    ends at the line break rather than at a delimiter.
    """
    match = re.search(rf"<\s*{tag}\s*>([^<\r\n]*)", block, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def parse_ofx_datetime(value: str) -> Optional[datetime.date]:
    """OFX timestamp -> date. Returns None when the field is absent or junk.

    Format: `YYYYMMDD[HHMMSS][.XXX][[offset:TZ]]`.

    The timezone suffix is deliberately **not** applied. An OFX timestamp of
    `20240401000000[-5:EST]` describes midnight in the posting institution's
    own zone; normalising it into UTC would move that transaction to 31 March
    and put it in the wrong statement month, changing every monthly aggregate
    for the sake of an hours field nothing downstream reads. The posting date
    as the bank wrote it is the date this API reports.

    `normalize_date_string` in the existing normalizer cannot be reused: its
    format table has no bare `YYYYMMDD` entry, and the trailing timestamp and
    bracketed zone would defeat every pattern it does have.
    """
    if not value:
        return None
    match = _RE_OFX_DATE.match(value)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        return datetime.date(year, month, day)
    except ValueError:
        logger.debug("[b2b.ofx] impossible date in DTPOSTED %r", value)
        return None


def _read_text(path: str) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise ApiError(PARSE_FAILED, f"the uploaded file could not be read: {exc}")
    raise ApiError(FILE_CORRUPT, "the file is not decodable as text")


def _description(block: str) -> str:
    """NAME is the counterparty, MEMO the free text; issuers use either or both."""
    name = _tag_value(block, "NAME")
    memo = _tag_value(block, "MEMO")
    if name and memo and memo.lower() != name.lower():
        return f"{name} {memo}".strip()
    return (name or memo).strip()


def _statement_blocks(text: str) -> List[str]:
    """Each `<STMTRS>`/`<CCSTMTRS>` response body, or the whole document.

    A single OFX file can carry several accounts. Splitting on the response
    element keeps each one's CURDEF, LEDGERBAL and date range attached to its
    own transactions instead of letting the last account's balance overwrite
    the first's.
    """
    blocks = re.findall(r"<(?:CC)?STMTRS>(.*?)</(?:CC)?STMTRS>", text,
                        re.IGNORECASE | re.DOTALL)
    return blocks or [text]


def parse(path: str, *, password: Optional[str] = None,
          currency: str = "INR") -> ParseOutput:
    """Parse an OFX/QFX file. `password` is accepted and ignored."""
    require_readable_file(path)
    text = _read_text(path)

    if not _RE_OFX_ROOT.search(text) and "OFXHEADER" not in text.upper():
        raise ApiError(FILE_CORRUPT,
                       "the file does not contain an OFX document (no <OFX> element "
                       "and no OFXHEADER declaration)")

    transactions: List[CanonicalTxn] = []
    meta: Dict[str, Any] = {}
    currencies: List[str] = []
    undated = 0
    unamounted = 0
    row_index = 0

    for block in _statement_blocks(text):
        block_currency = (_tag_value(block, "CURDEF") or currency).upper()
        account_id = _tag_value(block, "ACCTID")
        if account_id and "account_number_masked" not in meta:
            meta["account_number_masked"] = mask_account(account_id)

        for stmt in _RE_STMTTRN.finditer(block):
            body = stmt.group(1)
            txn_date = parse_ofx_datetime(_tag_value(body, "DTPOSTED"))
            if txn_date is None:
                undated += 1
                continue

            raw_amount = _tag_value(body, "TRNAMT").replace(",", "")
            try:
                amount = float(raw_amount)
            except ValueError:
                unamounted += 1
                continue

            paise = to_minor(abs(amount))
            if paise is None:
                # A zero-value STMTTRN carries no movement. Keeping it would
                # add a row every sum ignores and every count includes.
                unamounted += 1
                continue

            direction = DEBIT if amount < 0 else CREDIT
            trn_type = _tag_value(body, "TRNTYPE")
            description = _description(body)
            if not description and trn_type:
                description = trn_type

            transactions.append(CanonicalTxn(
                txn_date=txn_date,
                direction=direction,
                debit_paise=paise if direction == DEBIT else None,
                credit_paise=paise if direction == CREDIT else None,
                # OFX has no per-transaction balance. Left None rather than
                # reconstructed from LEDGERBAL, which would be a fabrication
                # presented in the same field as an extracted figure.
                balance_paise=None,
                narration_raw=description,
                narration_clean=description,
                row_index=row_index,
                currency=block_currency,
                value_date=parse_ofx_datetime(_tag_value(body, "DTUSER")),
                reference_number=(_tag_value(body, "FITID")
                                  or _tag_value(body, "CHECKNUM") or None),
                parse_confidence=1.0,
                source_format="ofx",
                transaction_method=trn_type.upper() or None,
            ))
            currencies.append(block_currency)
            row_index += 1

        # Statement-level figures, taken from the last block that carries them.
        ledger = re.search(r"<LEDGERBAL>(.*?)(?:</LEDGERBAL>|<AVAILBAL>|$)",
                           block, re.IGNORECASE | re.DOTALL)
        if ledger:
            raw_bal = _tag_value(ledger.group(1), "BALAMT").replace(",", "")
            try:
                set_balance(meta, "closing_balance", float(raw_bal), basis="reported")
            except ValueError:
                pass

        start = parse_ofx_datetime(_tag_value(block, "DTSTART"))
        end = parse_ofx_datetime(_tag_value(block, "DTEND"))
        if start and end:
            meta["period_start"] = start.isoformat()
            meta["period_end"] = end.isoformat()
            meta["period_source"] = "declared"

    if not transactions:
        raise ApiError(NO_TRANSACTIONS_FOUND,
                       "the OFX document contained no usable <STMTTRN> entries")

    output = ParseOutput(transactions=transactions)

    distinct = sorted(set(currencies))
    meta["currency"] = distinct[0] if len(distinct) == 1 else currency
    if len(distinct) > 1:
        meta["currencies"] = distinct
        output.warn(W_MULTI_CURRENCY,
                    "this file mixes " + ", ".join(distinct) +
                    "; totals across different currencies are not comparable")
    if "period_start" not in meta:
        meta.update(observed_period(transactions))
    output.statement_meta = meta

    # The point of this parser's contract: nothing was checked, so nothing is
    # claimed. `continuity_pass_rate` stays None rather than 1.0.
    output.rows_checked_for_continuity = 0
    output.continuity_pass_rate = None
    output.continuity_passed = None
    output.warn(W_CONTINUITY_UNVERIFIABLE,
                "OFX carries no per-transaction running balance, so balance "
                "continuity cannot be verified for this format")

    if undated or unamounted:
        from app.b2b.metrics import W_ROWS_REJECTED
        output.warn(W_ROWS_REJECTED,
                    f"{undated + unamounted} <STMTTRN> entr(ies) were skipped for "
                    f"an unreadable DTPOSTED or TRNAMT")
    return output
