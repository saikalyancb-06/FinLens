# Transaction storage architecture

**Decision:** `transactions` is the ledger. `processed_transactions` is a
classifier audit log. Nothing financial is ever read from the audit log.

## Why there were two tables

They are two generations of the same idea that were never reconciled:

| | `transactions` | `processed_transactions` |
|---|---|---|
| Role | **The ledger** | Classifier audit log |
| Money | `BigInteger` paise | `Numeric(12,2)` rupees |
| Scoped to | user + account + statement + entity | `file_id` only — **no account** |
| Category | FK to `categories` | denormalised strings |
| On re-parse | replaced per statement | replaced per file |
| Dedup | `superseded_by_id`, `hash` | none |

`processed_transactions` cannot be a ledger: it has no `account_id`, so its rows
cannot be attributed to a bank account, reconciled, or reported per entity. That
settles the direction of the decision.

## The rule

- Every figure shown to a user — balances, cash flow, reconciliation, reports,
  the dashboard — comes from `transactions`.
- `processed_transactions` records what the classifier saw and decided, for
  debugging and audit. It is never joined into a financial total.
- Divergence between them is a **defect**, not an expected state. Detect it with:

```bash
python -m scripts.check_ledger_consistency --strict
```

## Current state

At the time of writing: 8,153 ledger rows, 9,972 audit rows. The counts are not
expected to match — the audit log appends per parse while the ledger deduplicates
and is account-scoped.

The known divergence is **3,303 audit rows for 9 users with no ledger rows**.
Investigated and dispositioned:

| Users | Rows | Disposition |
|---|---|---|
| 8 × `e2e_val_*@example.com` | 3,288 | Automated test debris. Not real data. |
| `abc@gmail.com` | 15 | All zero-amount; `store_transactions` correctly skips rows with neither debit nor credit. |

**No real financial data is stranded, and nothing should be backfilled.** Copying
audit rows into the ledger would resurrect test debris and, worse, would require
inventing an `account_id` the audit row does not carry — misattributing money to
an arbitrary account. Recovery, if ever needed, is re-ingestion of the source
file with a resolvable user, not a table copy.

## Root cause of the divergence

`app/services/parsing_queue.py` writes the audit log first, then writes the
ledger **only if a `Statement` row exists**. A `Statement` is only created when
`target_user_id` resolves. When it does not, the audit write had already
succeeded and the ledger write was silently skipped — the parse reported success
while its money never entered the ledger.

Fixed: that branch now logs an error naming the file and row count, and sets
`ledger_write_skipped` on the job summary. The asymmetry can still occur, but it
can no longer occur *silently*.

## Migration status

`/analytics/recent-transactions` already reads from `transactions`; only its
response schema retains the legacy `ProcessedTransactionResponse` name. The
remaining references to `processed_transactions` are its writer, the two clear
endpoints, and the AA staging path.

## Known follow-ups

Found by audit, deliberately not changed in the same pass as the data migration:

- **`app/api/dashboard.py:291` is unreachable.** It registers
  `DELETE /transactions/clear`, which `app/api/transactions.py:334` also
  registers; `main.py` includes the transactions router first, so Starlette
  always dispatches that one. Because the OpenAPI dict is keyed by path, the
  published spec advertised the unreachable handler — documented behaviour and
  executed behaviour disagreed. Now marked `include_in_schema=False`; delete the
  handler in a follow-up.
- **AA audit rows carry `file_id = NULL`**, so the account-scoped branch of the
  clear endpoint — which reconstructs scope through
  `file_id → Statement.uploaded_file_id → account_id` — can never reach them.
- **`store_processed_transactions` deletes prior rows for a `file_id` before
  inserting.** Correct for a staging table, wrong for an audit log: reprocessing
  erases the previous classifier attempt instead of appending a second one.
- **`app/aa/service.py` discards the return value of `store_transactions`**, so
  the AA response reports audit-row counts rather than ledger-row counts. The two
  differ: the ledger skips zero-value rows.
