"""PDF bank statement -> ordered transaction rows, for real Indian layouts.

Two strategies, both run, the better one kept:

* **table** — statements drawn with cell borders (Axis, SBI, IOB net-banking).
  pdfplumber recovers each cell, including multi-line narrations, cleanly. The
  header row is mapped to roles by keyword; pages whose table has no header
  reuse the previous page's mapping.
* **lines** — statements with no cell borders (Bank of Baroda, IOB passbook and
  IOB branch printouts). Words are grouped into physical lines; a line that
  starts with a date (optionally after a serial number) and carries amounts is
  a transaction; lines between transactions are narration continuations and go
  to the nearest transaction line.

"Better" is decided by the statement's own arithmetic: the strategy whose rows
satisfy `previous balance ± amount = balance` on more row pairs wins. That is
the one test a parser cannot fake, and it is exactly Check 2 of the spec — so
the extractor is chosen by the same yardstick its output is judged by.

Rows come out in CHRONOLOGICAL order. Statements printed newest-first (the IOB
passbook and IOB 14099 layouts) are detected from their dates and reversed, so
"keep each statement's row order within a day" means the order in which the
transactions actually happened.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.b2b.consolidate.metadata import StatementMeta, detect_holder, detect_meta
from app.b2b.consolidate.tokens import (
    balance_paise, first_date, parse_amount, parse_date,
)

logger = logging.getLogger(__name__)

CREDIT = "CREDIT"
DEBIT = "DEBIT"


@dataclass
class RawTxn:
    date: object                      # datetime.date
    narration: str
    amount_paise: int                 # always positive
    direction: Optional[str]          # CREDIT / DEBIT, None if the layout did not say
    balance_paise: Optional[int]
    page: int
    value_date: object = None
    reference: Optional[str] = None
    direction_source: str = "column"  # column | suffix | drcr | balance


@dataclass
class StatementExtract:
    meta: StatementMeta
    rows: List[RawTxn]
    opening_paise: Optional[int] = None     # balance before the first row
    closing_paise: Optional[int] = None     # printed closing balance, if any
    strategy: str = ""
    page_count: int = 0
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# column roles
# --------------------------------------------------------------------------

def _role(header: str) -> Optional[str]:
    h = re.sub(r"\s+", " ", (header or "").replace("\n", " ")).strip().lower()
    if not h:
        return None
    if h in {"dr/cr", "cr/dr", "debit/credit", "credit/debit", "type"} or h.startswith("dr/cr"):
        return "drcr"
    if "balance" in h or h in {"शेष"}:
        return "balance"
    if h.startswith("date") or ("date" in h and "value" not in h):
        return "date"          # includes IOB's 'Date(Value Date)'
    if h.startswith("value"):
        return "value_date"
    if any(k in h for k in ("particular", "description", "narration", "naration", "remarks", "details")):
        return "narration"
    if any(k in h for k in ("withdrawal", "debit")):
        return "debit"
    if any(k in h for k in ("deposit", "credit")):
        return "credit"
    if "amount" in h:
        return "amount"
    if any(k in h for k in ("chq", "cheque", "ref")):
        return "reference"
    if h in {"s.no", "sno", "sr.no", "sr no", "s no"}:
        return "sno"
    return None


def _map_header(cells: List[Optional[str]]) -> Optional[Dict[str, int]]:
    roles: Dict[str, int] = {}
    for i, c in enumerate(cells):
        r = _role(c or "")
        if r and r not in roles:
            roles[r] = i
    has_amount = ("debit" in roles and "credit" in roles) or "amount" in roles
    if "date" in roles and "balance" in roles and has_amount:
        roles.setdefault("narration", -1)
        return roles
    return None


def _clean_narr(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()


def _cell(row, idx):
    if idx is None or idx < 0 or idx >= len(row):
        return None
    return row[idx]


# --------------------------------------------------------------------------
# strategy 1: ruled tables
# --------------------------------------------------------------------------

def _table_strategy(pdf) -> Tuple[List[RawTxn], Optional[int], List[str]]:
    rows: List[RawTxn] = []
    opening = None
    closing = None
    warnings: List[str] = []
    mapping: Optional[Dict[str, int]] = None
    ncols = None
    for pno, page in enumerate(pdf.pages, 1):
        try:
            tables = page.find_tables()
        except Exception:                      # noqa: BLE001
            continue
        datas = []
        for table in tables:
            try:
                datas.append(table.extract())
            except Exception:                  # noqa: BLE001
                continue
        # pdfplumber caches every character object of a page until the page is
        # closed; on a 57-page statement that cache was ~700 MB. Rows are
        # already extracted, so release it now.
        _release(page)
        for data in datas:
            if not data:
                continue
            for raw in data:
                cells = [c if c is None else str(c) for c in raw]
                hm = _map_header(cells)
                if hm:
                    mapping, ncols = hm, len(cells)
                    continue
                if mapping is None or len(cells) != ncols:
                    continue
                txn, op = _row_from_cells(cells, mapping, pno)
                if op is not None and not rows:
                    opening = op
                    continue
                if txn is None:
                    # No date: a narration that spilled into its own row (across
                    # a page break, typically) belongs to the previous entry.
                    extra = _clean_narr(_cell(cells, mapping.get("narration")))
                    if "CLOSING" in extra.upper():
                        closing = balance_paise(_cell(cells, mapping.get("balance")))
                    has_money = any(parse_amount(_cell(cells, mapping.get(k)))
                                    for k in ("debit", "credit", "amount", "balance"))
                    if extra and rows and not has_money and "OPENING" not in extra.upper() \
                            and "CLOSING" not in extra.upper():
                        rows[-1].narration = (rows[-1].narration + " " + extra).strip()
                    continue
                rows.append(txn)
    if closing is not None:
        warnings.append(f"__closing__={closing}")
    return rows, opening, warnings


def _row_from_cells(cells, m, page) -> Tuple[Optional[RawTxn], Optional[int]]:
    date = first_date(_cell(cells, m.get("date")))
    narr = _clean_narr(_cell(cells, m.get("narration")))
    bal = balance_paise(_cell(cells, m.get("balance")))
    if date is None:
        if ("OPENING" in narr.upper() or "BROUGHT FORWARD" in narr.upper()) and bal is not None:
            return None, bal
        # Some layouts print the opening balance in the amount/balance cell of
        # a dateless first row.
        return None, None

    amount = None
    direction = None
    source = "column"
    if "debit" in m and "credit" in m:
        d = parse_amount(_cell(cells, m["debit"]))
        c = parse_amount(_cell(cells, m["credit"]))
        if d and d[0] != 0 and not (c and c[0] != 0):
            amount, direction = abs(d[0]), DEBIT
        elif c and c[0] != 0 and not (d and d[0] != 0):
            amount, direction = abs(c[0]), CREDIT
    elif "amount" in m:
        a = parse_amount(_cell(cells, m["amount"]))
        if a:
            amount = abs(a[0])
            flag = (_cell(cells, m.get("drcr")) or "").strip().upper()
            if flag.startswith("CR") or flag == "C":
                direction, source = CREDIT, "drcr"
            elif flag.startswith("DR") or flag == "D":
                direction, source = DEBIT, "drcr"
            elif a[1]:
                direction, source = (CREDIT if a[1] == "CR" else DEBIT), "suffix"
    if amount is None:
        if "OPENING" in narr.upper() and bal is not None:
            return None, bal
        return None, None
    return RawTxn(date=date, narration=narr, amount_paise=amount, direction=direction,
                  balance_paise=bal, page=page,
                  value_date=first_date(_cell(cells, m.get("value_date"))),
                  reference=_clean_narr(_cell(cells, m.get("reference"))) or None,
                  direction_source=source), None


# --------------------------------------------------------------------------
# strategy 2: text lines
# --------------------------------------------------------------------------

_STOP_LINE = re.compile(
    r"(page\s*total|grand\s*total|this is a (computer|system)|end of statement|page \d+ of|"
    r"statement summary|brought forward|ffd balance|date stamp|abbreviation|legend)", re.I)
_HEADER_WORDS = re.compile(r"^(date|particulars|description|narration|balance|withdrawals?|"
                           r"deposits?|debit|credit|chq\.?no\.?|cheque|value|sr\.?no|number)$", re.I)


def _lines(page, tol=2.5):
    words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
    words.sort(key=lambda w: (round(w["top"]), w["x0"]))
    lines: List[List[dict]] = []
    for w in words:
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
    return lines


def _anchor(line) -> Optional[Tuple[object, int]]:
    """(date, index of first word after the date) if this line starts a transaction."""
    toks = [w["text"] for w in line]
    for start in (0, 1):
        if start >= len(toks):
            break
        if start == 1 and not re.fullmatch(r"\d{1,5}", toks[0]):
            break
        d = parse_date(toks[start])
        if d is None and start + 2 < len(toks):
            d = parse_date(" ".join(toks[start:start + 3]))  # '23 Mar 2026'
            if d:
                return d, start + 3
        if d:
            return d, start + 1
    return None


def _header_columns(lines) -> Dict[str, float]:
    """Right edges (x1) of the money columns, from the header row(s), if printed."""
    cols: Dict[str, float] = {}
    for ln in lines[:60]:
        for w in ln:
            t = w["text"].strip().lower().rstrip(".")
            if t in ("withdrawals", "withdrawal", "debit", "नामे") and "debit" not in cols:
                cols["debit"] = w["x1"]
            elif t in ("deposits", "deposit", "credit", "जमा") and "credit" not in cols:
                cols["credit"] = w["x1"]
            elif t in ("balance", "शेष") and "balance" not in cols:
                cols["balance"] = w["x1"]
        if len(cols) == 3:
            break
    return cols


def _money_tokens(line, start_idx):
    """[(x1, text)] for amount-or-dash tokens after the date, left to right."""
    out = []
    for w in line[start_idx:]:
        t = w["text"]
        if parse_amount(t) is not None or t in ("-", "--"):
            out.append((w["x1"], t, w["x0"]))
    return out


def _release(page) -> None:
    try:
        page.close()               # pdfplumber >= 0.10: flushes the object cache
    except Exception:              # noqa: BLE001
        pass


def _lines_strategy(pdf) -> Tuple[List[RawTxn], Optional[int], List[str]]:
    page_lines = []
    for p in pdf.pages:
        page_lines.append(_lines(p))
        _release(p)
    header = _header_columns(page_lines[0]) if page_lines else {}
    anchors: List[dict] = []
    conts: List[dict] = []
    opening = None
    narr_x0 = None
    for pno, lines in enumerate(page_lines, 1):
        stopped = False
        for ln in lines:
            text = " ".join(w["text"] for w in ln)
            if _STOP_LINE.search(text):
                stopped = True if re.search(r"page\s*total|grand\s*total|end of statement",
                                            text, re.I) else stopped
                continue
            a = _anchor(ln)
            if a:
                stopped = False
                date, idx = a
                money = _money_tokens(ln, idx)
                real = [m for m in money if m[1] not in ("-", "--")]
                if not real:
                    continue
                first_money_x0 = money[0][2]
                vdate = None
                if idx < len(ln) and parse_date(ln[idx]["text"]):
                    vdate = parse_date(ln[idx]["text"])     # value-date column
                    idx += 1
                narr_words = [w for w in ln[idx:] if w["x1"] <= first_money_x0 + 0.5
                              and not (parse_amount(w["text"]) or w["text"] in ("-", "--"))]
                if narr_words:
                    narr_x0 = narr_words[0]["x0"] if narr_x0 is None else min(narr_x0, narr_words[0]["x0"])
                anchors.append({"page": pno, "top": ln[0]["top"], "date": date, "money": money,
                                "narr": narr_words, "extra": [],
                                "value_date": vdate, "text": text})
                continue
            if stopped:
                continue
            if any(_HEADER_WORDS.match(w["text"]) for w in ln) and len(ln) <= 12:
                continue
            conts.append({"page": pno, "top": ln[0]["top"], "words": ln})

    # Narration continuation lines: only words inside the narration band, and
    # only lines close to a transaction line on the same page.
    by_page: Dict[int, List[dict]] = {}
    for a in anchors:
        by_page.setdefault(a["page"], []).append(a)
    heights = [b["top"] - a["top"] for p in by_page.values() for a, b in zip(p, p[1:])
               if 0 < b["top"] - a["top"] < 80]
    line_h = sorted(heights)[len(heights) // 4] if heights else 12.0
    money_x0 = min((m[2] for a in anchors for m in a["money"]), default=10_000)
    for c in conts:
        cands = by_page.get(c["page"], [])
        if not cands:
            continue
        best = min(cands, key=lambda a: (abs(a["top"] - c["top"]), a["top"] < c["top"]))
        if abs(best["top"] - c["top"]) > max(line_h * 1.6, 14):
            continue
        words = [w for w in c["words"]
                 if (narr_x0 is None or w["x0"] >= narr_x0 - 2) and w["x1"] <= money_x0]
        if words:
            best["extra"].append((c["top"], words))

    rows: List[RawTxn] = []
    # Right edge of the narration band: a line that reaches it was cut by the
    # cell width mid-word, so its continuation joins without a space
    # ('.../Monthl' + 'ySubsc' -> '.../MonthlySubsc').
    ends = [ws[-1]["x1"] for a in anchors for _, ws in [(0, a["narr"])] + a["extra"] if ws]
    narr_right = max(ends) if ends else None
    for a in anchors:
        money = a["money"]
        real = [m for m in money if m[1] not in ("-", "--")]
        pieces = sorted([(a["top"], a["narr"])] + a["extra"], key=lambda p: p[0])
        narration = _join_wrapped([ws for _, ws in pieces if ws], narr_right)
        if len(real) == 1:
            # Only a balance: the opening-balance line.
            if not rows and opening is None:
                opening = balance_paise(real[0][1])
            continue
        bal_tok = money[-1]
        bal = balance_paise(bal_tok[1]) if bal_tok[1] not in ("-", "--") else None
        amt_toks = [m for m in money[:-1] if m[1] not in ("-", "--")]
        if not amt_toks:
            continue
        amt_x1, amt_text, _ = amt_toks[-1]
        amount, suffix = parse_amount(amt_text)
        amount = abs(amount)
        direction, source = None, "column"
        if suffix:
            direction, source = (CREDIT if suffix == "CR" else DEBIT), "suffix"
        elif "debit" in header and "credit" in header:
            direction = DEBIT if abs(amt_x1 - header["debit"]) < abs(amt_x1 - header["credit"]) else CREDIT
        elif len(money) >= 3:
            # '-' placeholders keep the columns positional: [debit, credit, balance].
            direction = DEBIT if money[-3][1] not in ("-", "--") else CREDIT
        rows.append(RawTxn(date=a["date"], narration=narration, amount_paise=amount,
                           direction=direction, balance_paise=bal, page=a["page"],
                           value_date=a["value_date"], direction_source=source))

    # No header and no suffix (IOB branch printout): the debit and credit
    # columns are two clusters of right edges. Split them and name them by the
    # balance arithmetic, which is unambiguous on any row with a neighbour.
    if rows and any(r.direction is None for r in rows):
        _assign_by_clusters(rows, anchors)
    return rows, opening, []


def _join_wrapped(pieces, narr_right) -> str:
    out = ""
    for i, ws in enumerate(pieces):
        text = " ".join(w["text"] for w in ws)
        if not out:
            out = text
            continue
        prev_end = pieces[i - 1][-1]["x1"]
        glue = "" if (narr_right is not None and prev_end >= narr_right - 4
                      and len(pieces[i - 1]) == 1) else " "
        out = out + glue + text
    return _clean_narr(out)


def _assign_by_clusters(rows: List[RawTxn], anchors: List[dict]) -> None:
    xs = []
    for a in anchors:
        real = [m for m in a["money"] if m[1] not in ("-", "--")]
        if len(real) >= 2:
            xs.append(real[-2][0])
    if not xs:
        return
    xs_sorted = sorted(xs)
    # Largest gap splits the two columns.
    gaps = [(b - a, i) for i, (a, b) in enumerate(zip(xs_sorted, xs_sorted[1:]))]
    if not gaps:
        return
    gap, i = max(gaps)
    cut = (xs_sorted[i] + xs_sorted[i + 1]) / 2 if gap > 8 else None
    votes = {"left_is_debit": 0, "left_is_credit": 0}
    sides = []
    ai = 0
    for a in anchors:
        real = [m for m in a["money"] if m[1] not in ("-", "--")]
        if len(real) >= 2:
            sides.append("left" if cut is None or real[-2][0] < cut else "right")
    for r_prev, r, side in zip([None] + rows[:-1], rows, sides):
        if r_prev is None or r_prev.balance_paise is None or r.balance_paise is None:
            continue
        delta = r.balance_paise - r_prev.balance_paise
        if delta == -r.amount_paise:
            votes["left_is_debit" if side == "left" else "left_is_credit"] += 1
        elif delta == r.amount_paise:
            votes["left_is_credit" if side == "left" else "left_is_debit"] += 1
    left_debit = votes["left_is_debit"] >= votes["left_is_credit"]
    for r, side in zip(rows, sides):
        if r.direction is None:
            is_left = side == "left"
            r.direction = DEBIT if is_left == left_debit else CREDIT
            r.direction_source = "column"


# --------------------------------------------------------------------------
# ordering and scoring
# --------------------------------------------------------------------------

def _chronological(rows: List[RawTxn]) -> Tuple[List[RawTxn], bool]:
    if len(rows) < 2:
        return rows, False
    fwd = sum(1 for a, b in zip(rows, rows[1:]) if b.date > a.date)
    back = sum(1 for a, b in zip(rows, rows[1:]) if b.date < a.date)
    if back > fwd:
        return list(reversed(rows)), True
    return rows, False


def continuity_score(rows: List[RawTxn]) -> Tuple[int, int]:
    """(pairs that satisfy prev ± amount = balance, pairs checkable)."""
    ok = checked = 0
    for a, b in zip(rows, rows[1:]):
        if a.balance_paise is None or b.balance_paise is None or b.direction is None:
            continue
        checked += 1
        sign = 1 if b.direction == CREDIT else -1
        if a.balance_paise + sign * b.amount_paise == b.balance_paise:
            ok += 1
    return ok, checked


def extract_pdf(path: str, password: Optional[str] = None) -> StatementExtract:
    import pdfplumber

    with pdfplumber.open(path, password=password or None) as pdf:
        n = len(pdf.pages)
        first = pdf.pages[0].extract_text(layout=True) or ""
        last = pdf.pages[-1].extract_text(layout=True) or ""
        meta = detect_meta(first, last)
        meta.account_holder = detect_holder(first)

        candidates = []
        for name, fn in (("table", _table_strategy), ("lines", _lines_strategy)):
            try:
                rows, opening, warns = fn(pdf)
            except Exception as exc:           # noqa: BLE001
                logger.warning("[consolidate] %s strategy failed on %s: %s", name, path, exc)
                continue
            rows, reversed_ = _chronological(rows)
            ok, checked = continuity_score(rows)
            candidates.append((ok, len(rows), name, rows, opening, warns, reversed_, checked))
            # A ruled table whose every balance step reconciles cannot be beaten;
            # skip the slower text pass (halves the time on long Axis reports).
            if name == "table" and len(rows) >= 2 and checked == len(rows) - 1 and ok == checked:
                break

    if not candidates:
        return StatementExtract(meta=meta, rows=[], page_count=n,
                                warnings=["No transactions could be extracted."])
    # Most rows that reconcile wins; ties go to more rows.
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    ok, count, name, rows, opening, warns, reversed_, checked = candidates[0]

    if opening is None:
        opening = meta.printed_opening_paise
    for w in list(warns):
        if w.startswith("__closing__="):
            if meta.printed_closing_paise is None:
                meta.printed_closing_paise = int(w.split("=", 1)[1])
            warns.remove(w)
    ex = StatementExtract(meta=meta, rows=rows, opening_paise=opening,
                          closing_paise=meta.printed_closing_paise, strategy=name,
                          page_count=n, warnings=list(warns))
    if reversed_:
        ex.warnings.append("Statement is printed newest-first; rows were put in date order.")
    return ex
