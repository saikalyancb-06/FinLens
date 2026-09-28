# Statement API — quick start for callers

Send bank statements, get back one JSON record per transaction: duplicates
removed, internal transfers tagged, running balances checked.
Full reference: [B2B_API.md](B2B_API.md) §10 (consolidate) and
[CLASSIFY_API.md](CLASSIFY_API.md) (your own rules).

## What you need

| | Value |
|---|---|
| Base URL | `https://<service>.onrender.com` (you get this from the operator) |
| API key | `kl_live_…` (you get this from the operator; keep it private) |

The key goes in every request as `Authorization: Bearer <key>`.

## 1. Check the service is awake

Open `https://<service>.onrender.com/v1/health` in a browser. Wait for
`"status":"healthy"`. On the free test server the first call after 15 minutes
of no traffic takes about a minute while it wakes up.

## 2. Send statements

Run from the folder that holds the file.

**Windows (PowerShell)**
```powershell
curl.exe -X POST "https://<service>.onrender.com/v1/statements/consolidate" -H "Authorization: Bearer <key>" -F "files=@statement.pdf" -o result.json --max-time 300
```

**Mac / Linux**
```bash
curl -X POST "https://<service>.onrender.com/v1/statements/consolidate" -H "Authorization: Bearer <key>" -F "files=@statement.pdf" -o result.json --max-time 300
```

Options (add as extra `-F` fields):

| Field | Example | Use |
|---|---|---|
| `files` (repeat) | `-F "files=@hdfc.pdf" -F "files=@sbi.pdf"` | several statements in one call |
| `files` (ZIP) | `-F "files=@statements.zip"` | a ZIP of statements |
| `pdf_password` | `-F "pdf_password=ABCD1234"` | tried on every locked PDF |
| `passwords` | `-F 'passwords={"hdfc.pdf":"ABCD1234"}'` | per-file passwords |
| `rules` | `-F "rules=@rules.json"` | your own categorisation rules ([CLASSIFY_API.md](CLASSIFY_API.md)) |
| `async_mode` | `-F "async_mode=true"` | big batches: returns `request_id` at once |

Async: poll until `"status"` is `"completed"`:
```bash
curl "https://<service>.onrender.com/v1/statements/consolidate/<request_id>" -H "Authorization: Bearer <key>" -o result.json
```

Windows note: PowerShell treats `$` as a variable, so paste the key itself, not
`$kl_live_…`. Special characters in file names: wrap the whole `files=@…` part
in double quotes.

## 3. Read the result

```jsonc
{
  "request_id": "…",
  "status": "completed",
  "data": {
    "summary": { "files_processed": 1, "transactions_output": 412,
                 "duplicates_removed": 0, "internal_transfers": 0,
                 "balance_check": "PASSED", "flags": 0 },
    "transactions": [
      { "bank_account_no": "50100123456789",
        "bank_name": "HDFC Bank",
        "date": "2025-04-05",
        "narration": "ACH D- BAJAJ FINANCE LTD-…",
        "amount": 12500.0,
        "type": "Money Out",            // or "Money In"
        "category_1": "EMI",
        "category_2": "Bajaj Finance",  // "No" when there is nothing extra
        "balance": 84211.35,
        "source_files": ["statement.pdf"],
        "flags": [] }
    ],
    "flags": [],        // balance breaks: type, account, date, difference
    "statements": [],   // per statement: opening / closing check
    "accounts": [], "files": [], "duplicates_removed": [], "internal_transfers": []
  },
  "quality": { "balance_check": "PASSED", "flags": 0, "duplicates_removed": 0, "files_failed": 0 }
}
```

The first eight fields of each transaction are the required output. `balance`,
`source_files` and `flags` are supporting detail.

## Errors

Every error is `{"error": {"code": "…", "message": "…"}}` with the HTTP status.

| Code | Meaning |
|---|---|
| `401 MISSING_API_KEY` / `INVALID_API_KEY` | key missing or wrong |
| `422 PDF_PASSWORD_REQUIRED` / `PDF_PASSWORD_INVALID` | send `pdf_password` |
| `422 NO_TRANSACTIONS_FOUND` | layout not recognised; send the file to the operator |
| `429 RATE_LIMIT_EXCEEDED` / `QUOTA_EXCEEDED` | wait `Retry-After` seconds |
| `503 SERVICE_UNAVAILABLE` | server busy; retry after `Retry-After` |

Quote the `X-Request-Id` response header when reporting a problem.
