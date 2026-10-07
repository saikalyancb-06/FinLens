# Kredo Treasury Analytics & Automated BRS Engine

An enterprise-grade, multi-tenant Treasury Management System (TMS) and Automated Bank Reconciliation Statement (BRS) Engine built with Python (FastAPI), React, CatBoost ML, and ReportLab.

---

## 🏛️ Executive Summary

**Kredo Treasury Analytics** automates multi-channel bank statement ingestion, machine learning transaction categorization, duplicate transaction detection, multi-entity treasury liquidity aggregation, and automated bank-versus-books ledger reconciliation with automated PDF report exports and scheduled email dispatch.

### Key Highlights
- **Multi-Channel Ingestion**: Support for PDF, CSV, Excel statement uploads (with PDF password unlock), RPA automation, Account Aggregator (AA) integrations, and multi-provider mailbox discovery (Gmail API, Microsoft Graph, IMAP over TLS).
- **Hybrid Categorization Engine**: Rule Engine matching ($97\%$ default confidence threshold) with seamless fallback to CatBoost ML multi-class classification.
- **Automated BRS Engine**: books-vs-bank reconciliation that auto-matches only on transaction IDs (cheque no, UTR, voucher no) or unambiguous same-amount pairs, sends name-based and group matches to review, lists amount differences as their own BRS lines, and carries outstanding items forward period to period. Rules: [docs/RECONCILIATION_ENGINE.md](docs/RECONCILIATION_ENGINE.md).
- **Multi-Tenant Data Isolation**: Complete tenant scoping across database schemas, files, email schedules, and reports.
- **Responsive ERP Layout**: Zero-overflow responsive UI supporting 320px mobile viewports up to 1920px widescreen monitors.

---

## 🔌 Statement API (for other products, e.g. Credit Lens)

Bank statements in, one classified JSON record per transaction out: duplicates
removed, internal transfers tagged, running balances reconciled.

| Doc | For |
|---|---|
| [docs/API_QUICKSTART.md](docs/API_QUICKSTART.md) | callers: the curl command and the response shape |
| [docs/B2B_API.md](docs/B2B_API.md) | full API reference (`/v1/statements/consolidate` is §10) |
| [docs/CLASSIFY_API.md](docs/CLASSIFY_API.md) | classifying with the caller's own rules |
| [docs/RENDER_FREE_TEST.md](docs/RENDER_FREE_TEST.md) | free test deploy on Render |
| [docs/B2B_DEPLOY_CHECKLIST.md](docs/B2B_DEPLOY_CHECKLIST.md) | production deploy (`render.yaml`) and onboarding a client |
| [postman/](postman/) | Postman collection |

---

## 📐 System Architecture Diagrams

### 1. High-Level System Architecture

```mermaid
flowchart TD
    subgraph Data Sources & Ingestion Channel
        A1[Manual File Upload\nPDF / CSV / Excel]
        A2[RPA Playwright Automation\nBank Portal Scraping]
        A3[Account Aggregator\nAA Consent Flow]
        A4[Mailbox Discovery\nGmail / Microsoft Graph / IMAP]
    end

    subgraph Core Processing Pipeline
        B[FastAPI Application Server]
        C[File Storage & Deduplication]
        D[PyMuPDF / pdfplumber / pandas\nText & Table Extractor]
        E[Rule Engine\nPattern Matching]
        F[CatBoost ML Engine\nFeature Vector Classifier]
    end

    subgraph Database & Persistence
        G[(PostgreSQL Database)]
        G1[(Uploaded Statements)]
        G2[(Canonical Transactions)]
        G3[(Reconciliation Runs & Matches)]
        G4[(Multi-Tenant Entities & Accounts)]
    end

    subgraph Treasury Analytics & Output
        H[Dashboard & CFO Board Pack]
        I[Transactions Workbench]
        J[Automated BRS Matching Engine]
        K[ReportLab PDF Engine]
        L[SMTP Scheduled Email Worker]
    end

    A1 --> B
    A2 --> B
    A3 --> B
    A4 --> B

    B --> C
    C --> D
    D --> E
    E -- Confidence >= 0.80 --> G2
    E -- Confidence < 0.80 --> F
    F --> G2

    G2 --> H
    G2 --> I
    G2 --> J
    J --> G3
    J --> K
    K --> L
```

---

### 2. Transaction Categorization Flow

```mermaid
flowchart LR
    A[Raw Narration Text & Amount] --> B{Rule Engine Match?}
    B -- Yes (Conf >= 0.80) --> C[Assign Rule Category\nPrediction Source: Rule Engine]
    B -- No (Conf < 0.80) --> D[Extract Text & Amount Features]
    D --> E[CatBoost ML Classifier]
    E --> F{ML Conf >= 0.80?}
    F -- Yes --> G[Assign ML Category\nPrediction Source: ML Model]
    F -- No --> H[Flag as Uncategorized\nRoute to Review Queue]
    C --> I[(Transactions Ledger)]
    G --> I[(Transactions Ledger)]
    H --> I[(Transactions Ledger)]
```

---

### 3. Automated Bank Reconciliation (BRS) Matching Engine

```mermaid
flowchart TD
    A[Ledger entries in period + items carried from last run] --> M
    B[Bank entries in period + items carried from last run] --> M
    M{Matching} --> P0[Reviewer decisions from earlier runs: confirmed re-applied, rejected never re-proposed]
    P0 --> PA[Auto: same amount + same cheque/UTR/voucher ID, 7 days, cheques 90]
    PA --> PB[Auto: same amount, same date]
    PB --> PC[Auto: same amount within 7 days, only candidate on both sides]
    PC --> PD[Review: same ID, different amount -> difference line]
    PD --> PF[Review: 1 bank = 2-5 ledger rows, 1 ledger row = 2-5 bank rows]
    PF --> PE[Review: same amount, shared party name or nearest date, 30 days]
    PE --> BR[BRS bridge: book closing +/- outstanding items = computed bank closing]
    BR --> V{residual = bank closing - computed}
    V -- 0, nothing open --> C1[Reconciled clean]
    V -- 0, items or reviews --> C2[Reconciled with exceptions]
    V -- not 0 --> C3[Unreconciled]
```

---

## 🛠️ Technology Stack

| Layer | Technology Used |
| :--- | :--- |
| **Backend Framework** | FastAPI (Python 3.14 / Pydantic v2 / Starlette) |
| **Database & ORM** | SQLAlchemy 2.0 / Alembic (PostgreSQL) |
| **Machine Learning** | CatBoost Classifier / Scikit-Learn / Joblib |
| **Parsing & OCR** | PyMuPDF (fitz), pdfplumber, pandas, openpyxl |
| **PDF Generation** | ReportLab Platypus Engine |
| **Frontend Framework** | React (Vanilla JavaScript, Babel, Chart.js) |
| **Styling** | Custom Responsive Vanilla CSS (ERPNext-inspired theme) |
| **RPA & Automation** | Playwright / Headless Browser Worker |
| **Testing** | Pytest / TestClient / AnyIO |

---

## 💻 Full Feature & Tab Walkthrough

### 1. 📊 Dashboard Tab (`/dashboard`)
The Dashboard serves as the central Executive CFO / Board Pack view, aggregating real-time treasury metrics across all entities and bank accounts.

#### Features & Components:
- **CFO / Board Pack KPI Cards**:
  - **Consolidated Liquidity**: Real-time sum of group cash balances across active registered bank accounts and entities.
  - **High-Value Transactions ($> \text{₹}50,000$)**: Count of velocity spikes and large transactions requiring audit inspection.
  - **Anomalies & Uncategorized**: Number of pending transactions awaiting category review.
  - **Policy Compliance**: Percentage rating of account balance threshold adherence.
- **Ledger Summary Grid**: Total Inflow, Total Outflow, and Net Cash Flow (Inflow minus Outflow).
- **Cash Flow Trend Chart**: Interactive Chart.js line graph displaying daily or monthly credits vs. debits with granularity toggles (`Daily` / `Monthly`).
- **Category Mix & Top Categories**: Combined side-by-side card rendering a doughnut chart of expense breakdown alongside a progress-bar list of the top categories by monetary value.
- **Treasury Overview Table**: Latest stored canonical transactions with quick navigation to full workbench views.

---

### 2. 📑 Transactions Workbench (`/transactions`)
The Transactions Workbench allows searching, filtering, inspecting, sorting, and exporting all canonical financial transactions.

#### Features & Components:
- **Global & Advanced Filters**: Filter by Entity, Bank Account, Date Range (`From` / `To`), Category, Transaction Type (`DEBIT` / `CREDIT`), and keyword text search.
- **Responsive Data Grid**: Displays Date, Account Number, Narration, Category Badge, Confidence Score %, Debit, Credit, Running Balance, and Prediction Source (`Rule Engine` vs `ML Model`).
- **Inline Sorting & Pagination**: Sort by Date, Description, Category, or Amount with configurable page sizes (25, 50, 100).
- **Full Ledger Clear**: Option to clear categorized outputs for re-parsing.

---

### 3. 📥 Statement Ingestion (`/ingestion`)
Multi-channel statement ingestion hub supporting 4 distinct ingestion pathways:

#### Pathways:
1. **Manual File Upload**:
   - Accepts Bank Statements in PDF, CSV, and Excel formats.
   - **PDF Password Unlock**: Auto-detects password-protected PDF files and prompts in-memory document password unlock.
   - **Account Binding**: Select which registered bank account the file binds to.
   - **Pre-Upload Validation**: Enforces that at least 1 bank account exists in Bank Master before enabling uploads.
2. **RPA Automation**: Triggers automated Playwright browser workers to log into bank portals and fetch digital e-statements.
3. **Account Aggregator (AA)**: Simulates consent-based Account Aggregator API data pulls.
4. **Mailbox Discovery**: Connect one or more mailboxes (Gmail, Outlook/Microsoft 365, or any IMAP host) and let the discovery engine find statements in them. Read-only, per-user, and described in full under [📬 Multi-Provider Mailbox Statement Discovery](#-multi-provider-mailbox-statement-discovery).

---

### 4. ⚖️ Reconciliation Engine (BRS) (`/reconciliation`)
Automated Bank Reconciliation Statement (BRS) generator comparing internal accounting books (Tally, QuickBooks, SAP) against bank statement transactions.

#### Features & Components:
- **Books Import**: Drag-and-drop Tally/ERP CSV or Excel exports. Auto-detects column headers (`Date`, `Description`, `Debit`, `Credit`, `Ref`).
- **Convention Selection**: Supports both standard CSV (`Debit=Out`, `Credit=In`) and Tally-style (`Debit=In`, `Credit=Out`) conventions.
- **Book Opening Balance**: typed on the screen, or the previous reconciliation's closing, or the ledger file's "Opening Balance" row — never borrowed from the bank. Asked for only when none of these exists.
- **Matching** (rules in [docs/RECONCILIATION_ENGINE.md](docs/RECONCILIATION_ENGINE.md)): calculates
  - Opening & Closing Balances
  - Net Movement
  - Verdict (`All Clear`, `Reconciled with Outstanding Items`, or `Difference Found`)
  - Timing Differences (Unpresented cheques, uncleared deposits)
  - Unexplained Bank/Books Transactions
- **BRS PDF Export**: Downloads a professional ReportLab PDF BRS report containing executive summaries, exception tables, and audit logs.

---

### 5. 🔍 Review Queue (`/review-queue`)
The Exception Management workbench where finance controllers inspect and resolve ambiguous transactions.

#### Features & Components:
- Displays transactions requiring verification (Uncategorized, low-confidence predictions, or BRS discrepancies).
- **Confirm / Reclassify Actions**: Confirm suggested categories or assign new categories directly. Manual overrides immediately retrain rule memory.

---

### 6. 📈 Reports & CFO Board Pack (`/reports`)
Comprehensive financial reporting hub for executive presentation.

#### Features & Components:
- **Entity Comparison Matrix**: Multi-entity side-by-side liquidity, inflow, outflow, and net cash flow comparison table.
- **Export Capabilities**: Generate downloadable CFO Board Packs and category breakdown analytics.

---

### 7. 🏦 Bank Master & Entity Management (`/bank-master`)
Corporate structure and bank account registry.

#### Features & Components:
- **Entity Creation & Deletion**: Register corporate entities (e.g., Parent Co, Subsidiaries). Soft-delete checks verify no active bank accounts remain linked.
- **Bank Account Registration**: Register bank accounts linked to entities.
  - **12-Digit Account Validation**: Enforces strict 12-digit numeric account numbers (`^\d{12}$`).
  - **Account Types**: `CURRENT`, `SAVINGS`, `OVERDRAFT`, `CASH CREDIT`.
  - **Inline Editing**: Dynamically edit linked entity or account type directly in the table.
  - **Safe Account Deletion**: Soft-deletes account record (`deleted_at`) and purges all dependent statement and transaction records cleanly.

---

### 8. ⚙️ Settings & Scheduled Email Reports (`/settings`)
Application settings and automated report dispatch scheduler.

#### Features & Components:
- **Scheduled Email Reports**:
  - Create, Edit, Enable/Disable, and Delete scheduled email reports.
  - Supported Frequencies: `Daily`, `Weekly`, `Monthly`.
  - Configurable dispatch time, day of week/month, and recipient email lists.
- **Preferences**: Theme, notification thresholds, and currency preferences.

---

## 📬 Multi-Provider Mailbox Statement Discovery

Bank statements arrive by email. This subsystem connects a user's mailboxes and
finds the statements in them — without being told which bank, which sender, or
which folder to look at.

### 1. The `EmailProvider` Abstraction
Everything above the mailbox layer — discovery, classification, extraction, the
API and the UI — is written against one interface and never against a connector:

```
EmailProvider
  ├── GmailProvider           (Gmail REST API)
  ├── MicrosoftGraphProvider  (Microsoft Graph — Outlook.com and Microsoft 365)
  └── ImapProvider            (IMAP over TLS; XOAUTH2 or an app-specific password)
```

- **Adding a provider** is one module plus one line in `app/mailbox/registry.py`. No statement-detection code changes.
- **Retrieval only.** There is no send path anywhere in the mailbox layer, and **SMTP is not used and cannot be used** for retrieval — it is a protocol for *delivering* mail, not for reading it. Any tool that claims to "fetch statements over SMTP" is describing something else.
- **IMAP credentials** are provider-issued *application-specific passwords* (Yahoo, iCloud, Fastmail, Zoho, company mailboxes), never the account's primary login password: an app password can be revoked on its own.

### 2. Per-User, Multi-Mailbox Model
A user may hold several connections at once — two Gmail accounts plus an Outlook
mailbox is an ordinary configuration. `connected_accounts` therefore has no
one-row-per-user assumption:

- Every mailbox query filters on `user_id`. A connection is opened only from a row whose ownership the caller has already proved; there is no "find the connection for id X" helper that trusts an id from a request body.
- Re-connecting the same mailbox updates the existing row (matched on the provider-side stable identity — Google `sub`, Graph `id`, `host:user` for IMAP) instead of accumulating duplicates when a display address changes.
- A connection carries a **status** (`CONNECTED`, `NEEDS_REAUTH`, `REVOKED`, `ERROR`, `DISCONNECTED`) with the detail behind it, so the UI can say *why* a mailbox stopped working and offer the right action.

### 3. The Staged Scan
A large mailbox must cost a bounded amount of work, so each stage narrows the
set before the next, more expensive one runs:

```
search (cheap metadata)
  → score metadata, keep candidates
    → fetch the full message for candidates only
      → score content, walk the MIME tree
        → download only attachments that could be documents
          → classify from document content
            → persist, deduplicate, optionally ingest
```

**Discovery never depends on a known sender or a known institution.** There is
no allow-list of bank domains and no hardcoded issuer table gating the scan: a
statement from a credit union nobody has heard of is found on the same evidence
as one from a national bank. The institution is an *output* of classification
(`institution_name` + `institution_confidence`), not an input to it. A document
that arrived as the body of an email rather than as a file is still a statement
— `source_kind` records the difference.

Scans run on a bounded in-process thread pool (`MAILBOX_SCAN_WORKERS`, default
2) with progress written to the `mailbox_scans` table, so the UI polls a row
rather than holding an HTTP request open for minutes.

### 4. Duplicate Detection
The same statement legitimately arrives more than once, in more than one shape,
so two independent identities are checked:

- **Content**: SHA-256 of the document's bytes — catches a forwarded, re-sent or renamed copy anywhere in the mailbox.
- **Message + attachment identity**: `(email_message_id, provider_attachment_id)` — catches the same attachment seen on an earlier scan. The attachment reference is part of the key on purpose: matching on message id alone made the *second* attachment of a two-statement email look like a duplicate of the first and silently discarded it.

Underneath, the `statements` table's unique **`(user_id, file_sha256)`** index
makes re-imports idempotent at the database level — re-running a scan, or
importing the same file by hand, cannot produce a second copy. Uniqueness is
scoped per user: two users may legitimately hold the same statement.

### 5. Security Model
- **Per-user scoping** on every read: connections, discovered documents, scans and imports are all filtered by `user_id`.
- **Credentials encrypted at rest** with `TOKEN_ENCRYPTION_KEY` (Fernet, `app/email/utils.py`) — OAuth refresh tokens *and* IMAP application passwords, for every provider. Rotating that key forces every user to reconnect.
- **No credentials reach the frontend.** Tokens, secrets and app passwords are decrypted at the last possible moment inside the provider registry and never appear in an API response.
- **Read-only scopes only**: `gmail.readonly` + `userinfo.email` for Google; `Mail.Read` + `User.Read` + `offline_access` for Microsoft. No send scope, no write scope. A consent screen that mentions sending mail means the OAuth client is misconfigured.

### 6. Provider Setup
| Provider | What you need |
| --- | --- |
| **Google** | An OAuth client (type *Web application*) at [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials). Enable the Gmail API, add the `gmail.readonly` and `userinfo.email` scopes, and register `GOOGLE_REDIRECT_URI` (default `http://localhost:8000/email/oauth/callback`) as an Authorised redirect URI — **byte for byte**, or the callback fails with `redirect_uri_mismatch`. Set `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`. |
| **Microsoft** | An app registration at [entra.microsoft.com](https://entra.microsoft.com) → *App registrations*. Delegated permissions `Mail.Read`, `User.Read`, `offline_access`. Register `MICROSOFT_REDIRECT_URI` (default `http://localhost:8000/email/oauth/microsoft/callback`) as a *Web* redirect URI. Set `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET`, and `MICROSOFT_TENANT` to `common` (both audiences), `consumers` (personal Outlook/Hotmail only), `organizations`, or a specific tenant GUID — it must agree with the registration's own audience. |
| **IMAP** | **Nothing server-side.** The user supplies host, port and a provider-issued application-specific password at connect time; it is encrypted with `TOKEN_ENCRYPTION_KEY` before storage. |

All variables are documented in `.env.example`. An OAuth provider with no client
credentials configured is reported as unavailable by `GET /email/providers`
rather than offered and then failing at the consent screen.

### 7. API Endpoints
| Method & Path | Purpose |
| --- | --- |
| `GET /email/providers` | What the connect screen should offer, and whether each provider is configured. |
| `GET /email/connections` | This user's mailbox connections, with status. |
| `POST /email/connections/{provider}/authorize` | Begin an OAuth connection; returns the consent URL. |
| `POST /email/connections/imap` | Connect an IMAP mailbox (host, port, TLS, username, app password). |
| `GET /email/oauth/callback` | Google OAuth callback. |
| `GET /email/oauth/microsoft/callback` | Microsoft OAuth callback. |
| `POST /email/scans` | Queue a background scan (one connection, or every active mailbox) and return immediately. |
| `GET /email/scans/{id}` | Scan progress: stage, percentage, and per-stage counters. |
| `POST /email/scan` | Synchronous scan — waits for the result. Retained for compatibility with the original single-mailbox flow. |
| `GET /email/statements` | Discovered financial documents and their classification. |
| `POST /email/statements/{id}/map-account` | Bind a discovered document to a registered bank account. |
| `POST /email/import/{id}` | Import one discovered statement into the transaction pipeline. |
| `POST /email/import-all` | Import every pending discovered statement. |
| `DELETE /email/connections/{id}` | Disconnect one mailbox. |
| `DELETE /email/disconnect` | Disconnect every mailbox for this user. |

### 8. Known Limitations
Stated plainly, because each one is a case where a user could otherwise assume a
statement was found when it was not:

- **Statements behind a login link are not fetched.** When a bank emails "your statement is ready, sign in to view it", there is no document in the message. The scan detects that the message points at an externally hosted statement and *reports* it, rather than dropping it silently — but it will not log in to fetch it.
- **Scans do not survive a process restart.** There is no Celery, no RQ and no broker; work runs on an in-process thread pool. A scan in flight when the server restarts is lost, and `reap_stale_scans` marks its row failed at the next startup so the UI never shows a spinner that will never finish. This is the piece to replace if scans ever need to survive restarts or spread across machines — nothing else changes.
- **Legacy `.xls` needs the optional `xlrd` package.** Without it, an old-format Excel statement is reported as unreadable with that explanation attached, not counted as "not a statement".
- **Scanned, image-only PDFs need OCR.** A PDF with no text layer is reported as requiring OCR rather than silently discarded as empty.

---

## ⚡ Setup & Installation

### Prerequisites
- Python 3.10+ (Recommended: Python 3.14)
- Node.js (Optional, for frontend tooling)
- PostgreSQL 15+ (**required** — there is no SQLite fallback). The quickest way to get one is `docker compose up -d postgres`.

### 1. Clone & Install Dependencies
```bash
git clone https://github.com/your-org/kredo-treasury.git
cd kredo-treasury

python -m venv venv
venv\Scripts\activate  # On Windows
# source venv/bin/activate  # On Linux/macOS

pip install -r requirements.txt
```

### 2. Environment Configuration
Create a `.env` file in the root directory:
```ini
JWT_SECRET_KEY=your_super_secret_jwt_key_here
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/backend_db
ML_MODELS_DIR=mlmodel/saved_models
UPLOAD_DIR=uploads
```
`.env.example` documents every supported variable, including the connection
pool and test-database settings. A `DATABASE_URL` that is not a PostgreSQL URL
is rejected at startup, and an unreachable server is a hard failure rather than
a silent fallback to a local file.

### 3. Database & Startup

Start PostgreSQL (skip if one is already running), put its URL in `.env`, and
start the app — that is all:

```bash
docker compose up -d postgres        # or your local PostgreSQL service
python main.py
```

On start-up (`DB_AUTO_CREATE`, on by default outside production) the app
brings the database to the current schema whatever state it is in
(`app/database/bootstrap.py`):

* the database named in `DATABASE_URL` does not exist → it is created;
* empty database → every table is built and recorded in `alembic_version`;
* a database built by an older checkout → missing tables and columns are
  added (additive only, nothing dropped) and migrations are applied;
* afterwards `python -m alembic upgrade head` is always a clean no-op.

If it still cannot connect, the error says which of these it is: wrong
password in `DATABASE_URL`, PostgreSQL not running, or the database could not
be created.

In production (`ENVIRONMENT=production`) `DB_AUTO_CREATE` is off and
`python scripts/db_migrate.py` (Render's pre-deploy step) owns the schema: it
builds an empty database, upgrades a versioned one, and adopts one that was
built without Alembic.

App will be accessible at: `http://localhost:8000`

---

## Connecting Microsoft

1. Create an Entra ID App Registration: set account type to **Multitenant and personal accounts**.
2. Add Microsoft Graph delegated permissions: `Mail.Read`, `User.Read`, `offline_access`.
3. Configure Redirect URI: `http://localhost:8000/email/oauth/microsoft/callback` (Web platform).
4. Create a Client Secret under **Certificates & secrets**.
5. Set `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_TENANT=common`, and `MICROSOFT_REDIRECT_URI` in `.env`.

---

## Connecting Gmail

1. Create a Google Cloud project and enable the **Gmail API**.
2. Configure OAuth Consent Screen: User type **External**, add test users under **Test users**.
3. Create OAuth 2.0 Client ID (**Web application** type).
4. Set Authorized Redirect URI: `http://localhost:8000/email/oauth/callback`.
5. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` in `.env`.
6. *Note*: `gmail.readonly` requires CASA verification for public production, but works immediately for all added test users in Testing mode.

---

## 🧪 Testing & Verification

Run the full pytest suite:
```bash
python -m pytest tests/test_bank_account_deletion.py tests/test_multi_tenant_isolation.py tests/test_settings_api.py tests/test_transactions_api.py tests/test_deduplication_regression.py
```
Expected result: **`35 / 35 PASSED`**.

---

## 🛡️ License

Copyright © 2026 Kredo Treasury Analytics. All rights reserved.


## The legacy SQLite database (migration complete)

Earlier builds fell back to a local `backend_sqlite.db` file whenever PostgreSQL
was unreachable. That fallback is gone, the data was migrated on 17 Aug 2026, and
the one-shot migration and cleanup scripts have been removed along with it.
PostgreSQL is the only backend: `app/database/session.py` rejects any
non-`postgresql://` URL at startup, and `tests/test_security_hardening.py` holds
that line.

`docs/POSTGRES_MIGRATION.md` is kept as the record of what the migration changed
and which rows it repaired. Its command examples refer to scripts that no longer
exist — it is history, not a runbook.
