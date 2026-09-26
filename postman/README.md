# Testing the API with Postman

Everything here was run end to end before being written down, so the steps are
the ones that actually work rather than the ones that ought to.

## 1. Start the server

The admin routes are locked behind a token that has **no default** — if it is
unset, every admin route returns 503 rather than defaulting open. So set it
before starting.

**Windows PowerShell**, from the project folder:

```powershell
$env:B2B_ADMIN_TOKEN = "demo-admin-token-please-change-me"
python main.py
```

**cmd.exe:**

```cmd
set B2B_ADMIN_TOKEN=demo-admin-token-please-change-me
python main.py
```

The API is now at `http://localhost:8000`. The five API tables
(`api_clients`, `api_keys`, `analysis_requests`, `api_usage_records`,
`api_webhook_deliveries`) are created automatically on first start, because
`DB_AUTO_CREATE` defaults to true outside production. Nothing else to run.

Check it before opening Postman:

```
http://localhost:8000/v1/health
```

## 2. Import into Postman

`Import` → `File` → `TreasuryLens_API.postman_collection.json`.

Then open the collection's **Variables** tab and confirm:

| Variable | Value |
|---|---|
| `base_url` | `http://localhost:8000` |
| `admin_token` | the same string you exported in step 1 |
| `client_slug` | `creditlens` |
| `api_key` | *(leave blank — filled in for you)* |

## 3. Get a key (two clicks, in order)

Under **Admin (run these first)**:

1. **`1. Create client`** → expect `201`. Running it again returns `409`
   because the slug is taken; that is fine and means it already exists.
2. **`2. Issue API key`** → expect `201`. A test script on this request grabs
   the secret and writes it into the `{{api_key}}` collection variable
   automatically, so you never have to copy it by hand.

The secret is shown **once**. Only a SHA-256 of it is stored, so it cannot be
retrieved later — if you lose it, issue another key or rotate.

## 4. Analyse a statement

Open **Analyze → `Analyze — CSV / XLSX / PDF (basic)`**, go to
**Body → form-data**, click the **`file`** row and select a file. Postman
cannot store a file path inside a collection, so this is the one manual step
per request.

Ready-made samples are in `postman/samples/` — a six-month statement with
figures chosen so you can check the arithmetic by eye:

| File | What it proves |
|---|---|
| `sample_statement.csv` | the normal path |
| `sample_statement.tsv` | delimiter sniffing |
| `sample_statement.json` | structured input |
| `sample_statement.ofx` | a bank-native format |

All four contain the *same* statement, so all four must return
`total_credits = 468000` and 24 transactions. That is the real test of the
canonical-transaction design — if any parser leaked its own structure into the
analysis, the totals would diverge.

Expected from any of them:

```
total credits    468000.00     (6 × 78,000 salary)
total debits     273000.00     (6 × 45,500)
net flow         195000.00
salary           78000.00      source INFERRED, confidence ~0.97
proposed EMI     11122.22      (with the loan variant: 500000 @ 12% / 60m)
```

One deliberate difference: OFX returns `reconciliation_status:
NOT_VERIFIABLE` rather than `PASSED`. The OFX format carries no per-row running
balance, so continuity genuinely cannot be checked. It says so instead of
claiming a clean result — that distinction is the point.

## 5. What else is in the collection

- **Analyze — with loan**: adds `loan_amount` / `interest_rate` /
  `tenure_months` and returns affordability. All three or none; two of three is
  a `400` on purpose.
- **Analyze — async**: returns `202` immediately, then poll
  **Get result by request_id** (the `request_id` is saved for you).
- **Analyze — idempotent**: send it twice with the same file — the second
  response carries `Idempotent-Replay: true` and is not reprocessed. Send it
  with a *different* file and the same key and you get `409`.
- **Error cases**: five requests that must **not** return 200 — no key, bad
  key, `.zip`, partial loan params, unknown request id. Each has a test
  asserting the status and the error code.

Every request has test scripts, so **Run collection** gives you a pass/fail
report. The Analyze requests also log the key figures to the Postman console
(View → Show Postman Console), and one of them walks the entire response
asserting that no `INFERRED` metric ever claims a confidence of 1.0.

## Reading the response

No figure is a bare number. Each one says where it came from:

```json
"salary": {
  "value": 78000.0,
  "source": "INFERRED",
  "method": "RECURRING_CREDIT_PATTERN",
  "confidence": 0.97
}
```

- `EXTRACTED` — printed on the statement.
- `CALCULATED` — arithmetic over extracted values.
- `INFERRED` — a judgement. Always has a `method` and a confidence below 1.0.

A metric that could not be computed returns `"value": null` with a `note`,
never `0`. Do not let a client default that to zero.

## If something goes wrong

| Symptom | Cause |
|---|---|
| 503 on every admin route | `B2B_ADMIN_TOKEN` was not set before `python main.py` |
| 401 `MISSING_API_KEY` | `{{api_key}}` is empty — run `2. Issue API key` |
| 401 `INVALID_API_KEY` | the key was issued against a different database |
| 422 `NO_TRANSACTIONS_FOUND` | the file parsed but no rows matched a statement layout |
| 415 | the format is not supported — `GET /v1/formats` lists what is |
| Connection refused | the server is not running, or is on a different port |

Every error response carries a `request_id`. That same string is in the server
log, so it is the one thing worth quoting when something is unclear.

---

## Running it headlessly (Newman)

Postman ships a CLI runner, `newman`, which executes the same collection with
no GUI. Use it for a repeatable check or in CI.

```bash
npm install -g newman
newman run postman/TreasuryLens_API.newman.json --working-dir postman
```

`TreasuryLens_API.newman.json` is the same collection with one difference: the
`file` form field carries a path. The GUI cannot store a file path in a
collection, so the interactive copy leaves it blank for you to pick; Newman
can, so the automated copy fills it in. Requests, tests and assertions are
otherwise identical.

Expect **19 requests, 17 assertions, 0 failures**. Run it twice — the second
run is the interesting one, because it exercises two paths the first cannot:
`1. Create client` returns `409` (the slug is taken), and the idempotent
request logs `replay: 'true'` instead of reprocessing the file.

Point it somewhere else with:

```bash
newman run postman/TreasuryLens_API.newman.json --working-dir postman \
  --env-var base_url=http://localhost:8000 \
  --env-var admin_token=your-real-admin-token
```
