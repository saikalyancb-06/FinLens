# FinLens API — reference and Credit Lens integration

Everything below was verified against the live service on 3 October 2026. The
service does **not** publish an OpenAPI spec (`/openapi.json`, `/docs` and
`/redoc` all return 404), so this file is the reference.

- **Base URL:** `https://finlens-d6mb.onrender.com`
- **Service name:** `financial-analysis-api`, version `1.0.0`
- **Auth:** `Authorization: Bearer <api key>` on every call

---

## 1. Endpoints

### `GET /v1/health`

No authentication required. Returns the service status.

```json
{
  "status": "healthy",
  "service": "financial-analysis-api",
  "version": "1.0.0",
  "timestamp": "2026-10-03T07:20:43.107966+00:00"
}
```

Call this first after any idle period. The service runs on a free hosting tier
that suspends idle containers, so the first request after a sleep can take 30–60
seconds to answer. Credit Lens does this automatically before every upload.

### `POST /v1/statements/consolidate`

The endpoint Credit Lens uses. Accepts `multipart/form-data`.

| Field | Type | Required | Notes |
|---|---|---|---|
| `files` | file | yes | Repeat the field for multiple statements, or send one ZIP |
| `pdf_password` | text | no | Tried on every locked PDF |
| `passwords` | text (JSON) | no | Per-file passwords: `{"hdfc.pdf":"ABCD1234"}` |
| `account_numbers` | text (JSON) | no | `{"file.csv":"50100123456789"}` for files that print no account number |
| `async_mode` | text | no | `true` returns `202` + `request_id` at once; poll `GET /v1/statements/consolidate/{request_id}` |

One line, works in both PowerShell and cmd:

```
curl.exe -X POST "https://finlens-d6mb.onrender.com/v1/statements/consolidate" -H "Authorization: Bearer kl_live_..." -F "files=@statement.pdf" -o result.json --max-time 300
```

### Other endpoints the key can call

`GET /v1/statements/consolidate/{request_id}` (fetch a result again, kept 24 h),
`POST /v1/analyze` (income / EMI / affordability summary of one statement),
`GET /v1/usage`, `GET /v1/formats`. `POST /v1/classify` needs a key with the
`classify:write` scope, which this key does not have. Credit Lens uses none of
these.

The other routes on this host (`/v1/bank-master/…`, `/v1/review-queue/…`, etc.)
belong to the Treasury Lens web app, use its own login, and return
`401 {"detail":"Invalid token"}` for an API key. They are not part of this API.

---

## 2. Response shape

A successful call returns HTTP 200:

```json
{
  "request_id": "req_4887454a32b340029c7fde2d60225195",
  "status": "completed",
  "created_at": "2026-10-03T07:23:26.975642+00:00",
  "completed_at": "2026-10-03T07:23:27.125126+00:00",
  "duration_ms": 137,
  "data": {
    "schema_version": "1.0",
    "summary": {
      "files_received": 1, "files_processed": 1, "files_failed": 0,
      "accounts": 1, "transactions_extracted": 6, "duplicates_removed": 0,
      "transactions_output": 6, "internal_transfers": 0, "flags": 0,
      "balance_check": "PASSED", "duration_ms": 136
    },
    "accounts": [ /* one per detected account, with period and totals */ ],
    "files":    [ /* per-file status */ ],
    "statements": [ /* per-statement extraction detail and balance checks */ ],
    "transactions": [
      {
        "bank_account_no": "UNKNOWN-1",
        "bank_name": null,
        "date": "2026-09-20",
        "narration": "SALARY CREDIT",
        "amount": 65000.0,
        "type": "Money In",
        "category_1": "Salary Received",
        "category_2": "No",
        "balance": null,
        "source_files": ["sample_statement.csv"],
        "flags": []
      }
    ]
  }
}
```

Two things worth knowing. `bank_account_no` comes back as `UNKNOWN-1` when the
statement header gives the parser nothing to work with — Credit Lens substitutes
the account number you typed on the form in that case. And there are two balance
results. `summary.balance_check` is `PASSED` or `FAILED` — `FAILED` means at
least one break in the running balance (listed in `data.flags` with date and
difference). Per statement, `data.statements[].status` is `PASSED`, `FAILED` or
`NOT_VERIFIABLE`; the last means the file had no printed opening/closing balance
to check against, which is normal for CSV exports. So a CSV with no balance
column shows `PASSED` in the summary only because nothing contradicted it — read
`statements[].status` to tell the two apart.

### Errors

Every failure uses the same envelope:

```json
{
  "error": {
    "code": "PARSE_FAILED",
    "message": "None of the files could be processed: no transaction header row was found; expected columns such as date, description and debit/credit or amount",
    "request_id": "req_1888806933794a75baae96254d4ad363",
    "detail": { "files": [ { "file_name": "x.pdf", "status": "failed", "error": { } } ] }
  }
}
```

| Status | Code | Meaning |
|---|---|---|
| 401 | `MISSING_API_KEY` | No `Authorization` header |
| 401 | `INVALID_API_KEY` / `REVOKED_API_KEY` | Key rejected |
| 405 | — | Wrong HTTP method (`GET` on consolidate) |
| 413 | `FILE_TOO_LARGE` | File or batch over the size limit |
| 415 | `UNSUPPORTED_FILE_FORMAT` | Not a statement format (PDF, CSV, XLS/XLSX, OFX, JSON, ZIP) |
| 422 | `NO_TRANSACTIONS_FOUND` | File was read but has no transaction rows — scanned/image PDF, or not a statement |
| 422 | `PARSE_FAILED` | CSV/Excel without a recognisable header row |
| 422 | `PDF_PASSWORD_REQUIRED` / `PDF_PASSWORD_INVALID` | Locked PDF: send `pdf_password` |
| 429 | `RATE_LIMIT_EXCEEDED` / `QUOTA_EXCEEDED` | Wait `Retry-After` seconds |
| 503 | `SERVICE_UNAVAILABLE` | Server busy with other jobs; retry after `Retry-After` |

---

## 3. How Credit Lens connects to it

### Field mapping

The API's transaction schema lines up almost exactly with the `transactions`
table, which is why the integration is thin:

| API field | Credit Lens column | Handling |
|---|---|---|
| `bank_account_no` | `bank_account_no` | Falls back to the form value when `UNKNOWN*` |
| `bank_name` | `bank_name` | Falls back to the form value when null |
| `date` | `tx_date` | Already `YYYY-MM-DD` |
| `narration` | `narration` | — |
| `amount` | `amount` | Absolute value, rounded to 2dp |
| `type` | `tx_type` | Already matches `ENUM('Money In','Money Out')` |
| `category_1` | `category1` | Defaults to `Uncategorized` |
| `category_2` | `category2` | Defaults to `No` |

Rows whose `type` is neither `Money In` nor `Money Out` are dropped and counted
as skipped, because MySQL would reject them against the ENUM.

### Files

- **`finlens.php`** — the client. `finlens_import()` is the one function you
  need: it wakes the server, uploads, maps the response and returns
  `[rows, skipped, summary]` in the same shape `rows_to_transactions()` returns.
- **`index.php`** — the upload handler picks a route: PDF and ZIP always go to
  the API, everything else is parsed locally unless you tick the box.

### Configuration

Set the key as an environment variable so it stays out of your files:

```
setx FINLENS_API_KEY "kl_live_..."
```

Restart Apache afterwards. Failing that, edit the fallback in `finlens.php`.
Setting `FINLENS_API_KEY` to an empty string disables API uploads entirely and
the form reverts to local parsing only.

### Requirements

The PHP cURL extension must be enabled. XAMPP ships it on by default; if you see
"the PHP cURL extension is not enabled", uncomment `extension=curl` in
`php.ini` and restart Apache.

---

## 4. Caveats

The free tier sleeps, so an upload after an idle period waits for a cold start —
`finlens_import()` allows up to 90 seconds for this, then up to 300 seconds for
the upload, and raises PHP's execution limit to cover both, since XAMPP's
default 30 seconds would otherwise kill the request mid-upload. The free server
has a fraction of one CPU: a single statement takes from a few seconds to about
a minute.

The key lives in a file served by your web server. `finlens.php` is never
requested directly so PHP executes rather than prints it, but a server
misconfiguration that stops parsing PHP would expose it. Use the environment
variable on anything public, and rotate the key if it leaks.

Extracted data is not verified. `balance_check` tells you whether the totals
reconcile against printed balances, and it is worth reading before trusting a
statement — but categories in particular are a best guess and should be reviewed
in the dashboard before any decision rests on them.
