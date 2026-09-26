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
- Async processing runs in-process. A restart loses queued work; the request row
  is left in `processing` rather than being silently marked complete.
