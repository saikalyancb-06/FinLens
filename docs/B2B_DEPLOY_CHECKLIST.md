# B2B API — pre-deploy verification and deploy checklist (2026-09-28)

## What the service is

One FastAPI process (`main.py`) serves two things:

1. **Treasury Lens** — the internal multi-tenant web app (UI at `/`, JWT auth).
2. **The B2B Financial Analysis API** — `/v1/*`, API-key auth. A client uploads
   one bank statement (PDF, XLSX/XLS, CSV/TSV/TXT, JSON, OFX/QFX, CAMT.053) and
   gets back every transaction **classified** (category path, confidence, flow,
   rail) plus income, expenses, balances, cash flow, debt, risk and optional loan
   affordability. Each figure says whether it was `EXTRACTED`, `CALCULATED` or
   `INFERRED`. Nothing from the statement is written to the transaction tables;
   only the API's own records (`api_clients`, `api_keys`, `analysis_requests`,
   `api_usage_records`, `api_webhook_deliveries`) are stored.

   Classification uses the product's built-in rules + ML model + category tree.
   **The API does not accept client-supplied rules** — there is no request field
   for them.

## What was verified

| Check | Result |
|---|---|
| Full test suite (Python 3.11, PostgreSQL 16) | 1,262 passed, 1 skipped; 2 fail only for missing Windows `KredoAgent.exe` / Playwright browser in the sandbox |
| B2B tests incl. new ones | 215 passed |
| Production Docker image built from `Dockerfile` | builds; runs as uid 10001; `/health` healthy; `/docs` 404 |
| Existing DB at revision 013 → `alembic upgrade head` in the image | creates the 5 API tables; schema identical to the models |
| `scripts/b2b_smoke_test.py` against the container | 86 / 86 checks pass |
| Postman collection via newman | 19 requests, 17 assertions, 0 failures |
| Webhook delivery, signature, retry after a 503 | signed, verified, retried and delivered 78 s later |

## Fixed before deploy

1. **Server would not start on a fresh build.** `requirements.txt` was unpinned;
   SQLAlchemy 2.1 maps `postgresql://` to psycopg v3, which is not installed.
   The driver is now pinned in `app/database/session.py`
   (`normalize_postgres_url`), and requirements are pinned to tested versions.
2. **No migration for the API tables.** With `DB_AUTO_CREATE=false`
   (production) every `/v1` and admin call would 500 on an existing database.
   Added `alembic/versions/014_b2b_api_tables.py` (idempotent).
3. **`B2B_ADMIN_TOKEN` missing from `render.yaml`** — no way to issue a key in
   production. Added (`generateValue: true`); also in `docker-compose.yml`.
4. **ML model was git-ignored** (`mlmodel/artifacts/`), so a Render build from
   git would ship without it and degrade every response. The two runtime files
   are now re-included in `.gitignore`.
5. **Password-protected PDFs** returned `NO_TRANSACTIONS_FOUND` instead of
   `PDF_PASSWORD_REQUIRED` / `PDF_PASSWORD_INVALID`; message named the wrong
   field. Fixed in `app/b2b/parsers/legacy.py`.
6. **Result retention was never enforced** — full statement data stayed in
   Postgres forever and was served after expiry. Now enforced on read and purged
   by `app/b2b/maintenance.py`.
7. **Webhook retries never ran** (nothing polled `due_deliveries`). Now retried.
8. **Stuck `processing` requests** blocked their idempotency key forever. Now
   reaped after 30 min and the key released.
9. **Missing dependencies**: `lxml` (HTML-disguised bank "Excel" exports parsed
   to 0 rows), `beautifulsoup4` (RBI FX rates). `bcrypt` held at 4.0.1 (5.x breaks
   passlib). `scikit-learn` = 1.9.0 to match the pickled model.
10. The per-IP rate limiter now answers `/v1` callers in the documented error
    shape (`RATE_LIMIT_EXCEEDED`, `request_id`, `Retry-After`); the poll URL in
    `REQUEST_IN_PROGRESS` pointed at a non-existent `/v1/requests/...`.

## Deploy steps (Render)

1. **Commit and push** everything above. Before pushing, run `git status` and
   confirm `mlmodel/artifacts/categorizer_model.joblib` and
   `model_metadata.json` are listed as tracked, and `.env` is **not**.
2. **Rotate secrets that sit in the local `.env`** if that file has ever been
   committed, shared or copied off the laptop (Google OAuth client secret, Setu
   AA client secret). It is ignored by git and Docker, but check history:
   `git log --all -- .env` should print nothing.
3. Render → **Blueprint sync** (or first-time: New → Blueprint). Fill the
   `sync: false` prompts (`ALLOWED_ORIGINS`, OAuth values can stay blank).
4. **Database**, in the service Shell:
   - brand-new database: `DB_AUTO_CREATE=true python -c "import main"` then
     `python -m alembic stamp head` → expect **44 tables** (incl. `alembic_version`), revision **014**;
   - database that already exists: `python -m alembic upgrade head` → **014**.
5. **Check**: `https://<app>/v1/health` → healthy; `/v1/ready` → database ok.
6. **Smoke test production** from your laptop (token from Render → Environment →
   `B2B_ADMIN_TOKEN`):
   ```bash
   python scripts/b2b_smoke_test.py --base-url https://<app>.onrender.com \
       --admin-token <B2B_ADMIN_TOKEN>
   ```
   Expect `86 passed, 0 failed`. It creates and then disables two `smoke-*`
   clients.
7. **Issue the real client's key** (shown once):
   ```bash
   curl -X POST https://<app>/internal/clients -H "X-Admin-Token: $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"name":"<Client>","slug":"<client>","contact_email":"...","rate_limit_per_minute":60}'
   curl -X POST https://<app>/internal/clients/<client>/keys -H "X-Admin-Token: $TOKEN" \
     -H "Content-Type: application/json" -d '{"name":"production"}'
   ```
   Send them `docs/B2B_API.md` and the key over a secure channel.

## Known limits (not blockers)

- `/internal/clients` is on the public host, protected only by the token. Keep
  the token long and private; put it behind an IP allow-list if Render plan
  allows.
- One instance only (in-process async + disk). Fine for launch volumes.
- Scanned PDFs use OCR (tesseract is in the image) — slower, lower confidence.
- A single IP is capped at `RATE_LIMIT_PER_MINUTE` (120) across all routes
  before plan limits apply; raise it if a client's plan needs more.
- The internal app's RPA/local-agent features need Playwright browsers, which
  the image does not install. Not used by the B2B API.
