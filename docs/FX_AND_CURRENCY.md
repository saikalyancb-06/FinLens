# Foreign currency: dataset, model, and multi-currency display

Two pieces of work that share a subject but not a code path.

1. **A cross-border training corpus** so the purpose classifier stops guessing on
   narrations it has never seen a shape of.
2. **Currency display**, so the Transactions tab can show every row in whatever
   currency you ask for.

---

## 1. Run this on your machine first

The database needs migration 007 before the app will start with the new models.

```powershell
cd C:\Users\Saikalyan-Kredo\Desktop\PROJECT
python -m alembic upgrade head
python main.py
```

`alembic current` should report `007`. The migration is guarded — every
`CREATE TABLE` and `ADD COLUMN` checks first — so running it twice is safe.

The currency table seeds itself the first time the Transactions tab loads.

---

## 2. Currency display

### What you see

The Transactions tab has a **"Show in:"** selector next to the Category filter.

| Selection | What it shows |
|---|---|
| **Actual (as contracted)** | Each row in the currency it was actually contracted in. A cross-border payment shows USD; a UPI transfer shows INR. |
| **INR / USD / GBP / …** | Every row converted to that one currency, at the rate stored for that row's *own transaction date*. |

Converted figures carry a `~` and a banner above the table. Figures read off the
bank's own advice do not. The distinction is the whole point: one is a fact from
a document, the other is an estimate from a rate table.

### What is stored, and what is not

`debit_paise`, `credit_paise` and `balance_paise` are **never rewritten**. Every
displayed figure is computed on read from those columns.

This is not fastidiousness. A US cent is 0.88 rupees, so a rupee amount rounded
to cents and back can land up to 44 paise from where it started. Converting only
ever from the stored rupees means switching to USD and back shows the exact
original. Persisting the converted value would make that drift permanent and
compound it on every switch.

Four new columns on `transactions`:

| Column | Meaning |
|---|---|
| `booked_currency` | What the `*_paise` columns are denominated in. `INR` for every existing row. |
| `original_currency` | The foreign currency from the advice — NULL on domestic rows. |
| `original_amount_minor` | The foreign amount, in that currency's own minor units. |
| `fx_rate` | INR per unit, as the bank applied it. |

`original_*` is left NULL rather than filled with `INR`/1.0, so "has a foreign
leg" stays a queryable fact rather than a guess.

### Where the foreign amount comes from

`app/currency/fx_parser.py` reads it off the narration, and is deliberately
conservative — it returns nothing far more often than it returns something:

- It rejects a narration where the amount, the quoted rate, and the booked
  rupees do not reconcile to within 5%. A misread narration produces a foreign
  figure that appears on no document, which is worse than showing rupees.
- It rejects bank-charge lines (`SWIFT CHARGES USD 25.00`) by looking at what
  precedes the currency code, not merely whether the word "fee" appears
  anywhere — `USD 306.36 MANAGEMENT FEE RECEIPT` is a 306-dollar receipt.
- It never infers a currency from the counterparty's country.

Rows it declines to parse display in INR, which is true.

### Rates

Editable under **Settings → Exchange Rates**, and refreshed automatically. Each
rate is dated; a transaction converts at the newest rate dated on or before its
own date.

Four provenance tags, shown as coloured badges:

| Tag | Meaning |
|---|---|
| `fbil` | India's official reference rate, from Financial Benchmarks India. The one to cite in anything statutory. Lags a few days. |
| `frankfurter` | Market reference rate, for the currencies and dates FBIL has not covered. No standing with a tax officer. A market rate never replaces an official one for the same day. |
| `manual` | You typed it. **Never overwritten by a fetch.** |
| `seed` | Placeholder that shipped in the source code, dated to no real day. Anything still showing `seed` has never refreshed successfully. |

The shipped placeholders were meaningfully wrong — measured against live rates
on 18 Aug 2026, SEK was out by 19.6%, AUD by 17.8%, CHF by 16.6%, EUR by 16.0%,
GBP by 15.5% and SGD by 15.0%. That is the whole argument for fetching them.

#### Automatic refresh

Runs inside the application's lifespan, so **rates refresh only while the app is
running**. A machine that has been off for a week comes back with week-old rates
until the first poll.

| Setting | Default | Notes |
|---|---|---|
| `FX_REFRESH_ENABLED` | `true` | Single switch. Off means no outbound calls at all. |
| `FX_REFRESH_MINUTES` | `30` | Floor of 5. RBI publishes once per business day, so faster polling only helps the market API. |
| `FX_REFRESH_STARTUP_DELAY` | `20` | Serve requests before reaching out. |
| `FX_MAX_STEP_CHANGE` | `0.10` | Reject a rate that moves more than this from the last trusted one. |

**Refresh now** in Settings forces a poll and shows the full report, rejections
included. A source that has started returning nonsense is visible there rather
than only in the server log.

#### What gets refused, and why

A wrong rate does not look wrong. It does not raise, it does not break a page —
it quietly multiplies every converted figure on the Transactions tab and keeps
doing so until someone finds a total they cannot explain. So a fetched rate is
stored only if it is positive, inside a plausible band (0.0001–1000 INR per
unit), and within `FX_MAX_STEP_CHANGE` of the last *trusted* rate. Everything
else is rejected and reported; the old rate stays, which is stale but true.

"Trusted" deliberately excludes `seed`. A placeholder has no authority to
protect, and treating it as a baseline would have rejected the first real rate
for six of the twelve currencies.

#### Manual rates: asked, never assumed

A `manual` rate is never overwritten *silently*. Where a fetched rate differs
from one you typed, **Refresh now** reports the pair and leaves yours alone:

```
1 rate you entered differs from what was just fetched
USD  2026-08-18    yours 91.2500    fetched 95.5110    +4.67%    frankfurter
                                        [Use fetched]  [Use fetched for all]  [Keep mine]
```

Choosing **Use fetched** re-runs the fetch with that currency named, so the
stored number still comes from the source — the client never supplies a rate,
and a row tagged `rbi` was genuinely published by RBI regardless of which button
produced it. The row's provenance flips from `manual` to the source at the same
time, because the value did.

The two-step exists because the application cannot know which number is better.
A rate you copied off a bank advice beats a market mid-rate *for that
transaction*; a rate you typed six months ago does not. Only you know which.

Note that the step-change gate compares **source to source**, deliberately
skipping `manual` and `seed` baselines. It answers one question — "has this
source started returning nonsense?" — which is only meaningful against what the
same source said last time. Comparing against a hand-typed rate conflated a
broken scraper with a legitimate disagreement and resolved both as *rejected*,
which swallowed the very conflict this flow exists to surface.

#### Where the official rate comes from — and why there is no scraper

An earlier version scraped `rbi.org.in`. That was wrong twice over.

RBI's site answers **HTTP 418 to every automated request**, including its own
`robots.txt`. It is not a proxy artefact and not a User-Agent problem — the site
blocks non-browser traffic outright. Getting around it would have meant
impersonating a browser, which is bot-detection evasion and a good way to have
an address banned from a site you may actually need.

More importantly, the scraper was aimed at the wrong target. **Since July 2018
the official reference rate is set by FBIL (Financial Benchmarks India), not
RBI** — RBI's page merely republishes it. And FBIL's rate is available as plain
JSON from the same keyless API already used for everything else:

```
GET https://api.frankfurter.dev/v2/rates?providers=FBIL&base=INR&quotes=USD,EUR,GBP,JPY,AED
```

So ~250 lines of ASP.NET ViewState scraping, plus the `beautifulsoup4` and
`lxml` dependencies, were deleted rather than repaired. The lesson was that the
goal was the *number*, not the *server*.

FBIL covers USD, EUR, GBP, JPY and AED — one more than RBI published. The other
seven currencies come from the general central-bank aggregate.

**FBIL publishes with a lag** of several days. The aggregate therefore fills the
most recent dates, which is why a currency can show `frankfurter` today and
`fbil` for last week. Rates are ranked `manual > fbil > frankfurter > seed`, so
a later poll can never downgrade an official rate to a market one for a date
FBIL has already covered — without that rank the badge would quietly change and
a figure someone cited as official would no longer be it.

#### Backfilling history

The live poller stores *today's* rate, and conversion picks the newest rate on
or before each transaction's date — so polling does nothing for rows already in
the database. They keep using the placeholder until you run:

```powershell
python scripts/backfill_rates.py --dry-run     # fetch and report, write nothing
python scripts/backfill_rates.py               # covers every date you have transactions on
```

Options: `--from` / `--to`, `--codes USD,EUR`, `--chunk-days 60`. The step-change
limit defaults to 100% here rather than 10%, because a multi-year span
legitimately contains moves a single day never would.

Rates are shared reference data, not per-user — see the note in
`app/models/currency.py` for why, and what would need revisiting before this
serves unrelated tenants.

### API

```
GET    /currencies                     list + latest rate for each
PUT    /currencies/{code}/rate         set a dated rate
GET    /currencies/{code}/rates        rate history
DELETE /currencies/{code}/rates/{date} undo a mistyped entry
POST   /currencies/reseed              re-insert missing currencies

GET /transactions?display_currency=actual|INR|USD|...
```

### Fixed along the way

Typing `/transactions` into the address bar returned
`{"detail":"Not authenticated"}` instead of the app, because the API router
claims that path and is registered first. A middleware now serves the SPA when
the request is a browser navigation (`Accept: text/html`, no bearer token) and
leaves API calls alone.

`format.js` is now cache-busted like `styles.css` — it is served by a plain
StaticFiles mount, so a stale copy would have made the new formatter simply
undefined at runtime.

---

## 3. The cross-border training corpus

`mlmodel/generate_fx_dataset.py` builds narrations for 12 major currencies
across 8 Indian bank advice formats (HDFC, ICICI, Axis, SBI, Kotak, generic
SWIFT, forex desk, cross-border), with RBI purpose codes, IBANs, and
beneficiaries grouped so the counterparty agrees with the purpose.

```powershell
python mlmodel/generate_fx_dataset.py --report              # stats only
python mlmodel/generate_fx_dataset.py --rows 40000 --out mlmodel/fx_dataset.csv
```

### Read these two numbers together

```
rows                 40,000
distinct templates   15,699
distinct shapes       1,505   (slot-fillers masked)
```

The original corpus multiplies 180,000 rows off **278** templates, which is why
its honest generalisation estimate is macro F1 0.7488 ± 0.0522 — the model has
seen very few distinct ways of saying anything.

*Distinct templates* is the number the trainer's held-out split actually uses.
*Distinct shapes* masks beneficiary and currency too, and is the pessimistic
reading — how many genuinely different sentence layouts exist. Both are printed
so neither can be quoted alone. 1,505 is the number to compare against 278.

### Coverage gap, stated plainly

The FX corpus covers **14 of the 18 purposes**. Food & Dining, Transportation,
Healthcare and Other are absent — nobody wires money abroad for a Swiggy order —
so those four classes still rely entirely on the original corpus.

### Retraining

Blocked until `bank_transactions_180k(1).csv` is in the PROJECT folder. Then:

```powershell
python mlmodel/train_purpose_classifier.py `
    --data "bank_transactions_180k(1).csv" mlmodel/fx_dataset.csv
```

`--data` now accepts several corpora. The report prints:

- each corpus's rows, templates and classes, and which classes appear in only one
- **per-corpus accuracy and macro F1 on the held-out split** — because one
  combined figure cannot tell you whether the new corpus taught the model
  something or simply drowned the old one out

Expect a warning. The FX generator contributes ~15,700 templates against the
original's 278, a 56:1 ratio, which means model selection and the headline macro
F1 would be decided almost entirely by cross-border phrasings. Use the per-corpus
scores, and compare against a capped run:

```powershell
python mlmodel/train_purpose_classifier.py `
    --data "bank_transactions_180k(1).csv" mlmodel/fx_dataset.csv `
    --cap-source-pairs 2000
```

Which to ship is an empirical question, and the per-corpus numbers are how you
answer it — not a default worth guessing at.

Training also now fails with a named class list, rather than an opaque sklearn
error, when a class has too few templates to survive the holdout.

---

## Tests

`tests/test_currency_display.py` — 22 tests covering the rate table, date-aware
lookup, zero-decimal currencies, round-trip drift, the FX parser's refusals, and
all four display modes through the API.

Suite: **473 passed, 7 failed** — the same 7 pre-existing failures (RPA, agent
exe, statement closing balance) that were failing before this work.


---

## 4. Dashboard: Unreconciled Ageing

The **Spending Treemap** has been replaced by an **Unreconciled Ageing**
schedule. The treemap was a third view of the same category composition already
shown by *Category Mix* and *Top Categories By Value*; nothing on the dashboard
aged the reconciliation bridge, which is the list that actually has to be
cleared before a period closes.

Items from the newest non-archived run per account, banded 0–30 / 31–60 / 61–90
/ 90+ days, with value, count, and flagged exceptions per band.

Three decisions worth knowing:

- **Bank and book are shown separately, not netted.** An aged *bank* item is
  money the bank moved that the books never recorded; an aged *book* item is
  something the books expect that never reached the bank. One combined number
  tells you the size of the problem but not which problem you have.
- **Age is measured from the reconciliation period end, not from today.** That
  is the accounting convention — a reconciliation ages as at the date it was
  drawn to — and it means the figure does not drift between page loads. The
  panel prints the as-at date.
- **Amounts are summed absolute.** A bridge item's sign encodes add-or-subtract
  against the balance, not whether it is outstanding; summing signed amounts let
  an addition cancel a subtraction and reported a band as empty while it still
  held work.

The panel is not filtered by the dashboard's date range. The bridge is a running
balance of what has not cleared, so slicing it to a 30-day window would report
"0 items over 90 days" purely because the window is 30 days wide.

Endpoint: `GET /analytics/unreconciled-ageing` (optional `bank_id`).
