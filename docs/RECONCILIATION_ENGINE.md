# Bank reconciliation (BRS) engine — rules

`app/services/reconciliation_engine.py` (engine `v3.0`). Rules agreed on
2026-10-06; every rule has a test in `tests/test_reconciliation_engine_v3.py`.

## The statement it produces

```
Book closing          = book opening + ledger movements dated inside the period
+ payments in books, not yet in bank      (unpresented_cheque)          ADD
- receipts in books, not yet in bank      (uncleared_deposit)           SUBTRACT
+ credits in bank, not in books           (interest_credit, direct_credit, ...)  ADD
- debits in bank, not in books            (bank_charge, standing_instruction, ...) SUBTRACT
+/- difference on a paired entry          (amount_difference)
+/- possible duplicate ledger row         (duplicate_ledger_credit/debit)
= Computed bank closing

Residual = bank closing (statement balance on period end) - computed bank closing
```

| Verdict | When |
|---|---|
| `reconciled_clean` | residual 0, no open items, no pending reviews |
| `reconciled_with_exceptions` | residual 0, but items or reviews remain |
| `unreconciled` | residual not 0, **or** no bank balance on file for the period |

## Book opening balance — never taken from the bank

In this order:

1. typed by the user (run request `book_opening`, or the import screen) — `manual`
2. the book closing of the previous run, when it ended the day before — `previous_run`
3. the ledger file's own `Opening Balance` row, rolled forward through the
   file's entries dated before the period — `ledger_file`
4. otherwise the run stops with `400` and header `X-Error-Code: BOOK_OPENING_REQUIRED`.

Amounts are rupees; negative for an overdraft. `run.book_opening_source`
records which one was used.

## Carry-forward

Everything still open at the end of a run (ledger items and bank items) joins
the next period's pools, so an unpresented cheque stays on the BRS — ageing —
until the bank entry that clears it is matched. The run records
`carried_from_run_id`.

## Matching, in order

Same direction always. Nothing is ever deleted: two identical ledger rows are
two rows.

| Pass | Kind | Rule |
|---|---|---|
| 0 | as decided | A pair a reviewer **confirmed** in any earlier run is re-applied; a pair a reviewer **rejected** is never proposed again. |
| A | auto | Same amount and a shared transaction ID — cheque/instrument no, UTR/RRN, voucher no — within **7 days**, or **90 days** when the shared ID is the ledger's cheque number. |
| B | auto | Same amount, same date. |
| C | auto | Same amount within 7 days, and the only candidate on **both** sides. |
| D | review | Shared transaction ID, different amount. Paired, and the difference becomes an `amount_difference` line, so the BRS still balances. |
| F | review | One bank entry = sum of 2–5 ledger entries (deposit slip), and one ledger entry = sum of 2–5 bank entries. Within 7 days; at most 30 candidates and 50,000 search steps per entry, so it cannot stall. |
| E | review | Same amount within 30 days, linked only by a shared party word (or nearest date within 7) — a suggestion, never an auto-match. |

What counts as a transaction ID: ledger instrument/voucher numbers, the bank
reference, and in either narration tokens of 6+ digits (cheque numbers,
UPI/IMPS RRNs) or 10–22 character alphanumerics with 6+ digits (NEFT/RTGS
UTRs). Not IDs: words and names, 8-digit dates, and the entry's own amount.

After matching, a ledger row left over that repeats a matched row (same date,
amount, direction) is listed as a **possible duplicate** for review.

## Reviewer decisions

`POST /v1/reconciliation/matches/{id}/confirm` and `/reject` rebuild the run's
bridge in place: items, totals, pending count and verdict are recomputed, and
categories a person set with `/items/{id}/classify` are kept.

## Ledger import (`POST /v1/reconciliation/imports/confirm`)

- `Opening Balance` / `Closing Balance` rows are recognised only when the row has
  no date — "Loan closing charges" on a dated row is an ordinary entry.
- A negative amount is moved to the other side (a negative receipt is a payment);
  the response lists those rows in `negative_amount_rows`.
- A row with amounts on both sides is ambiguous: the import is refused with the
  row numbers.
- Undated rows that are not balance rows are skipped and listed in `skipped_rows`.
- Identical rows import as separate entries.

## Constants

`AUTO_WINDOW_DAYS = 7`, `CHEQUE_WINDOW_DAYS = 90`, `SUGGEST_WINDOW_DAYS = 30`,
`GROUP_MAX_PARTS = 5`, at the top of the engine.
