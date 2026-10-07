# Filter bar — what every screen promises (audit 2026-10-07)

The Entity / Bank Account / From / To bar narrows **one population**, and every
panel reads it the same way. `tests/test_filters_agree.py` asks every endpoint
the same question and requires the same answer.

| Rule | Where |
|---|---|
| Entity = the account's entity; a row's own copy only counts when its account has none (moving an account no longer counts its old rows under both entities) | `app/api/scoping.entity_scope` |
| Flow figures (counts, in/out, uncategorised, bank charges, risk alerts, net movement, burn) use the selected period | `/dashboard/summary` |
| Balances (Total Cash, Closing Position) are **as at the To date**, never a later balance | `/dashboard/summary`, `/analytics/cash-position`, `/reports/treasury` |
| Data age, unreconciled count/value and coverage use the selected entity/account | `/dashboard/summary` |
| Anomaly/violation tiles, panel headers and lists: same scope, same dates, same de-duplication | `app/api/scoping.scope_findings`, `/compliance/*` |
| Treasury report with an account selected contains only that account; trend, previous-period comparison, runway and forecast use the same scope | `/reports/treasury` |
| Reports page shows its Entity/Account filter on its own bar and uses its own Period, not the dashboard's hidden dates; exports cover the period on screen | `index.html` ReportsView |
| Dashboard "By month" bars are built from the dated daily series (no partial months outside the range) | `index.html` movement |
| Transactions Category filter = the label in the Category column; the dropdown lists exactly those labels | `display_category_label` |
| Malformed entity/account ids on exports are a 400, never "no filter" | `/reports/export/*` |

Ingestion fixes found during the audit (they changed every total):

- Bank PDFs go through the balance-verified extractor first (`TransactionParsingPipeline._parse_pdf_verified`); the legacy parser is the fallback. An Axis statement had 51 receipts stored as payments.
- A different running balance means a different transaction: the validator's UPI duplicate key and the dedup engine no longer drop same-day repeats.

Existing data parsed before this change keeps its old rows until the statement is re-parsed (`scripts/reparse_statements.py`) or re-uploaded.
