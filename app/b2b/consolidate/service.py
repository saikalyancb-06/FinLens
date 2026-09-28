"""Many statements in, one reconciled transaction list out.

    files ──extract──> per-statement rows ──group by account──> merge + de-duplicate
          ──> balance continuity + per-statement closing check ──> categorise
          ──> pair internal transfers across accounts ──> JSON

Stateless: nothing is written to the database. Used by
`POST /v1/statements/consolidate` and by `scripts/consolidate_statements.py`.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

from app.b2b.consolidate.categorize import NO, categorize
from app.b2b.consolidate.extract import CREDIT, DEBIT, RawTxn, StatementExtract, extract_pdf
from app.b2b.consolidate.metadata import StatementMeta, bank_from_filename
from app.b2b.consolidate.reconcile import (
    SourceStatement, Txn, continuity_flags, file_sha256, merge_account,
    pair_internal_transfers, statement_reconciliation,
)
from app.b2b.consolidate.tokens import fmt

logger = logging.getLogger("b2b.consolidate")

SCHEMA_VERSION = "1.0"


class StatementError(Exception):
    """A single file could not be read. Reported per file; the batch continues."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_pdf(path: str) -> bool:
    with open(path, "rb") as fh:
        return fh.read(5) == b"%PDF-"


def _extract_non_pdf(path: str, file_name: str) -> StatementExtract:
    """CSV / XLSX / JSON / OFX / CAMT through the existing B2B parsers."""
    from app.b2b.detect import detect_format
    from app.b2b.parsers.registry import get_parser
    from app.b2b.service import _path_matching_format

    with open(path, "rb") as fh:
        head = fh.read(8192)
    detected = detect_format(file_name, head, path)
    if not detected.is_supported:
        raise StatementError("UNSUPPORTED_FILE_FORMAT", f"'{detected.format}' files are not supported.")
    parsed = get_parser(detected.format)(_path_matching_format(path, detected))
    rows = []
    for t in parsed.transactions:
        if t.txn_date is None:
            continue
        rows.append(RawTxn(date=t.txn_date, narration=(t.narration_raw or t.narration_clean or "").strip(),
                           amount_paise=abs(int(t.amount_paise)),
                           direction=CREDIT if t.is_credit else DEBIT,
                           balance_paise=t.balance_paise, page=1, direction_source="column"))
    meta = StatementMeta()
    sm = parsed.statement_meta or {}
    meta.account_number = sm.get("account_number")
    meta.bank_name = sm.get("bank_name")
    return StatementExtract(meta=meta, rows=rows, strategy=f"parser:{detected.format}")


def extract_file(path: str, file_name: str, password: Optional[str] = None) -> StatementExtract:
    if os.path.getsize(path) == 0:
        raise StatementError("FILE_EMPTY", "The file is empty.")
    if _is_pdf(path):
        try:
            import fitz
            doc = fitz.open(path)
            locked = doc.needs_pass
            if locked and (not password or not doc.authenticate(password)):
                doc.close()
                raise StatementError("PDF_PASSWORD_REQUIRED" if not password else "PDF_PASSWORD_INVALID",
                                     "This PDF is password-protected; supply its password."
                                     if not password else "The password did not open this PDF.")
            doc.close()
        except StatementError:
            raise
        except Exception:  # noqa: BLE001
            raise StatementError("FILE_CORRUPT", "The PDF could not be opened.")
        ex = extract_pdf(path, password=password)
    else:
        from app.b2b.errors import ApiError
        try:
            ex = _extract_non_pdf(path, file_name)
        except ApiError as exc:
            raise StatementError(exc.code, exc.message)
    if not ex.rows:
        raise StatementError("NO_TRANSACTIONS_FOUND",
                             "The file was read but no transaction rows were recognised.")
    return ex


def consolidate(files: Sequence[Tuple[str, str]],
                passwords: Optional[Dict[str, str]] = None,
                default_password: Optional[str] = None,
                account_numbers: Optional[Dict[str, str]] = None,
                ruleset=None) -> dict:
    """`files` = [(path on disk, original file name)]. Returns the response `data`."""
    started = time.monotonic()
    passwords = passwords or {}
    # Caller-supplied account numbers, by file name: for formats that do not
    # print one (CSV/JSON exports) or statements that print it masked.
    account_numbers = {k: str(v).strip() for k, v in (account_numbers or {}).items() if v}
    statements: List[SourceStatement] = []
    file_results: List[dict] = []
    seen_hashes: Dict[str, str] = {}
    flags: List[dict] = []

    for idx, (path, name) in enumerate(files):
        sha = file_sha256(path)
        if sha in seen_hashes:
            file_results.append({"file_name": name, "status": "skipped_duplicate_file",
                                 "duplicate_of": seen_hashes[sha]})
            flags.append({"type": "DUPLICATE_FILE", "statement": [name],
                          "duplicate_of": seen_hashes[sha],
                          "message": f"'{name}' is byte-for-byte the same file as "
                                     f"'{seen_hashes[sha]}'; it was used once."})
            continue
        seen_hashes[sha] = name
        try:
            ex = extract_file(path, name, passwords.get(name) or default_password)
        except StatementError as exc:
            file_results.append({"file_name": name, "status": "failed",
                                 "error": {"code": exc.code, "message": exc.message}})
            continue
        except Exception:  # noqa: BLE001
            logger.exception("[consolidate] extraction crashed on %s", name)
            file_results.append({"file_name": name, "status": "failed",
                                 "error": {"code": "PARSE_FAILED",
                                           "message": "The file could not be parsed."}})
            continue
        acct = (account_numbers.get(name) or account_numbers.get(os.path.basename(name))
                or ex.meta.account_number or f"UNKNOWN-{idx + 1}")
        statements.append(SourceStatement(file_name=name, sha256=sha, extract=ex, account_no=acct,
                                          bank_name=ex.meta.bank_name, index=idx))
        file_results.append({"file_name": name, "status": "processed", "account_no": acct})

    return consolidate_statements(statements, file_results=file_results, flags=flags,
                                  files_received=len(files), started=started, ruleset=ruleset)


def consolidate_statements(statements: List[SourceStatement], *, file_results: Optional[List[dict]] = None,
                           flags: Optional[List[dict]] = None, files_received: Optional[int] = None,
                           started: Optional[float] = None, ruleset=None) -> dict:
    """The reconciliation half, on already-extracted statements (also used by tests)."""
    started = started if started is not None else time.monotonic()
    file_results = file_results if file_results is not None else [
        {"file_name": s.file_name, "status": "processed", "account_no": s.account_no} for s in statements]
    flags = flags if flags is not None else []
    files_received = files_received if files_received is not None else len(statements)

    # Bank name: the statement's own text first; otherwise another statement of
    # the same account in this batch; the file name only as a last resort.
    by_acct_bank = {s.account_no: s.bank_name for s in statements if s.bank_name}
    for s in statements:
        if not s.bank_name:
            s.bank_name = by_acct_bank.get(s.account_no) or bank_from_filename(s.file_name)
            s.extract.meta.bank_name = s.bank_name

    by_index = {s.index: s for s in statements}
    accounts: Dict[str, List[SourceStatement]] = {}
    for s in statements:
        accounts.setdefault(s.account_no, []).append(s)

    all_txns: List[Txn] = []
    duplicates: List[dict] = []
    reconciliations: List[dict] = []
    account_summaries: List[dict] = []
    uid = 0
    for acct, stmts in accounts.items():
        merged, dups, uid = merge_account(stmts, uid)
        duplicates.extend(dups)
        acct_flags = continuity_flags(acct, merged, by_index)
        flags.extend(acct_flags)
        for s in stmts:
            rec, f = statement_reconciliation(s)
            reconciliations.append(rec)
            if f:
                flags.append(f)
                if merged:
                    last = [t for t in merged if any(src[0] == s.index for src in t.sources)]
                    if last:
                        last[-1].flags.append({"type": f["type"], "difference": f["difference"]})
            opening_flag = _opening_flag(s)
            if opening_flag:
                flags.append(opening_flag)
        all_txns.extend(merged)
        holder = next((s.extract.meta.account_holder for s in stmts if s.extract.meta.account_holder), None)
        account_summaries.append({
            "account_no": acct,
            "bank_name": stmts[0].bank_name,
            "account_holder": holder,
            "statements": [s.file_name for s in sorted(stmts, key=lambda x: x.extract.rows[0].date)],
            "period_from": merged[0].row.date.isoformat() if merged else None,
            "period_to": merged[-1].row.date.isoformat() if merged else None,
            "transactions": len(merged),
            "duplicates_removed": len(dups),
            "opening_balance": fmt(_opening_of(merged)),
            "closing_balance": fmt(merged[-1].row.balance_paise) if merged else None,
            "total_money_in": fmt(sum(t.row.amount_paise for t in merged if t.row.direction == CREDIT)),
            "total_money_out": fmt(sum(t.row.amount_paise for t in merged if t.row.direction == DEBIT)),
            "balance_check": "PASSED" if not acct_flags else "FAILED",
            "balance_breaks": len(acct_flags),
        })

    for t in all_txns:
        c1, c2 = categorize(t.row.narration, t.row.direction or DEBIT, t.row.amount_paise)
        t.category_1, t.category_2 = c1, c2 or NO
    _salary_by_employer(all_txns)
    rules_report = _apply_caller_rules(all_txns, ruleset) if ruleset is not None else None
    holders = {a["account_no"]: a["account_holder"] for a in account_summaries}
    # Internal-transfer tagging runs LAST, after caller rules: the requirement
    # fixes that category ("Internal Transfer" + the other account number), and
    # a generic caller rule such as 'UPI -> Transfers' must not undo it.
    transfers = pair_internal_transfers(all_txns, holders)
    for t in all_txns:
        if t.category_1 == "Internal Transfer" and t.classification is not None:
            t.classification = {"method": "internal_transfer",
                                "explanation": "Matched to the opposite entry in account "
                                               f"{t.category_2}; takes precedence over caller rules.",
                                "overridden_rule_id": t.classification.get("rule_id")}

    all_txns.sort(key=lambda t: (t.account_no, 0))   # stable: keeps per-account order
    out_rows = [_row(t, by_index) for t in all_txns]
    return {
        "schema_version": SCHEMA_VERSION,
        "summary": {
            "files_received": files_received,
            "files_processed": sum(1 for f in file_results if f["status"] == "processed"),
            "files_failed": sum(1 for f in file_results if f["status"] == "failed"),
            "accounts": len(accounts),
            "transactions_extracted": sum(len(s.extract.rows) for s in statements),
            "duplicates_removed": len(duplicates),
            "transactions_output": len(out_rows),
            "internal_transfers": len(transfers),
            "flags": len(flags),
            "balance_check": "PASSED" if not any(f["type"] != "DUPLICATE_FILE" for f in flags) else "FAILED",
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
        "accounts": account_summaries,
        "files": file_results,
        "statements": reconciliations,
        "transactions": out_rows,
        "flags": flags,
        "duplicates_removed": duplicates,
        "internal_transfers": transfers,
        **({"rules": rules_report} if rules_report is not None else {}),
    }


class _RuleSubject:
    """The attributes app/b2b/rules.py reads off a transaction."""

    def __init__(self, t: Txn):
        self.narration_raw = t.row.narration
        self.narration_clean = t.row.narration
        self.amount_paise = t.row.amount_paise
        self.direction = "credit" if t.row.direction == CREDIT else "debit"
        self.txn_date = t.row.date


def _apply_caller_rules(txns: List[Txn], ruleset) -> dict:
    """Caller-supplied rules (same format as POST /v1/classify) set Category 1.

    A matching rule's `category` becomes Category 1; its `set.counterparty`, if
    any, becomes Category 2 (otherwise our detail stays). Rows no rule matched
    keep the built-in category, or take the ruleset's `default_category` when
    one is given. Every row records how it was decided.
    """
    from collections import Counter
    from app.b2b.rules import classify_one

    used: Counter = Counter()
    ambiguous = 0
    matched = 0
    unmatched_samples: List[str] = []
    for t in txns:
        d = classify_one(ruleset, _RuleSubject(t))
        if d.method == "rule":
            matched += 1
            used[d.rule_id] += 1
            ambiguous += int(d.ambiguous)
            t.category_1 = d.category
            cp = (d.assign or {}).get("counterparty")
            if cp:
                t.category_2 = cp
            t.classification = d.to_api()
        elif ruleset.default_category:
            t.category_1 = ruleset.default_category
            t.classification = {"method": "default", "category": ruleset.default_category,
                                "explanation": "No caller rule matched; default_category applied."}
        else:
            t.classification = {"method": "builtin", "category": t.category_1,
                                "explanation": "No caller rule matched; built-in category kept."}
            if len(unmatched_samples) < 25 and t.row.narration not in unmatched_samples:
                unmatched_samples.append(t.row.narration)
    return {
        "rule_count": len(ruleset.rules),
        "version": getattr(ruleset, "version", None),
        "transactions_matched": matched,
        "transactions_unmatched": len(txns) - matched,
        "ambiguous_count": ambiguous,
        "rule_usage": [{"rule_id": r.rule_id, "category": r.category, "matched": used.get(r.rule_id, 0)}
                       for r in ruleset.rules],
        "rules_that_never_matched": [r.rule_id for r in ruleset.rules if not used.get(r.rule_id)],
        "unmatched_samples": unmatched_samples,
    }


def _salary_by_employer(txns: List[Txn]) -> None:
    """Salary credits that name only the employer ('ULTISMART INFOTECH').

    The same account already carries credits literally narrated SALARY; a
    credit of the same amount (within 2%) that recurs at least twice is the
    same salary paid with the employer's name instead of the word.
    """
    from collections import defaultdict
    salary = defaultdict(list)
    for t in txns:
        if t.category_1 == "Salary Received":
            salary[t.account_no].append(t.row.amount_paise)
    if not salary:
        return
    groups = defaultdict(list)
    for t in txns:
        if (t.row.direction == CREDIT and t.account_no in salary
                and t.category_1 in ("Business Receipt", "Transfer In", "Other Credit")):
            groups[(t.account_no, t.category_2)].append(t)
    for (acct, payer), rows in groups.items():
        if payer in (None, NO) or len(rows) < 2:
            continue
        if all(any(abs(r.row.amount_paise - s) <= 0.02 * s for s in salary[acct]) for r in rows):
            for r in rows:
                r.category_1 = "Salary Received"


def _opening_of(merged: List[Txn]) -> Optional[int]:
    if not merged or merged[0].row.balance_paise is None or not merged[0].row.direction:
        return None
    r = merged[0].row
    return r.balance_paise - (r.amount_paise if r.direction == CREDIT else -r.amount_paise)


def _opening_flag(s: SourceStatement) -> Optional[dict]:
    """Printed opening balance vs the first row (the other half of Example 5)."""
    ex = s.extract
    if ex.opening_paise is None or not ex.rows:
        return None
    r = ex.rows[0]
    if r.balance_paise is None or not r.direction:
        return None
    expected = ex.opening_paise + (r.amount_paise if r.direction == CREDIT else -r.amount_paise)
    if expected == r.balance_paise:
        return None
    diff = r.balance_paise - expected
    return {"type": "OPENING_BALANCE_MISMATCH", "account_no": s.account_no, "date": r.date.isoformat(),
            "statement": [s.file_name], "opening_balance": fmt(ex.opening_paise),
            "expected_balance": fmt(expected), "actual_balance": fmt(r.balance_paise),
            "difference": fmt(diff),
            "message": f"The statement opens at {fmt(ex.opening_paise):,.2f} but its first entry does "
                       f"not follow from it (difference {fmt(diff):,.2f})."}


def _row(t: Txn, by_index: Dict[int, SourceStatement]) -> dict:
    r = t.row
    return {
        # --- the eight fields of the requirement, in its order
        "bank_account_no": t.account_no,
        "bank_name": t.bank_name,
        "date": r.date.isoformat(),
        "narration": r.narration,
        "amount": fmt(r.amount_paise),
        "type": "Money In" if r.direction == CREDIT else "Money Out",
        "category_1": t.category_1,
        "category_2": t.category_2 or NO,
        # --- supporting detail
        "balance": fmt(r.balance_paise),
        "source_files": sorted({by_index[s].file_name for s, _ in t.sources}),
        "flags": t.flags,
        **({"classification": t.classification} if t.classification is not None else {}),
    }
