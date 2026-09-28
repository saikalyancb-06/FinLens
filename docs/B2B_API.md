# Financial Analysis API — integration guide

Version `1.0.0`. Base path `/v1`. Everything below is served by the existing
application process; run it locally with `python main.py` and the API is at
`http://localhost:8000/v1`.

A client sends a bank statement file and receives structured financial analysis.
They never implement PDF, Excel or CSV parsing, and never implement the
financial calculations.

---

## 1. Authentication

Every endpoint except `/v1/health`, `/v1/ready`, `/v1/version` and `/v1/formats`
requires an API key:

```
Authorization: Bearer kl_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Keys are issued through the internal admin router, which is protected by a
separate shared token in `B2B_ADMIN_TOKEN` (if that variable is unset the whole
admin router refuses every request — it never defaults open):

```bash
export B2B_ADMIN_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))")

# create a client
curl -X POST http://localhost:8000/internal/clients \
  -H "X-Admin-Token: $B2B_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Credit Lens","slug":"creditlens","contact_email":"dev@creditlens.example"}'

# issue a key — the secret is shown ONCE and never again
curl -X POST http://localhost:8000/internal/clients/creditlens/keys \
  -H "X-Admin-Token: $B2B_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"production"}'
```

Only a SHA-256 of the secret is stored, so a database disclosure does not hand
over working credentials. Keys support revocation and rotation; a rotated key
leaves the old one valid until you explicitly revoke it, so an integrator can
deploy the new one without a flap of 401s.

Scopes: `analyze:write` (submit) and `analyze:read` (poll, usage).

---

## 2. `POST /v1/analyze`

`multipart/form-data`.

| Field | Required | Notes |
|---|---|---|
| `file` | yes | The statement. |
| `country` | no | ISO country, default `IN`. |
| `currency` | no | ISO currency, default `INR`. |
| `loan_amount` | no | With the next two, returns affordability. |
| `interest_rate` | no | Annual %, e.g. `12`. |
| `tenure_months` | no | Integer months. |
| `pdf_password` | no | For encrypted PDFs. |
| `include_transactions` | no | `false` omits the row list. |
| `async_mode` | no | `true` returns `202` immediately. |

Optional header `Idempotency-Key: <your-unique-string>`.

**Loan parameters are all-or-nothing.** Sending one or two of the three is a
`400`, not a default — guessing a tenure would produce an EMI and a
debt-to-income figure that look computed but rest on a number nobody supplied.

### Response

```json
{
  "request_id": "req_5f3c1a...",
  "status": "completed",
  "data": {
    "statement": {...}, "transactions": [...],
    "income": {...}, "expenses": {...}, "balances": {...},
    "cashflow": {...}, "debt": {...}, "loan": {...},
    "affordability": {...}, "risk": {...}, "financial_metrics": {...}
  },
  "quality": {
    "overall_confidence": 0.91,
    "reconciliation_status": "PASSED",
    "warnings": [...]
  },
  "metadata": {"country":"IN","currency":"INR","detected_format":"csv", ...}
}
```

### Every figure declares its provenance

This is the most important thing to understand about the response. No metric is
a bare number:

```json
"salary": {
  "value": 78000.0,
  "source": "INFERRED",
  "method": "RECURRING_CREDIT_PATTERN",
  "confidence": 0.94,
  "unit": "INR",
  "evidence": {"occurrences": 6, "cadence": "monthly"}
}
```

- `EXTRACTED` — printed on the statement and read off it.
- `CALCULATED` — arithmetic over extracted values. No judgement.
- `INFERRED` — a judgement about what the data means. **Always** carries a
  `method` and a `confidence` below 1.0.

A metric that could not be computed returns `{"value": null, "note": "..."}`
rather than `0`. Do not default a null to zero: a missing salary figure read as
₹0 is how a lending decision gets made on a number nobody produced.

`reconciliation_status` is `PASSED`, `FAILED`, or `NOT_VERIFIABLE` — the last
means the file carried no running-balance column, so continuity could not be
checked at all. It is not the same as passing.

---

## 3. Supported formats

Call `GET /v1/formats` for the live list with per-format caveats.

| Format | Extensions | Notes |
|---|---|---|
| PDF | `.pdf` | Digital and password-protected. Scanned PDFs need `tesseract` + `poppler` installed. |
| XLSX | `.xlsx`, `.xlsm` | Largest sheet only. |
| XLS | `.xls` | Legacy binary; needs `xlrd`. |
| CSV | `.csv` | 5 encodings, quoted fields. |
| TSV | `.tsv`, `.tab` | Delimiter sniffed. |
| TXT | `.txt`, `.dat` | Delimited text only. |
| JSON | `.json` | List, `{"transactions":[]}`, or `{"data":{"transactions":[]}}`. |
| OFX / QFX | `.ofx`, `.qfx` | No running balance in the format, so continuity is always `NOT_VERIFIABLE`. |
| CAMT.053 | `.xml` | ISO 20022. Any `camt.053.001.xx` version. |

**Not supported, and why:** ZIP and other archives (one statement per request
keeps errors, warnings and billing unambiguous); generic XML that is not
CAMT.053, including camt.052/054 and pain.* (XML without a schema is not a
format); images; `.doc`/`.docx`; OFX investment statements.

The format is detected from **content**, not the filename. A CSV uploaded as
`statement.pdf` is parsed as a CSV.

---

## 4. Errors

```json
{"error": {"code": "UNSUPPORTED_FILE_FORMAT",
           "message": "...", "request_id": "req_..."}}
```

Switch on `code`, never on the message. Never on the status line alone.

| Status | Codes |
|---|---|
| 400 | `MISSING_FILE`, `INVALID_PARAMETER`, `MALFORMED_REQUEST` |
| 401 | `MISSING_API_KEY`, `INVALID_API_KEY`, `REVOKED_API_KEY`, `EXPIRED_API_KEY` |
| 403 | `INSUFFICIENT_SCOPE`, `CLIENT_DISABLED` |
| 404 | `REQUEST_NOT_FOUND` |
| 409 | `IDEMPOTENCY_KEY_REUSED`, `REQUEST_IN_PROGRESS`, `RESOURCE_ALREADY_EXISTS` |
| 413 | `FILE_TOO_LARGE` |
| 415 | `UNSUPPORTED_FILE_FORMAT` |
| 422 | `FILE_CORRUPT`, `FILE_EMPTY`, `PARSE_FAILED`, `NO_TRANSACTIONS_FOUND`, `PDF_PASSWORD_REQUIRED`, `PDF_PASSWORD_INVALID`, `ANALYSIS_FAILED` |
| 429 | `RATE_LIMIT_EXCEEDED` (per-minute), `QUOTA_EXCEEDED` (day/month) |
| 500 | `INTERNAL_ERROR` |
| 503 | `SERVICE_UNAVAILABLE` |

Responses never contain a stack trace, a file path, or a library name. Quote the
`request_id` in a support ticket — it appears in our logs, the usage record and
any webhook for the same request.

---

## 5. Rate limits, idempotency, async

**Limits** are per client across three windows, returned on every response:

```
X-RateLimit-Limit-Minute / -Day / -Month
X-RateLimit-Remaining-Minute / -Day / -Month
X-RateLimit-Reset-Minute / -Day / -Month
Retry-After            (429 only)
```

The unsuffixed `X-RateLimit-Remaining` tracks whichever window has least left.

**Idempotency**: send `Idempotency-Key`. The same key with the same file
replays the first result (`Idempotent-Replay: true`) without reprocessing. The
same key with a *different* file is a `409` — that is a client bug and is
deliberately loud.

**Async**: `async_mode=true` returns `202` with a `request_id`, then poll
`GET /v1/analyze/{request_id}` until `status` is `completed` or `failed`. A
result past its retention window returns `410`.

**Webhooks**: set a `webhook_url` and `webhook_secret` on the client and
completions POST to it:

```
X-Kredo-Event-Id:   evt_...
X-Kredo-Signature:  t=<unix>,v1=<hmac-sha256 hex of "{t}.{raw body}">
```

Verify with a constant-time compare and reject timestamps more than 300s old.
`app/b2b/webhooks.py::verify_signature` is the reference implementation.

---

## 6. Examples

### cURL

```bash
curl -X POST http://localhost:8000/v1/analyze \
  -H "Authorization: Bearer $API_KEY" \
  -F "file=@statement.pdf" \
  -F "country=IN" -F "currency=INR" \
  -F "loan_amount=500000" -F "interest_rate=12" -F "tenure_months=60"

curl -X POST http://localhost:8000/v1/analyze \
  -H "Authorization: Bearer $API_KEY" -F "file=@statement.xlsx"

curl -X POST http://localhost:8000/v1/analyze \
  -H "Authorization: Bearer $API_KEY" -F "file=@statement.csv"
```

### Python

```python
import requests

API = "http://localhost:8000/v1"
KEY = "kl_live_..."

with open("statement.pdf", "rb") as fh:
    r = requests.post(
        f"{API}/analyze",
        headers={"Authorization": f"Bearer {KEY}",
                 "Idempotency-Key": "application-8813"},
        files={"file": ("statement.pdf", fh, "application/pdf")},
        data={"country": "IN", "currency": "INR",
              "loan_amount": 500000, "interest_rate": 12, "tenure_months": 60},
        timeout=120,
    )

if r.status_code != 200:
    err = r.json()["error"]
    raise RuntimeError(f"{err['code']}: {err['message']} ({err['request_id']})")

body = r.json()
salary = body["data"]["income"]["salary"]

# Read the provenance before you use the number.
if salary["value"] is None:
    print("No salary could be determined")
elif salary["source"] == "INFERRED" and salary["confidence"] < 0.7:
    print(f"Low-confidence salary {salary['value']} — route to manual review")
else:
    print(f"Monthly salary {salary['value']} (conf {salary['confidence']})")

if body["quality"]["reconciliation_status"] != "PASSED":
    print("Balances did not reconcile:", body["quality"]["warnings"])
```

### Node.js

```javascript
import fs from "node:fs";

const API = "http://localhost:8000/v1";
const KEY = process.env.API_KEY;

const form = new FormData();
form.append("file", new Blob([fs.readFileSync("statement.csv")]), "statement.csv");
form.append("country", "IN");
form.append("currency", "INR");

const res = await fetch(`${API}/analyze`, {
  method: "POST",
  headers: { Authorization: `Bearer ${KEY}`, "Idempotency-Key": "application-8813" },
  body: form,
});

if (!res.ok) {
  const { error } = await res.json();
  throw new Error(`${error.code}: ${error.message} (${error.request_id})`);
}

const body = await res.json();
console.log("Monthly income:", body.data.income.monthly_income.value);
console.log("Detected EMIs:", body.data.debt.detected_emis.value);
console.log("Confidence:", body.quality.overall_confidence);
```

---

## 7. Versioning

`/v1` is stable. Within it we will add fields and add new metrics, but we will
not remove a field, rename one, change a type, or change the meaning of an
existing value. Treat unknown fields as additive and ignore them. A breaking
change gets `/v2`.

`GET /v1/version` returns the API version and the `ruleset_version` — the latter
changes when classification or detection rules change, which can move an
`INFERRED` value without any API change.

---

## 8. Known limitations

- Scanned PDFs need `tesseract` and `poppler` installed; without them an
  image-only PDF yields no transactions rather than a partial result.
- `.xls` requires `xlrd`; the request is refused cleanly if it is absent.
- XLSX reads only the sheet with the most rows.
- OFX/QFX carry no running balance, so balance metrics and continuity are
  unavailable for them by construction.
- Multi-currency statements are parsed and flagged, but the aggregate figures
  do not convert between currencies — they are computed in the booked currency.
- Categorisation confidence depends on the ML artifact being present; without it
  the service falls back to rules only and says so via a `CLASSIFIER_DEGRADED`
  warning.
- Async processing runs in-process. A restart loses queued work. A request
  still `processing` after 30 minutes (`B2B_STUCK_REQUEST_MINUTES`) is marked
  `failed` with `SERVICE_UNAVAILABLE` and its `Idempotency-Key` is released, so
  retrying with the same key starts a fresh analysis.

## 9. Housekeeping (runs inside the server process)

`app/b2b/maintenance.py`, every 60 s (`B2B_MAINTENANCE_INTERVAL_SECONDS`):

- **Retention.** Once `result_expires_at` passes (client's
  `result_retention_hours`, default 24), the stored analysis — which contains
  every transaction of the statement — is deleted. The request row, usage record
  and idempotency fingerprint remain. `GET /v1/analyze/{id}` and an idempotent
  replay then return `410`.
- **Webhook retries.** A failed delivery is retried with backoff (30 s, 1 m,
  2 m, … up to 6 attempts); 4xx other than 429 is not retried.
- **Stuck requests.** See the async note above.

Set `B2B_MAINTENANCE_ENABLED=false` to turn the loop off (the test suite does).

---

## 10. `POST /v1/statements/consolidate` — many statements, one reconciled list

Built for the Credit Lens "bank statement → JSON" requirement. Send every
statement of a case in one request (several `files` fields, or one ZIP); get
back one record per unique transaction across all accounts, with duplicates
removed and balance breaks flagged.

`multipart/form-data`, scope `analyze:write`:

| Field | Notes |
|---|---|
| `files` | Repeat once per file. PDF, XLSX/XLS, CSV/TSV, JSON, OFX, CAMT.053 — or a `.zip` of them (folders inside are fine; non-statement files are reported, not fatal). Up to 25 uploads / 150 MB. |
| `passwords` | Optional JSON `{"file name": "password"}` for locked PDFs. |
| `pdf_password` | Optional password tried on every locked PDF. |
| `async_mode` | `true` → `202`, then `GET /v1/statements/consolidate/{request_id}`. Large batches (50+ pages) take 15–40 s synchronously. |
| `include_duplicates` | `false` omits the list of removed rows. |

`Idempotency-Key`, rate limits, error envelope and retention work exactly as on
`/v1/analyze`.

### One record per transaction (`data.transactions[]`)

```json
{
  "bank_account_no": "922020061147994",
  "bank_name": "Axis Bank",
  "date": "2025-08-05",
  "narration": "ACH/DR/DEUTSCHE BANK/350041556770019/UTIB00000000",
  "amount": 87754.0,
  "type": "Money Out",
  "category_1": "EMI",
  "category_2": "Deutsche",
  "balance": 1269410.94,
  "source_files": ["Account_Statement_Report_12-08-2026_1240hrs.PDF"],
  "flags": []
}
```

The first eight keys are the requirement's eight fields, in its order. Dates are
ISO `YYYY-MM-DD`. `category_2` is `"No"` when there is nothing to add; for an
internal transfer it is the other account number; for an EMI, the lender; for a
transfer or payment, the counterparty.

Category 1 values: `EMI`, `Loan Deduction` (bank loan recovery, OD/CC
interest), `Loan Repayment`, `Loan Received`, `Internal Transfer`,
`Salary Received`, `Salary Paid`, `Food Expenses`, `Travel Expenses`,
`Shopping`, `Utility Bills`, `Medical Expenses`, `Education Expenses`,
`Rent Paid`/`Rent Received`, `Cash Withdrawal`, `Cash Deposit`, `Bank Charges`,
`Bounce Charges`, `Cheque/EMI Bounce`, `Payment Returned`, `Tax Payment`,
`Tax Refund`, `Statutory Payment` (ESIC/EPF/PT), `Interest Received`,
`Credit Card Payment`, `Insurance`, `Investment`, `Vendor Payment`,
`Business Receipt`, `Cheque Payment`/`Cheque Deposit`, `Transfer In`/`Transfer Out`,
`Other Debit`/`Other Credit`.

### Check 1 — duplicates and internal transfers

* Same account + date + amount + direction + balance, and the same narration
  (ignoring case/spacing, allowing one bank format to truncate the other) = one
  transaction. Each removed row is listed in `data.duplicates_removed` with the
  file it came from and the file whose copy was kept.
* Genuine same-day repeats differ in balance and are kept.
* A statement downloaded part-way through a day contributes only the rows it
  shares; the merged day has every row once, in the bank's order.
* A byte-identical file uploaded twice is used once (`DUPLICATE_FILE` flag).
* Money Out of one supplied account matched to Money In of another (same
  amount, within 3 days, with evidence: a shared UTR/UPI reference, the other
  account number, or the other account holder's name in the narration) is kept
  on both sides and tagged `Internal Transfer`, `category_2` = the other account.
  Equal amounts with no such evidence are not paired.

### Check 2 — balance reconciliation (`data.flags[]`)

| `type` | Meaning |
|---|---|
| `MISSING_TRANSACTIONS_BETWEEN_STATEMENTS` | The last balance of one statement and the next entry of the following statement do not follow (Example 4). `date`, `difference`, both file names. |
| `BALANCE_BREAK_WITHIN_STATEMENT` | Previous balance ± amount ≠ balance inside one statement: a row missed or misread. |
| `CLOSING_BALANCE_MISMATCH` | Opening + Σ entries ≠ the statement's printed closing balance (Example 5). |
| `OPENING_BALANCE_MISMATCH` | The printed opening balance does not lead to the first entry. |
| `DUPLICATE_FILE` | The same file was supplied twice. |

The affected transaction also carries the flag in its own `flags` array.
`data.statements[]` gives each statement's opening, totals, computed and stated
closing balance and `status` (`PASSED`/`FAILED`/`NOT_VERIFIABLE`);
`data.accounts[]` gives per-account period, totals and `balance_check`.

### Supported layouts (verified)

Checked end to end on the Credit Lens samples (Case 2 and Case 3, 15 PDFs,
4,424 rows) — every running balance in every statement below reconciles to the
paisa, and printed opening and closing balances match where the statement prints them:

| Bank | Layouts |
|---|---|
| Axis Bank | CA statement (Amount + DR/CR), CC/OD statement (Debit/Credit, negative balance), "Account Statement Report" |
| Indian Overseas Bank | Branch printout (fixed-width, no header), passbook-style mobile statement (newest first, Cr/Dr suffixes), net-banking statement, "Date(Value Date)" statement (newest first) |
| State Bank of India | Statement of Account (Brought Forward / Closing Balance) |
| Bank of Baroda | bob World statement (serial numbers, bilingual header) |

Other banks' ruled-table and text layouts go through the same two strategies
and are accepted when their balances reconcile; check the `statements[].status`
on a new layout before relying on it. CLI equivalent:
`python scripts/consolidate_statements.py <folder|zip|files> -o out.json`.
