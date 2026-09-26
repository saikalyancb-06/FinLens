"""JSON statements, in the three shapes integrators actually send.

There is no standard for a JSON bank statement, so rather than inventing one
and rejecting everything else, this parser accepts the three envelopes seen in
practice —

    [ {...}, {...} ]                       a bare list of transactions
    { "transactions": [ ... ] }            the common wrapper
    { "data": { "transactions": [ ... ] } } an API response pasted verbatim

— and then reuses the tabular machinery for the field names. Each object is
flattened into a `header: value` mapping and handed to `map_headers`, which
means the ~130 column aliases already tuned on spreadsheets apply unchanged:
`narration`, `withdrawal (dr)`, `chq/ref no` and `value_date` all resolve here
for free, alongside the plain ISO names `date` / `description` / `amount` /
`type` / `balance`.

Two things JSON does that a spreadsheet does not, handled explicitly:

* **Signed amounts.** A JSON feed usually encodes direction as a negative
  number rather than a `type` column. `build_normalized_transaction` takes the
  absolute value, so the sign would be lost. If any record in the document
  carries a negative amount, the file is treated as using the signed
  convention and every unlabelled row gets its direction from the sign.
* **Per-row currency.** A `currency` field is read per record; more than one
  distinct value raises `MULTI_CURRENCY`.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.b2b.errors import ApiError, FILE_CORRUPT, NO_TRANSACTIONS_FOUND, PARSE_FAILED
from app.b2b.metrics import W_MULTI_CURRENCY
from app.b2b.parsers.base import ParseOutput, require_readable_file
from app.b2b.parsers.delimited import grid_to_normalized_rows, validated_output

logger = logging.getLogger(__name__)

#: Keys whose value may hold the transaction list, in preference order.
_LIST_KEYS = ("transactions", "txns", "entries", "rows", "records", "items")
#: Wrapper objects to look inside before giving up.
_ENVELOPE_KEYS = ("data", "result", "payload", "statement")

#: Field names carrying an ISO currency code on a per-row basis.
_CURRENCY_KEYS = ("currency", "ccy", "currency_code", "curr")

_MAX_FLATTEN_DEPTH = 3


def _load(path: str) -> Any:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as handle:
                return json.load(handle)
        except UnicodeDecodeError:
            continue
        except json.JSONDecodeError as exc:
            raise ApiError(
                FILE_CORRUPT,
                f"the file is not valid JSON (line {exc.lineno}, column {exc.colno})",
            )
        except OSError as exc:
            raise ApiError(PARSE_FAILED, f"the uploaded file could not be read: {exc}")
    raise ApiError(FILE_CORRUPT, "the file is not decodable as text")


def extract_records(document: Any) -> List[Dict[str, Any]]:
    """Find the transaction list inside whichever envelope was used."""
    if isinstance(document, list):
        records = document
    elif isinstance(document, dict):
        records = None
        for key in _LIST_KEYS:
            value = document.get(key)
            if isinstance(value, list):
                records = value
                break
        if records is None:
            for envelope in _ENVELOPE_KEYS:
                inner = document.get(envelope)
                if isinstance(inner, dict):
                    for key in _LIST_KEYS:
                        value = inner.get(key)
                        if isinstance(value, list):
                            records = value
                            break
                if records is not None:
                    break
        if records is None:
            raise ApiError(
                PARSE_FAILED,
                "no transaction list found; expected a top-level array, "
                "a 'transactions' key, or 'data.transactions'",
            )
    else:
        raise ApiError(PARSE_FAILED,
                       "the JSON document is neither an array nor an object")

    objects = [r for r in records if isinstance(r, dict)]
    if not objects:
        raise ApiError(NO_TRANSACTIONS_FOUND,
                       "the transaction list is empty or contains no objects")
    return objects


def flatten(record: Dict[str, Any], _prefix: str = "", _depth: int = 0) -> Dict[str, str]:
    """One record -> flat `header: text` pairs.

    Nested objects are joined with a dot (`txn.value_date`). That still matches
    the aliases, because `map_headers` accepts a substring match — `date` is
    found inside `txn.value_date` — so a nested feed needs no special casing.
    Lists are skipped: a bank statement row has no repeating field that belongs
    in a column, and flattening one would invent header names per record and
    break the shared header list.
    """
    flat: Dict[str, str] = {}
    for key, value in record.items():
        name = f"{_prefix}{key}"
        if isinstance(value, dict) and _depth < _MAX_FLATTEN_DEPTH:
            flat.update(flatten(value, f"{name}.", _depth + 1))
        elif isinstance(value, (list, dict)):
            continue
        elif value is None:
            flat[name] = ""
        elif isinstance(value, bool):
            flat[name] = "true" if value else "false"
        else:
            flat[name] = str(value)
    return flat


def _amount_is_negative(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("-") or (stripped.startswith("(") and stripped.endswith(")"))


def _resolve_currencies(flats: List[Dict[str, str]],
                        default: str) -> Tuple[List[str], Optional[str]]:
    """Per-row currency codes, plus a message when the statement is mixed."""
    codes: List[str] = []
    for flat in flats:
        code = ""
        for key, value in flat.items():
            leaf = key.rsplit(".", 1)[-1].strip().lower()
            if leaf in _CURRENCY_KEYS and value.strip():
                code = value.strip().upper()[:8]
                break
        codes.append(code or default)
    distinct = sorted(set(codes))
    if len(distinct) > 1:
        return codes, ("this statement mixes " + ", ".join(distinct) +
                       "; totals across different currencies are not comparable")
    return codes, None


def parse(path: str, *, password: Optional[str] = None,
          currency: str = "INR") -> ParseOutput:
    """Parse a JSON statement. `password` is accepted and ignored."""
    require_readable_file(path)
    document = _load(path)
    records = extract_records(document)
    flats = [flatten(r) for r in records]
    flats = [f for f in flats if f]
    if not flats:
        raise ApiError(NO_TRANSACTIONS_FOUND,
                       "the transaction objects carried no scalar fields")

    # Source keys, in first-seen order across every record, so a field present
    # only on later rows still gets a column.
    source_keys: List[str] = []
    seen = set()
    for flat in flats:
        for key in flat:
            if key not in seen:
                seen.add(key)
                source_keys.append(key)

    # The header the alias table sees is the *leaf* name wherever that leaf is
    # unique in the document. Keeping the dotted path would let a container
    # name poison the match: `amounts.debit` contains the alias `amount`, which
    # is six characters against `debit`'s five, so `map_headers` would bind the
    # debit column to the amount field and every debit would parse as an
    # unlabelled amount. Only a genuinely ambiguous leaf — the same name under
    # two parents — keeps its dotted form, where the path is the only thing
    # telling the two apart.
    leaf_counts: Dict[str, int] = {}
    for key in source_keys:
        leaf = key.rsplit(".", 1)[-1]
        leaf_counts[leaf] = leaf_counts.get(leaf, 0) + 1
    headers = [
        key.rsplit(".", 1)[-1] if leaf_counts[key.rsplit(".", 1)[-1]] == 1 else key
        for key in source_keys
    ]

    codes, mixed_message = _resolve_currencies(flats, currency)

    # Signed-amount convention: decided for the document as a whole, because a
    # feed that signs its debits signs all of them. Deciding per row would make
    # an all-positive statement's credits indistinguishable from its debits.
    def _leaf(name: str) -> str:
        return name.rsplit(".", 1)[-1].strip().lower()

    amount_keys = [k for k in source_keys
                   if _leaf(k) in ("amount", "amt", "transaction_amount", "txn_amount")]
    signed_convention = any(
        _amount_is_negative(flat.get(k, "")) for flat in flats for k in amount_keys
    )
    type_index: Optional[int] = next(
        (i for i, k in enumerate(source_keys)
         if _leaf(k) in ("type", "transaction_type", "txn_type", "dr/cr", "drcr")),
        None,
    )
    if signed_convention and type_index is None:
        # No direction column at all: synthesise one rather than returning rows
        # whose direction is "unknown" when the sign already stated it.
        source_keys.append("__direction__")
        headers.append("transaction_type")
        type_index = len(headers) - 1

    grid: List[List[str]] = [headers]
    for flat in flats:
        row = [flat.get(key, "") for key in source_keys]
        if signed_convention and type_index is not None and amount_keys:
            if not row[type_index].strip():
                negative = any(_amount_is_negative(flat.get(k, "")) for k in amount_keys)
                row[type_index] = "debit" if negative else "credit"
        grid.append(row)

    rows = grid_to_normalized_rows(grid, source_method="json")
    output = validated_output(rows, source_format="json", currency=currency)

    # Currencies are applied after validation because the validator works on the
    # normalizer dicts, which have no currency concept. Row order is preserved
    # through the tabular path, and dropped rows only ever come off the end of
    # the mapping by index, so a positional zip is safe here.
    if len(codes) == len(output.transactions):
        for txn, code in zip(output.transactions, codes):
            txn.currency = code
    if mixed_message:
        output.warn(W_MULTI_CURRENCY, mixed_message)
        output.statement_meta["currencies"] = sorted(set(codes))
    return output
