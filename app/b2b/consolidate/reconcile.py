"""Merge statements per account, remove duplicates, check balances, pair transfers.

Implements the two checks of the Credit Lens requirement exactly:

Check 1 — duplicates. Two rows are the same transaction when Account + Date +
Amount + Direction + Balance agree and the narrations are the same text (after
case/spacing normalisation, allowing one bank format to truncate what another
prints in full). The balance is what keeps genuine same-day repeats apart: two
₹5,000 UPI credits on one day leave different balances behind. Matching is a
multiset match, row by row — never "drop the overlapping day" — so a statement
downloaded part-way through a day contributes only the rows it really shares
(Example 2).

Cross-account rows are never duplicates of each other. A debit in one account
and the equal credit in another are an internal transfer and both are kept,
tagged "Internal Transfer" with the other account number (Example 3).

Check 2 — balance continuity. Per account, every statement's rows are merged in
date order, keeping each statement's own order within a day. Then for every
consecutive pair `previous balance ± amount = balance` must hold. A break is
reported with the date and the difference (Example 4: between statements;
Example 5: inside one statement, and against the statement's own printed
opening/closing balance).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import heapq
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.b2b.consolidate.extract import CREDIT, DEBIT, RawTxn, StatementExtract
from app.b2b.consolidate.tokens import fmt


@dataclass
class SourceStatement:
    file_name: str
    sha256: str
    extract: StatementExtract
    account_no: str
    bank_name: Optional[str]
    index: int                                      # upload order, for tie-breaks


@dataclass
class Txn:
    """One unique transaction after de-duplication."""
    uid: int
    account_no: str
    bank_name: Optional[str]
    row: RawTxn
    sources: List[Tuple[int, int]] = field(default_factory=list)   # (statement index, row index)
    category_1: Optional[str] = None
    category_2: Optional[str] = None
    flags: List[dict] = field(default_factory=list)
    transfer_peer: Optional[str] = None


def norm_narr(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def narrations_match(a: str, b: str) -> bool:
    na, nb = norm_narr(a), norm_narr(b)
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    if len(short) >= 8 and long_.startswith(short):
        return True               # one format truncates the other
    ra = set(re.findall(r"\d{9,}", na))
    rb = set(re.findall(r"\d{9,}", nb))
    return bool(ra & rb)          # same UTR / UPI reference


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# per-account merge
# --------------------------------------------------------------------------

def merge_account(stmts: List[SourceStatement], uid_start: int
                  ) -> Tuple[List[Txn], List[dict], int]:
    """De-duplicate and order all rows of one account. Returns (txns, duplicates, next uid)."""
    # Earliest statement first; for the same start, the longer one first so it
    # becomes the reference the shorter one is matched against.
    order = sorted(stmts, key=lambda s: (s.extract.rows[0].date if s.extract.rows else _dt.date.max,
                                         -len(s.extract.rows), s.index))
    txns: Dict[int, Txn] = {}
    index: Dict[tuple, List[int]] = {}
    sequences: List[List[int]] = []
    duplicates: List[dict] = []
    uid = uid_start

    for s in order:
        used: set = set()
        seq: List[int] = []
        for ri, r in enumerate(s.extract.rows):
            key = (r.date, r.amount_paise, r.direction, r.balance_paise)
            match = None
            for cand in index.get(key, []):
                if cand in used:
                    continue
                if any(src[0] == s.index for src in txns[cand].sources):
                    continue      # never collapse two rows of the SAME statement
                if narrations_match(txns[cand].row.narration, r.narration):
                    match = cand
                    break
            if match is not None:
                used.add(match)
                txns[match].sources.append((s.index, ri))
                seq.append(match)
                first = txns[match].sources[0][0]
                duplicates.append({
                    "account_no": s.account_no,
                    "date": r.date.isoformat(),
                    "narration": r.narration,
                    "amount": fmt(r.amount_paise),
                    "type": "Money In" if r.direction == CREDIT else "Money Out",
                    "balance": fmt(r.balance_paise),
                    "removed_from": s.file_name,
                    "kept_from": next(x.file_name for x in stmts if x.index == first),
                })
                continue
            t = Txn(uid=uid, account_no=s.account_no, bank_name=s.bank_name, row=r,
                    sources=[(s.index, ri)])
            txns[uid] = t
            index.setdefault(key, []).append(uid)
            seq.append(uid)
            uid += 1
        sequences.append(seq)

    ordered = _topo_merge(txns, sequences)
    return ordered, duplicates, uid


def _topo_merge(txns: Dict[int, Txn], sequences: List[List[int]]) -> List[Txn]:
    """Order rows so every statement's own order is respected, dates ascending.

    Each statement contributes 'a before b' for its consecutive rows; a shared
    (de-duplicated) row joins the constraints of both statements, which is how
    a partial day in one statement and the full day in another interleave
    correctly (Example 2). Ties go to date, then first appearance.
    """
    succ: Dict[int, set] = {u: set() for u in txns}
    indeg: Dict[int, int] = {u: 0 for u in txns}
    first_seen: Dict[int, Tuple[int, int]] = {}
    for si, seq in enumerate(sequences):
        for pos, u in enumerate(seq):
            first_seen.setdefault(u, (si, pos))
        for a, b in zip(seq, seq[1:]):
            if a != b and b not in succ[a] and txns[a].row.date <= txns[b].row.date:
                succ[a].add(b)
                indeg[b] += 1
    heap = [(txns[u].row.date, first_seen[u], u) for u in txns if indeg[u] == 0]
    heapq.heapify(heap)
    out: List[Txn] = []
    done = set()
    while heap:
        _, _, u = heapq.heappop(heap)
        if u in done:
            continue
        done.add(u)
        out.append(txns[u])
        for v in succ[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                heapq.heappush(heap, (txns[v].row.date, first_seen[v], v))
    if len(out) < len(txns):      # conflicting orders (a cycle): fall back to date + first seen
        rest = sorted((u for u in txns if u not in done),
                      key=lambda u: (txns[u].row.date, first_seen[u]))
        out.extend(txns[u] for u in rest)
        out.sort(key=lambda t: t.row.date)   # stable: keeps the topological order within a day
    return out


# --------------------------------------------------------------------------
# balance checks
# --------------------------------------------------------------------------

def _files_of(t: Txn, by_index: Dict[int, SourceStatement]) -> List[str]:
    return sorted({by_index[s].file_name for s, _ in t.sources})


def continuity_flags(account_no: str, txns: List[Txn],
                     by_index: Dict[int, SourceStatement]) -> List[dict]:
    flags = []
    for prev, cur in zip(txns, txns[1:]):
        pb, cb = prev.row.balance_paise, cur.row.balance_paise
        if pb is None or cb is None or cur.row.direction is None:
            continue
        sign = 1 if cur.row.direction == CREDIT else -1
        expected = pb + sign * cur.row.amount_paise
        if expected == cb:
            continue
        diff = cb - expected
        prev_stmts = {s for s, _ in prev.sources}
        cur_stmts = {s for s, _ in cur.sources}
        between = not (prev_stmts & cur_stmts)
        flag = {
            "type": "MISSING_TRANSACTIONS_BETWEEN_STATEMENTS" if between
                    else "BALANCE_BREAK_WITHIN_STATEMENT",
            "account_no": account_no,
            "date": cur.row.date.isoformat(),
            "previous_date": prev.row.date.isoformat(),
            "previous_balance": fmt(pb),
            "amount": fmt(cur.row.amount_paise),
            "expected_balance": fmt(expected),
            "actual_balance": fmt(cb),
            "difference": fmt(diff),
            "message": (
                f"Balance does not flow: {fmt(pb):,.2f} {'+' if sign > 0 else '-'} "
                f"{fmt(cur.row.amount_paise):,.2f} should be {fmt(expected):,.2f} but the "
                f"statement shows {fmt(cb):,.2f} — a difference of {fmt(diff):,.2f}, so "
                + ("transactions between these statements are missing."
                   if between else "a row was missed or misread.")),
            "previous_statement": _files_of(prev, by_index),
            "statement": _files_of(cur, by_index),
        }
        flags.append(flag)
        cur.flags.append({"type": flag["type"], "difference": flag["difference"]})
    return flags


def statement_reconciliation(s: SourceStatement) -> Tuple[dict, Optional[dict]]:
    """Opening + Σ(in) − Σ(out) against the statement's own closing (Example 5)."""
    ex = s.extract
    rows = ex.rows
    total_in = sum(r.amount_paise for r in rows if r.direction == CREDIT)
    total_out = sum(r.amount_paise for r in rows if r.direction == DEBIT)
    opening, opening_basis = ex.opening_paise, "printed"
    if opening is None and rows and rows[0].balance_paise is not None and rows[0].direction:
        sign = 1 if rows[0].direction == CREDIT else -1
        opening, opening_basis = rows[0].balance_paise - sign * rows[0].amount_paise, "derived_from_first_row"
    closing, closing_basis = ex.closing_paise, "printed"
    if closing is None and rows:
        closing, closing_basis = rows[-1].balance_paise, "last_row_balance"
    computed = None if opening is None else opening + total_in - total_out
    diff = None if computed is None or closing is None else closing - computed
    status = "NOT_VERIFIABLE" if diff is None else ("PASSED" if diff == 0 else "FAILED")
    rec = {
        "file_name": s.file_name,
        "account_no": s.account_no,
        "bank_name": s.bank_name,
        "account_holder": ex.meta.account_holder,
        "period_from": ex.meta.period_from or (rows[0].date.isoformat() if rows else None),
        "period_to": ex.meta.period_to or (rows[-1].date.isoformat() if rows else None),
        "pages": ex.page_count,
        "transactions_extracted": len(rows),
        "opening_balance": fmt(opening), "opening_balance_basis": opening_basis,
        "total_money_in": fmt(total_in), "total_money_out": fmt(total_out),
        "computed_closing_balance": fmt(computed),
        "stated_closing_balance": fmt(closing), "closing_balance_basis": closing_basis,
        "difference": fmt(diff),
        "status": status,
        "extraction_method": ex.strategy,
        "notes": ex.warnings,
    }
    flag = None
    if status == "FAILED":
        flag = {
            "type": "CLOSING_BALANCE_MISMATCH",
            "account_no": s.account_no,
            "date": rows[-1].date.isoformat() if rows else None,
            "statement": [s.file_name],
            "opening_balance": fmt(opening),
            "computed_closing_balance": fmt(computed),
            "stated_closing_balance": fmt(closing),
            "difference": fmt(diff),
            "message": (f"Opening {fmt(opening):,.2f} plus the extracted entries gives "
                        f"{fmt(computed):,.2f}, but the statement closes at {fmt(closing):,.2f}: "
                        f"{fmt(diff):,.2f} is unaccounted for — a row was missed or misread "
                        "(typically an entry split across pages)."),
        }
    return rec, flag


# --------------------------------------------------------------------------
# internal transfers
# --------------------------------------------------------------------------

_NAME_STOP = {"PRIVATE", "LIMITED", "PVT", "LTD", "THE", "AND", "M/S", "MR", "MRS", "MS", "SMT"}


def _holder_in(holder: Optional[str], text: str) -> bool:
    """Is this account holder named in the narration?

    Two distinctive words must appear ('RAKSHITHA SQUARE', 'RAKSHITHA
    ELECTRICALS'); a name with only one distinctive word ('KRISHNA G') must
    appear whole. One shared first name is not evidence — a payer who happens
    to be called Rakshitha is not the company Rakshitha Square Infra.
    """
    if not holder:
        return False
    words = [w for w in re.findall(r"[A-Z0-9]+", holder.upper()) if w not in _NAME_STOP]
    distinctive = [w for w in words if len(w) >= 3]
    padded = " " + re.sub(r"[^A-Z0-9]+", " ", text.upper()) + " "
    if len(distinctive) >= 2:
        return all(f" {w} " in padded for w in distinctive[:2])
    full = " ".join(words)
    return bool(full) and len(full) >= 5 and f" {full} " in padded


def _evidence(debit: Txn, credit: Txn, holders: Dict[str, Optional[str]]) -> Optional[str]:
    nd, nc = debit.row.narration.upper(), credit.row.narration.upper()
    refs_d = set(re.findall(r"\d{9,}", re.sub(r"[^0-9A-Z]", " ", nd)))
    refs_c = set(re.findall(r"\d{9,}", re.sub(r"[^0-9A-Z]", " ", nc)))
    refs_d -= {debit.account_no, credit.account_no}
    refs_c -= {debit.account_no, credit.account_no}
    if refs_d & refs_c:
        return "shared reference number"
    for acct, text in ((credit.account_no, nd), (debit.account_no, nc)):
        digits = re.sub(r"\D", "", acct or "")
        if len(digits) >= 6 and (digits in re.sub(r"\D", "", text) or
                                 re.search(r"X{2,}" + digits[-4:] + r"\b", text)):
            return "account number in narration"
    # The other account holder's name in the narration.
    for own, other_text in ((credit.account_no, nd), (debit.account_no, nc)):
        if _holder_in(holders.get(own), other_text):
            return "account holder name in narration"
    return None


def pair_internal_transfers(all_txns: List[Txn], holders: Dict[str, Optional[str]],
                            max_days: int = 3) -> List[dict]:
    """Money Out of one supplied account matched to Money In of another."""
    by_amount: Dict[int, List[Txn]] = {}
    for t in all_txns:
        if t.row.direction == CREDIT:
            by_amount.setdefault(t.row.amount_paise, []).append(t)
    pairs = []
    taken: set = set()
    debits = sorted((t for t in all_txns if t.row.direction == DEBIT), key=lambda t: t.row.date)
    for d in debits:
        best = None
        for c in by_amount.get(d.row.amount_paise, []):
            if c.uid in taken or c.account_no == d.account_no:
                continue
            gap = (c.row.date - d.row.date).days
            if not (-1 <= gap <= max_days):
                continue
            why = _evidence(d, c, holders)
            if not why or (gap < 0 and why != "shared reference number"):
                continue      # money does not arrive before it leaves, bar a shared UTR
            score = (abs(gap), c.uid)
            if best is None or score < best[0]:
                best = (score, c, why)
        if best is None:
            continue
        _, c, why = best
        taken.add(c.uid)
        d.category_1 = c.category_1 = "Internal Transfer"
        d.category_2, c.category_2 = c.account_no, d.account_no
        d.transfer_peer, c.transfer_peer = c.account_no, d.account_no
        pairs.append({"from_account": d.account_no, "to_account": c.account_no,
                      "amount": fmt(d.row.amount_paise), "debit_date": d.row.date.isoformat(),
                      "credit_date": c.row.date.isoformat(), "evidence": why})
    return pairs
