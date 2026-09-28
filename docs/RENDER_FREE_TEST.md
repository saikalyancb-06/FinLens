# Free test deploy on Render (API only)

For trying the statement API before paying for hosting. The production setup is
`render.yaml` + [B2B_DEPLOY_CHECKLIST.md](B2B_DEPLOY_CHECKLIST.md); this is the
stripped-down free version. Verified 2026-09-28.

## Limits of the free tier

- 512 MB RAM, a fraction of one CPU: one statement takes ~30 s–2 min, big batches several minutes.
- Sleeps after 15 min without traffic; the next request waits ~1 min.
- Free Postgres is deleted 30 days after creation.
- No disk, no pre-deploy command, no shell.

Not for real client traffic.

## 1. Database

Render → **New → Postgres** → region Singapore, plan **Free** → Create.
Copy the **Internal Database URL**.

## 2. Web service

Render → **New → Web Service** → this repo, branch `master`, language
**Docker**, region Singapore (same as the database), instance **Free**.

- **Advanced → Health Check Path:** `/health`
- **Advanced → Docker Command:** leave **empty** (a custom `sh -c "…"` command
  is passed as one word by Render and fails with exit 127).

Environment variables:

| Key | Value |
|---|---|
| `DATABASE_URL` | Internal Database URL |
| `ENVIRONMENT` | `production` |
| `JWT_SECRET_KEY` | random, see below |
| `B2B_ADMIN_TOKEN` | random, see below (issues API keys; keep private) |
| `DB_AUTO_CREATE` | `true` (creates the tables on start; no migration step on free) |
| `TRUST_PROXY_HEADERS` | `true` |
| `B2B_MAX_CONCURRENT_JOBS` | `1` (keeps memory under 512 MB) |

Random values (PowerShell, run twice):
```powershell
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Deploy. The log ends with `Uvicorn running on http://0.0.0.0:10000`.

## 3. Check

```powershell
$API   = "https://<service>.onrender.com"
$TOKEN = "<B2B_ADMIN_TOKEN>"
Invoke-RestMethod "$API/v1/health"    # healthy
Invoke-RestMethod "$API/v1/ready"     # database=ok (cache=degraded is expected: no Redis)
```

## 4. Issue a key

```powershell
$h = @{ "X-Admin-Token" = $TOKEN }
Invoke-RestMethod -Method Post "$API/internal/clients" -Headers $h -ContentType "application/json" -Body '{"name":"Credit Lens Test","slug":"creditlens-test","contact_email":"<email>"}'
$resp = Invoke-RestMethod -Method Post "$API/internal/clients/creditlens-test/keys" -Headers $h -ContentType "application/json" -Body '{"name":"test"}'
$resp.secret     # shown once
```

Give the caller the base URL, that key and [API_QUICKSTART.md](API_QUICKSTART.md).
Never give out `B2B_ADMIN_TOKEN`.

## Moving to production

Create a fresh Blueprint from `render.yaml` (paid plan, managed migrations).
Do not reuse the free database: it was built with `DB_AUTO_CREATE`, so it has
no `alembic_version` and `scripts/db_migrate.py` will refuse it. Issue new keys
from the new admin token.
