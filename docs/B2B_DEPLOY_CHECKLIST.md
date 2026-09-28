# Deploying the B2B statement API (for Credit Lens) — checklist

Last verified 2026-09-28 against the Credit Lens sample set (Case 2 + Case 3,
15 bank PDFs, 4,424 rows), the full test suite and the production Docker image.

## What the service offers

One FastAPI process. External callers use API keys on `/v1`:

| Endpoint | Input | Output |
|---|---|---|
| `POST /v1/statements/consolidate` | several statements (or one ZIP), optional ruleset | one reconciled record per transaction (the 8 required fields), duplicates removed, internal transfers tagged, balance breaks flagged — docs/B2B_API.md §10 |
| `POST /v1/classify` | one statement + ruleset | every row classified by the caller's rules — docs/CLASSIFY_API.md |
| `POST /v1/analyze` | one statement | income / expenses / EMI / affordability / risk — docs/B2B_API.md §1–8 |
| `GET /v1/health`, `/v1/ready`, `/v1/formats`, `/v1/classify/schema` | — | no key needed |

Keys are issued by you through `/internal/clients` with `B2B_ADMIN_TOKEN`.

## 1. Get the code onto GitHub

Render builds from `github.com/saikalyancb-06/FinLens`, branch `master`. The
local PROJECT folder was one commit behind it (the `/v1/classify` pull request
was merged on GitHub only), so the deploy-ready code is delivered as a git
bundle built on top of the current `master`. From the PROJECT folder:

```powershell
git stash push -u -m "local copy before deploy-ready"   # safety net; everything in it is also in the bundle
git fetch .\finlens-deploy.bundle deploy-ready:deploy-ready
git merge --ff-only deploy-ready
git push origin master
```

Then confirm:

```powershell
git log --oneline -3                   # "Deploy-ready: ..." on top
git ls-files mlmodel/artifacts         # categorizer_model.joblib + model_metadata.json
git ls-files .env                      # prints nothing
```

## 2. Render

**First time:** render.com → **New → Blueprint** → the FinLens repo. It creates
the web service (`standard`: 1 CPU / 2 GB), Postgres and Key Value from
`render.yaml`. Answer the prompts: `ALLOWED_ORIGINS` = the service URL (e.g.
`https://treasury-lens.onrender.com`); leave the Google / Microsoft values blank
unless mailbox ingestion is needed. Deploy.

**Already on Render:** push, then Blueprint → Sync (applies the plan change and
the new environment variables).

Either way the `preDeployCommand` (`scripts/db_migrate.py`) runs before the new
version takes traffic: empty database → all tables + `alembic stamp head`;
existing database → `alembic upgrade head`. Expect in the deploy log:
`[migrate] done: revision 014, 44 tables`. A migration failure aborts the deploy
and the old version keeps serving. There is no manual shell step any more.

## 3. Check it

```bash
curl https://<app>/v1/health            # {"status":"healthy",...}
curl https://<app>/v1/ready             # "database":"ok"
curl -o /dev/null -w "%{http_code}\n" https://<app>/docs     # 404 (hidden in production)
python scripts/b2b_smoke_test.py --base-url https://<app> --admin-token <B2B_ADMIN_TOKEN>
```

`B2B_ADMIN_TOKEN` is under Render → service → Environment (Render generated it).
The smoke test expects `86 passed, 0 failed`; it creates, then disables, two
`smoke-*` clients.

## 4. Onboard Credit Lens

```bash
API=https://<app>; TOKEN=<B2B_ADMIN_TOKEN>
curl -X POST $API/internal/clients -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Credit Lens","slug":"creditlens","contact_email":"<their email>",
       "rate_limit_per_minute":60,"rate_limit_per_day":5000,"rate_limit_per_month":100000,
       "result_retention_hours":24}'
curl -X POST $API/internal/clients/creditlens/keys -H "X-Admin-Token: $TOKEN" \
  -H "Content-Type: application/json" -d '{"name":"production"}'
```

The second call prints the key **once**. Send it over a secure channel with
docs/B2B_API.md, docs/CLASSIFY_API.md and
`postman/TreasuryLens_API.postman_collection.json`.

Their first call:

```bash
curl -X POST https://<app>/v1/statements/consolidate \
  -H "Authorization: Bearer <key>" -F "files=@Case3.zip"
```

Batches of 50+ pages take 15–50 s; for those they add `-F async_mode=true` and
poll `GET /v1/statements/consolidate/{request_id}`.

## Before going live

- **The GitHub repository is public.** It holds the full source of a system
  that processes bank statements. No secrets or statements are in its history
  (checked 2026-09-28), but make it private (GitHub → Settings → General →
  Danger Zone → Change visibility) and re-authorise Render if it asks.
- The local `.env` holds a real Google OAuth secret and Setu credentials. It was
  never committed; rotate them if the file has been shared.
- `/internal/clients` is reachable on the public URL, protected by the admin
  token alone. Treat that token like a root password.

## Operating notes

- One instance, one worker. At most `B2B_MAX_CONCURRENT_JOBS` (2) statement jobs
  run at once; further requests wait up to 120 s, then get `503` + `Retry-After`.
  Health checks and light requests are never blocked by a running job.
- Results are kept `result_retention_hours` (default 24 h), then purged; usage
  records stay for billing (`GET /internal/clients/<slug>/usage`).
- Logs: Render → service → Logs. Every response carries `X-Request-Id`, which
  is also in the log line — ask clients to quote it.
- New bank layout from a client: check `data.statements[].status` is `PASSED`
  on their first file. `FAILED` or a balance flag means the layout needs a look.
