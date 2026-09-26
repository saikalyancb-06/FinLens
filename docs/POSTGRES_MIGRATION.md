# PostgreSQL Migration — Runbook

The application no longer supports SQLite. PostgreSQL is the only backend in
development, test and production. This document covers the cutover, the data
repairs the migration performs, and how to roll back.

---

## 1. What changed

| Area | Before | After |
|---|---|---|
| `app/database/session.py` | Fell back to `backend_sqlite.db` whenever PostgreSQL was unreachable | PostgreSQL only. A non-`postgresql://` URL or an unreachable server raises at startup. Adds pooling, `pool_pre_ping`, bounded startup retry and a server-side `statement_timeout` |
| `app/config.py` | `ALLOW_SQLITE_FALLBACK` | Removed. New `DB_*` pool/retry settings and `validate_database()`, which rejects any non-PostgreSQL URL |
| `app/models/statement.py` | `reconciled` had `server_default=text("0")` | `text("false")` — PostgreSQL rejects an integer default on a boolean column, so the schema could not be created at all |
| `app/api/dashboard.py` | `SUM(...)` result used directly in float arithmetic | Wrapped in `int()`. PostgreSQL's `SUM(bigint)` returns `numeric` → `Decimal`, which raised `TypeError` on every dashboard request |
| `alembic.ini` / `alembic/env.py` | Hard-coded `sqlite:///backend_sqlite.db`, `render_as_batch=True` | URL read from `DATABASE_URL`; batch mode dropped; `compare_type` / `compare_server_default` enabled |
| `alembic/versions/001_*` | Rebuilt the table via SQLite batch mode | Drops the old unique constraint in place, guarded and idempotent |
| Test suite | `sqlite:///:memory:` | A real PostgreSQL test database, created automatically and reset per run (`tests/pgtestdb.py`) |
| `docker-compose.yml` | Password `postgrespassword`, mismatched with `.env` | Credentials come from `POSTGRES_*` in `.env`; healthcheck waits for the application database, not just the server |
| AA tables | Registered only as a side effect of importing `app.aa.routes` — `create_all` could miss them | `app.aa.models` is in the model import list |

---

## 2. Cutover

Run these from the project root, with `.env` set up (copy `.env.example` if needed).

### 2a. Getting a PostgreSQL server

**Native install on Windows (no Docker required).** This is the path used on the
development machine, which has no Docker installed.

```powershell
winget install -e --id PostgreSQL.PostgreSQL.17
```

The installer asks for a superuser password. Whatever you choose, put the same
value into `DATABASE_URL` in `.env` — the scripts and the app both read it from
there. Accept the default port 5432; leave Stack Builder unticked at the end.

`psql` is not added to `PATH` by the installer, so either add
`C:\Program Files\PostgreSQL\17\bin` to `PATH` or call it by full path. Then
create the application database:

```powershell
& "C:\Program Files\PostgreSQL\17\bin\createdb.exe" -U postgres backend_db
& "C:\Program Files\PostgreSQL\17\bin\pg_isready.exe" -U postgres -d backend_db
```

**Or with Docker,** if it is available:

```bash
docker compose up -d postgres
docker compose exec postgres pg_isready -U postgres -d backend_db
```

Either server works — nothing in this project depends on a specific PostgreSQL
version beyond 15+. The compose file remains valid for CI and for other machines.

### 2b. The migration itself

```bash
# 0. Back up the legacy file. It is the only copy of the pre-migration data.
copy backend_sqlite.db backend_sqlite.db.pre_postgres_backup     # Windows
# cp backend_sqlite.db backend_sqlite.db.pre_postgres_backup     # Linux/macOS

# 1. Install the PostgreSQL driver if this is a fresh environment.
#    psycopg2-binary ships a cp314 Windows wheel, so Python 3.14 needs no
#    compiler and no source build.
pip install -r requirements.txt

# 2. Dry run — reports exactly what will be copied, repaired and dropped.
#    Writes nothing.
python scripts/migrate_sqlite_to_postgres.py --dry-run

# 3. Migrate. --wipe truncates the target first, so the command is repeatable.
python scripts/migrate_sqlite_to_postgres.py --wipe

# 4. Confirm.
python scripts/migrate_sqlite_to_postgres.py --verify-only
alembic current          # expect 004

# 5. Start the app and check readiness.
uvicorn main:app --reload
curl http://localhost:8000/readiness
```

Both scripts call `load_dotenv()` before parsing arguments, so `DATABASE_URL`
from `.env` is picked up without exporting anything into the shell. If you see
`error: no target database`, `.env` is missing or has no `DATABASE_URL` — pass
`--database-url` explicitly to override it.

The migration script creates the schema, copies every table in foreign-key
order, converts SQLite's loose values into PostgreSQL types, re-validates every
foreign key afterwards, stamps the Alembic revision, and writes
`migration_report.json`. If foreign key validation fails, the whole thing rolls
back and nothing is written.

---

## 3. What the migration does to the data

Verified against the 17 Aug 2026 copy of `backend_sqlite.db` (36,858 rows across
33 tables). Money totals — `transactions.debit_paise`, `transactions.credit_paise`,
`statements.opening_balance_paise`, `statements.closing_balance_paise` — matched
to the paise on both sides.

### Repairs

SQLite never enforced NOT NULL or foreign keys here, so the file contains rows
PostgreSQL will not accept. Each repair is deterministic, and every one is
counted in `migration_report.json`.

| Repair | Rows | How the value is derived |
|---|---:|---|
| `statements.user_id` | 125 | Recovered from the owning account (`accounts.user_id`) — exact, not guessed |
| `statements.status` | 129 | Set to `pending`, the column's own server default |
| `statements.reconciled` | 129 | Set to `false` |
| `statements.uploaded_at` | 148 | Set to the migration timestamp — no earlier timestamp survives anywhere in the row |
| `transactions.direction` | 528 | Derived from `debit_paise`/`credit_paise`. All 528 are unambiguously credits |
| `transactions.created_at` | 8,161 | `updated_at` where present, otherwise the migration timestamp |
| `transactions.updated_at` | 1,832 | `created_at` where present, otherwise the migration timestamp |

### Rows dropped

170 rows reference a parent that no longer exists. Every one of these foreign
keys is `ON DELETE CASCADE` and `NOT NULL`, so PostgreSQL would have deleted
these rows when the parent was deleted — SQLite simply left them behind. The
full contents of each dropped row are recorded in `migration_report.json`.

| Table | Rows | Dangling reference |
|---|---:|---|
| `reconciliation_matches` | 118 | `run_id` → a deleted `reconciliation_runs` row |
| `aa_data_sessions` | 26 | `user_id` → a deleted user |
| `aa_consents` | 22 | `user_id` → a deleted user |
| `email_attachments` | 3 | `user_id` → a deleted user |
| `connected_accounts` | 1 | `user_id` → a deleted user |

Use `--orphan-policy abort` to stop at the first such row instead, if you would
rather repair the source data by hand first.

### Not migrated

* `_alembic_tmp_email_attachments` — leftover from an interrupted Alembic batch
  migration, 0 rows.
* `test_connected_accounts` — a stray test artefact, 1 row.
* Five legacy `email_attachments` columns that no longer exist on the model:
  `period_from`, `period_to`, `signals_json`, `account_number_masked`,
  `classification_code`. 9 rows are affected.

---

## 4. Tests

```bash
# With PostgreSQL running (native service or `docker compose up -d postgres`)
pytest -q
```

The suite creates `backend_test_db` (plus a few `backend_test_db_*` databases
for modules that need their own), drops and recreates the schema at the start of
each run, and never touches `backend_db`. Override the target with
`TEST_DATABASE_URL`.

Result of the conversion: **451 passed, 7 failed, 1 skipped**. All 7 failures are
pre-existing and unrelated to the database — they fail identically on the
original SQLite code in the same environment, because they need a Windows
`dist/KredoAgent.exe` build, a Playwright browser, or a specific PDF-parser
version:

```
test_kredo_agent_exe.py::test_standalone_kredo_agent_exe_execution
test_local_agent_pipeline.py::test_full_local_agent_mock_pipeline_and_security
test_rpa_backend_workflow.py::test_start_job_keeps_credentials_memory_only
test_rpa_backend_workflow.py::test_rpa_job_lifecycle_and_otp_limit
test_rpa_backend_workflow.py::test_rpa_otp_timeout
test_rpa_backend_workflow.py::test_rpa_pdf_password_pause_resume
test_statement_closing_balance.py::test_statement_footer_preserved_and_reconciles
```

Four test fixtures had to be corrected because they invented parent rows that
never existed — SQLite accepted the orphans, PostgreSQL does not. That is the
enforcement working, not a regression:

* `test_tier3_reuse_regression.py` — creates the `Account` it references
* `test_rpa_backend_workflow.py` — creates the `uploaded_files` row
* `test_storage_service.py` — creates the `User`, `UploadedFile`, `Account` and `Statement`
* `test_email_pickup_pdf_password.py` — a 66-character "SHA-256" trimmed to 64,
  the actual column width

---

## 5. Deleting the SQLite files

Only after the cutover has run **on the machine that holds the data**. Until
then `backend_sqlite.db` is the only copy of it.

`scripts/cleanup_sqlite_after_migration.py` will not delete anything until it
has proven, against the live PostgreSQL database, that:

1. every model table exists;
2. every table holds exactly the rows the migration should have produced —
   source count minus the documented drops, so a legitimate drop is never
   mistaken for data loss, and a missing row is never excused as one;
3. the four money totals match the SQLite file to the paise;
4. every foreign key resolves;
5. the Alembic revision was carried across.

```bash
# Check only. This is the default and never deletes.
python scripts/cleanup_sqlite_after_migration.py

# Verify, write backend_sqlite.db.gz, then delete backend_sqlite.db.
python scripts/cleanup_sqlite_after_migration.py --confirm

# Also delete the four backup_pre_* snapshots (~119 MB).
python scripts/cleanup_sqlite_after_migration.py --confirm --include-backups

# Skip the archive.
python scripts/cleanup_sqlite_after_migration.py --confirm --no-archive
```

If any check fails the script prints which one, deletes nothing, and exits
non-zero. Verified failure modes: PostgreSQL unreachable, schema absent
(migration never run), and rows missing from a single table — each is reported
precisely, with the files left intact.

By default the original is gzipped to `backend_sqlite.db.gz` (134 MB → ~27 MB)
and the archive is read back and checked before the original is removed. The
four `backup_pre_*` snapshots are kept unless you pass `--include-backups`.

Afterwards, `scripts/inspect_db.py` and `scripts/db_cleanup.py` stop working —
they open the SQLite file directly. Your backups are now `pg_dump`, not a file
copy.

---

## 6. Rollback

The migration only reads the SQLite file; it is never modified. To roll back,
restore the previous commit of the code and point `DATABASE_URL` back at the old
file. Note that the SQLite fallback path no longer exists in the current code, so
a rollback means reverting the code, not just the configuration.

```bash
git checkout <pre-migration-commit>   # or restore your file backup
# DATABASE_URL=sqlite:///./backend_sqlite.db   (only valid on the old code)
```

To redo the migration from scratch at any time:

```bash
python scripts/migrate_sqlite_to_postgres.py --wipe
```

---

## 7. Operational notes

* **Schema ownership.** Locally, `DB_AUTO_CREATE=true` lets SQLAlchemy create
  missing tables at import. In production and under docker-compose it is
  `false`, and `alembic upgrade head` owns the schema.
* **Pool sizing.** `DB_POOL_SIZE=10` plus `DB_MAX_OVERFLOW=20` means one process
  can hold up to 30 connections. Stock PostgreSQL allows 100 — count your API
  workers plus the worker container before raising these.
* **Statement timeout.** `DB_STATEMENT_TIMEOUT_MS` defaults to 60s. Long report
  exports may need a higher value, or `0` to disable.
* **Backups.** The data no longer lives in a file you can copy. Set up
  `pg_dump`:
  `docker compose exec postgres pg_dump -U postgres backend_db > backup_$(date +%F).sql`
* **`scripts/inspect_db.py` and `scripts/db_cleanup.py`** still open
  `backend_sqlite.db` directly with the `sqlite3` module. They are developer
  utilities and were left alone; they operate on the legacy file, not on
  PostgreSQL.
