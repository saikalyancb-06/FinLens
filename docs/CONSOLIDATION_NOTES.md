# Credit Lens multi-statement consolidation — build notes (2026-09-28)

## What was asked
Bank statements → one JSON record per transaction with 8 fields (account no,
bank, date, narration, amount, Money In/Out, Category 1, Category 2 or "No").
Check 1: remove duplicates across overlapping statements (Account + Date +
Narration + Amount + Balance); keep internal transfers on both sides tagged
"Internal Transfer" with the other account no. Check 2: per-account running
balance must flow across and within statements; flag breaks with date and
difference; check opening + entries against the printed closing balance.

## What was built
- `app/b2b/consolidate/` — extractor (ruled-table + text-line strategies, picked
  by balance continuity), metadata (bank via IFSC, account no, holder, period,
  printed opening/closing), reconcile (multiset de-dup, topological merge,
  continuity + statement checks, evidence-based transfer pairing), categorize
  (Indian-banking rules first, general classifier as fallback), service, safe ZIP.
- `POST /v1/statements/consolidate` (+ GET by request_id), multipart `files`
  (repeatable or one ZIP), `passwords`, `pdf_password`, `account_numbers`,
  `async_mode`, `include_duplicates`. Same auth/limits/idempotency as /v1/analyze.
- CLI `scripts/consolidate_statements.py <folder|zip|files> -o out.json`.
- Tests `tests/b2b/test_consolidate.py` — one per spec example, category rules,
  endpoint (dedupe, ZIP, errors), and the real Case 2/3 files when present.

## Results on the supplied samples
| | Case 2 | Case 3 |
|---|---|---|
| Files | 3 PDFs (+ CAM xlsx, reported as not a statement) | 12 PDFs (+ CAM xlsx) |
| Accounts | 3 Axis | 3 IOB, 1 SBI, 1 Bank of Baroda |
| Rows read → output | 2,787 → 2,787 | 1,637 → 1,261 |
| Duplicates removed | 0 | 376 (SBI 6-month inside 12-month: 226; IOB net-banking vs branch printout: 150) + 1 identical file |
| Internal transfers | 120 pairs | 19 pairs |
| Balance check | PASSED, every statement | PASSED, every statement |

The older single-statement parser (behind /v1/analyze) read these files
unreliably: 346 of 348 rows on Axis CA, 515 of 520 on Axis CC, 5 of 238 on IOB
net-banking, none on IOB 14099 or the IOB branch printout. The new extractor
reads every row and every running balance reconciles.

Real-data check of the gap flag: removing the Aug–Oct 2025 Bank of Baroda
statement produces `MISSING_TRANSACTIONS_BETWEEN_STATEMENTS` on 2025-11-01,
difference −439.42.

## Known limits
- New bank layouts: accepted when their balances reconcile; check
  `statements[].status` on the first file of any new bank.
- Narration text wrapped mid-word inside a table cell is joined with a space
  (e.g. `DOMINIO N ENTERPRISE`); amounts, dates and balances are unaffected.
- Int.Coll debits from a CA to the same borrower's CC account are tagged
  Internal Transfer (they are one); the interest nature is visible in the
  narration.
- /v1/analyze (single statement) still uses the older parser.
