# Statement Classification API — integration guide

`POST /v1/classify`. You send a bank statement **and the rules to classify it
by**; you get the classified transactions back. Nothing is stored.

This is a different service from [`/v1/analyze`](B2B_API.md), which returns a
full financial analysis (income, expenses, debt, affordability, risk). Use this
one when you own the categories and only want them applied to the rows.

```
   your statement file  ─┐
                         ├──►  POST /v1/classify  ──►  classified transactions
   your ruleset (JSON)  ─┘                             + per-rule usage report
```

---

## Contents

- [1. Authentication](#1-authentication)
- [2. The request](#2-the-request)
- [3. The ruleset](#3-the-ruleset)
- [4. How a term matches](#4-how-a-term-matches)
- [5. How a winner is chosen](#5-how-a-winner-is-chosen)
- [6. The response](#6-the-response)
- [7. Errors](#7-errors)
- [8. Limits](#8-limits)
- [9. Known limitations](#9-known-limitations)
- [10. Testing it before you deploy](#10-testing-it-before-you-deploy)

---

## 1. Authentication

Same API-key scheme as the rest of `/v1`:

```
Authorization: Bearer kl_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

The endpoint accepts a key carrying **either** `classify:write` **or**
`analyze:write`. Two consequences worth knowing:

- Every key issued before this endpoint existed already works, because they all
  carry `analyze:write`.
- You can be issued a key scoped to `classify:write` **only**. That key can
  classify and cannot reach `/v1/analyze` — it gets a `403 INSUFFICIENT_SCOPE`
  there. If this integration should not be able to pull full financial
  analysis, ask for that narrow key:

  ```bash
  curl -X POST http://localhost:8000/internal/clients/<slug>/keys \
    -H "X-Admin-Token: $B2B_ADMIN_TOKEN" -H "Content-Type: application/json" \
    -d '{"name":"classify-only","scopes":"classify:write"}'
  ```

`GET /v1/classify/schema` needs **no** key — it is the machine-readable contract
and you should be able to read it before you have credentials.

---

## 2. The request

`multipart/form-data`.

| Field | Required | Notes |
|---|---|---|
| `file` | yes | The statement. Same formats as `/v1/analyze` — see `GET /v1/formats`. |
| `rules` | yes | The ruleset, as a JSON **string**. |
| `currency` | no | ISO code, default `INR`. |
| `pdf_password` | no | For encrypted PDFs. |
| `include_transactions` | no | `false` returns the summary only. Default `true`. |
| `include_unmatched_samples` | no | `false` omits `summary.unmatched_samples`. Default `true`. |

The ruleset is validated **before** the upload is read, so a typo in a rule
costs you one fast `400` rather than a full file transfer.

```bash
curl -X POST http://localhost:8000/v1/classify \
  -H "Authorization: Bearer $KEY" \
  -F "file=@statement.csv" \
  -F 'rules={"default_category":"Unclassified","rules":[
        {"id":"fuel","category":"Fuel","priority":100,
         "match":{"any_of":["INDIAN OIL","IOCL"],"direction":"debit"}}
      ]}'
```

---

## 3. The ruleset

`GET /v1/classify/schema` returns this same reference generated from the
constants that enforce it, so it can never drift from the implementation. Read
your limits from there rather than hardcoding them.

### Top level

```json
{
  "version": "2026.09.1",
  "default_category": "Unclassified",
  "fallback": "none",
  "rules": [ ... ]
}
```

| Key | Required | Meaning |
|---|---|---|
| `rules` | yes | Array of rule objects. May be empty **only** when `fallback` is `builtin`. |
| `default_category` | no | Applied to any row no rule matched. Omit it and those rows come back with `category: null`. |
| `fallback` | no | `none` (default) or `builtin` — see below. |
| `version` | no | Free string, echoed back as `metadata.ruleset_version`. Useful for correlating output with the ruleset that produced it. |

A bare array is also accepted: `[{...}, {...}]` is treated as `{"rules": [...]}`.

### A rule

```json
{
  "id": "fuel",
  "category": "Fuel",
  "priority": 100,
  "stop": false,
  "catch_all": false,
  "match": {
    "any_of":  ["INDIAN OIL", "IOCL", "HP PETRO"],
    "all_of":  ["POS"],
    "none_of": ["REVERSAL", "REFUND"],
    "regex":   "^NEFT-[A-Z]{4}\\d{7}",
    "regex_flags": "i",
    "direction": "debit",
    "min_amount": 100,
    "max_amount": 50000,
    "min_date": "2026-01-01",
    "max_date": "2026-12-31"
  },
  "set": {
    "category_path": "Transport > Fuel",
    "counterparty": "Indian Oil",
    "tags": ["vehicle"]
  }
}
```

| Key | Required | Meaning |
|---|---|---|
| `category` | yes | What to assign. **Opaque** — see below. |
| `id` | no | Unique within the ruleset; defaults to `rule_<index>`. Echoed on every row it classifies and in `summary.rule_usage`. |
| `priority` | no | Default `50`. Highest wins. |
| `stop` | no | When a `stop: true` rule matches, evaluation ends there. Opt-in first-match. |
| `catch_all` | no | Required to be `true` for a rule with no conditions. |
| `match` | usually | The conditions. **Every condition present must hold** (AND). |
| `set` | no | Extra fields to stamp on the row. |

### Categories are opaque

Your category names are echoed back **exactly as supplied** and are never
checked against any taxonomy of ours. `GL-4100-COGS-RAW-MATERIAL` is a perfectly
good category. The only constraint is a 200-character cap.

### What `set` may write

Only these: `category_path`, `counterparty`, `merchant`, `flow_type`,
`event_type`, `transaction_method`, `tags`.

**Amounts, dates, balances and direction cannot be written by a rule.** They are
facts read off the statement, and a classification service that let a rule
rewrite an amount could return a number that appears on no document. Attempting
it is a `400 INVALID_RULES` naming the field, not a silently ignored key.

If a rule sets a `category` but no `category_path`, the path is filled with the
category, so grouping on `category_path` never sees nulls for half your rows.

### `fallback`

| Value | Behaviour |
|---|---|
| `none` (default) | Rows no rule matched get `default_category`, or `null` if you did not supply one. |
| `builtin` | Rows no rule matched are put through **our** hybrid rule+ML classifier. |

With `builtin`, those rows come back with `"method": "builtin"` and a category
from **our** taxonomy, not yours — so a single response can carry two
vocabularies. That is stated in each such row's `explanation` rather than left
for you to notice. `default_category` still applies to rows our classifier also
abstained on.

`fallback: builtin` is the only setting that loads the ML stack. With `none`,
this endpoint never imports it.

---

## 4. How a term matches

A term in `any_of` / `all_of` / `none_of` matches, case-insensitively, when:

1. it appears as a **whole word** in the narration; or
2. it appears as a run of characters **inside a single token** of at least 5
   characters.

Rule 2 is there because banks strip separators. `NAMMAYATRI` has to match
`UPI-NAMMAYATRI-99112233`, and `SERVICECHARGE` has to match a narration that
arrived without the space. Rule 1 is what stops `VI` matching inside `VIDEO`.

Matching is tried against both the normalised and the raw narration.
Normalisation strips reference numbers and long digit runs, so a term containing
digits would otherwise be unfindable even though it is plainly in the text.

`regex` is different: it is matched against the **raw, unmodified** narration,
because a regex author wants full control of the string. It is **always
case-insensitive** and is applied to the first 2000 characters. `regex_flags`
accepts a subset of `smx` (DOTALL, MULTILINE, VERBOSE) which *add* to that
case-insensitivity — they cannot switch it off, because a lowercase pattern
that silently stopped matching an uppercase narration is exactly the kind of
invisible wrong answer this service avoids.

`none_of` is a **veto**: if any of its terms matches, the rule does not fire,
regardless of how much else matched.

---

## 5. How a winner is chosen

**Every rule is evaluated, then ranked. It is not first-match.**

First-match makes a ruleset order-dependent in a way its author cannot see, and
means inserting a rule at the top silently changes the meaning of every rule
below it. So:

1. All rules that match are collected.
2. The highest `priority` wins.
3. A tie breaks on the **earlier position** in your array — so the result is
   reproducible for a given ruleset, always.
4. A tie between two rules with **different categories** is reported as
   `"ambiguous": true` with the `runner_up_rule_id`, and counted in
   `summary.ambiguous_count`. It is not silently resolved: it is a fault in the
   ruleset you need to see. A tie between rules agreeing on the category is not
   ambiguous and is not reported.

`stop: true` is the escape hatch when you do want first-match for one rule.

### Two guards against accidentally matching everything

A rule with no conditions claims every transaction. Forgetting the `match` block
is a likelier explanation than wanting that, so it is rejected unless you set
`"catch_all": true`. Same for a rule with only `none_of`, which is a catch-all in
disguise.

### Unknown keys are rejected, not ignored

A typo'd condition key under a lenient parser produces a rule that *looks*
specific and matches far more than intended — the worst failure a classifier can
have, because the output still looks plausible. So `{"any_off": [...]}` is a
`400` naming the key, not a rule that quietly matches everything.

---

## 6. The response

```json
{
  "request_id": "req_5f3c1a...",
  "status": "completed",
  "data": {
    "statement": { "period": {"start": "2026-03-01", "end": "2026-03-08"}, ... },
    "transactions": [
      {
        "date": "2026-03-02",
        "description": "UPI-SWIGGY-ORDER-8821",
        "amount": 450.0,
        "type": "DEBIT",
        "balance": 177550.0,
        "currency": "INR",
        "category": "Food",
        "category_path": "Food > Delivery",
        "counterparty": "Swiggy",
        "requires_review": false,
        "tags": ["discretionary"],
        "classification": {
          "category": "Food",
          "method": "rule",
          "rule_id": "food",
          "priority": 100,
          "matched_terms": ["SWIGGY"],
          "explanation": "Rule 'food' matched on SWIGGY → Food."
        }
      }
    ]
  },
  "summary": {
    "transaction_count": 8,
    "classified": 8,
    "unclassified": 0,
    "coverage": 1.0,
    "by_category": {"Food": 1, "Payroll": 1, "Unclassified": 2, ...},
    "by_method": {"rule": 6, "default": 2},
    "rule_count": 7,
    "rules_that_matched": 6,
    "rules_that_never_matched": ["never-fires"],
    "rule_usage": [{"rule_id": "food", "category": "Food",
                    "priority": 100, "matched": 1}],
    "ambiguous_count": 0,
    "unmatched_samples": ["SOMETHING COMPLETELY UNKNOWN"]
  },
  "quality": {
    "continuity_pass_rate": 1.0,
    "continuity_passed": true,
    "rows_checked_for_continuity": 8,
    "warnings": []
  },
  "metadata": {
    "currency": "INR", "detected_format": "csv", "filename": "statement.csv",
    "api_version": "1.0.0", "transaction_count": 8,
    "ruleset_version": "test-1", "rule_count": 7,
    "fallback": "none", "duration_ms": 12
  }
}
```

### `method`, per row

| Value | Meaning |
|---|---|
| `rule` | One of your rules matched. |
| `builtin` | Our classifier decided it (only with `fallback: builtin`). Category is from **our** taxonomy. |
| `default` | Nothing matched; `default_category` applied. |
| `none` | Nothing matched and no default was supplied. `category` is `null`. |

`requires_review` is `true` for `default` and `none`. It is also `true` when the
**parser** could not determine a row's direction, independently of how the row
was classified — a parse problem is a reason to look at a row even if a rule
labelled it confidently.

### `category_confidence` is null for your rules, and that is deliberate

A caller rule is a deterministic assertion: it either matched or it did not.
There is no probability to report, so `category_confidence` is `null` and the
`classification` block carries no `confidence` key.

The one exception is `method: "builtin"`, where a real probability exists and is
reported in both places (to 3 decimal places, identically).

Do not read a missing confidence as low confidence. If you need a numeric
certainty per row, derive it from your own `priority`, which is echoed on every
rule decision.

### The two summary fields built for iterating on a ruleset

**`rules_that_never_matched` / `rule_usage`** lists *every* rule, including the
zeroes. A rule that matches nothing is usually a typo in a term, and it is
invisible in output that only reports what did match.

**`unmatched_samples`** gives up to 25 distinct narrations nothing matched —
which is what you need to write the next rule. Suppress with
`include_unmatched_samples=false`.

---

## 7. Errors

Standard `/v1` error envelope. Switch on `error.code`, never on prose.

| Code | HTTP | Cause |
|---|---|---|
| `MISSING_RULES` | 400 | No `rules` form field. |
| `INVALID_RULES` | 400 | The ruleset is malformed. `detail` names the rule index and field. |
| `RULES_TOO_LARGE` | 413 | Ruleset over the byte limit. |
| `MISSING_FILE` | 400 | No `file` form field. |
| `FILE_TOO_LARGE` | 413 | Over the per-client cap. |
| `UNSUPPORTED_FILE_FORMAT` | 415 | See `GET /v1/formats`. |
| `PARSE_FAILED` | 422 | The file could not be read. |
| `NO_TRANSACTIONS_FOUND` | 422 | Parsed, but no transaction rows. |
| `INSUFFICIENT_SCOPE` | 403 | Key lacks `classify:write` and `analyze:write`. |
| `RATE_LIMIT_EXCEEDED` | 429 | Per-client limit. |

`INVALID_RULES` always locates the fault, because a ruleset is often
machine-generated and "it is invalid" is unactionable:

```json
{"error": {"code": "INVALID_RULES",
           "message": "'direction' must be 'debit' or 'credit'.",
           "detail": {"rule": "rules[2]", "field": "direction",
                      "received": "'sideways'"}}}
```

---

## 8. Limits

Read the live values from `GET /v1/classify/schema`.

| Limit | Value |
|---|---|
| Rules per request | 1000 |
| Ruleset JSON | 512 KB |
| Terms per clause | 200 |
| Term length | 200 chars |
| Regex length | 500 chars |
| File size | per-client, default 50 MB |

The 512 KB ruleset cap sits deliberately below Starlette's 1 MB per-part
multipart limit. At 1 MB the framework refused the field first, with a bare 400
that did not say the ruleset was the oversized part.

### Measured cost

Evaluation is `rows x rules` and scales linearly in both. Measured on a local
instance, CSV input:

| Rows | Rules | Server-side |
|---|---|---|
| 1 000 | 1 000 (the maximum) | ~1.9 s |
| 1 000 | 50 | well under 100 ms |
| 8 | 7 | 2 ms |

So the limits are reachable but the top corner is not free. If you are sending
1000-rule rulesets, most of that cost is term matching — prefer fewer, broader
rules with several terms in one `any_of` over many single-term rules, which
costs the same per term but multiplies the per-rule overhead.

---

## 9. Known limitations

Stated plainly, because each is a case where you could otherwise assume
something this service does not do.

- **Synchronous only.** There is no `async_mode` and no polling URL. Delimited,
  JSON, OFX and CAMT statements of a few thousand rows classify in tens of
  milliseconds. The one slow input is a **scanned PDF needing OCR**, which can
  take a minute and will hold the connection for that long. If you send those,
  either raise your client timeout or pre-extract them.

- **No `Idempotency-Key`.** Deliberate, not missing: the operation is a pure
  function of (file bytes, ruleset) and writes nothing, so a retry cannot
  double-apply anything. The only side effect is a usage record, so a retry
  counts twice against your quota.

- **Caller regex is bounded, not sandboxed.** Patterns are compiled with
  Python's `re`, which has no execution timeout, so a pathological pattern can
  burn CPU on this service (ReDoS). Pattern length, rule count and the span of
  text scanned are all capped, which bounds the damage; it does not eliminate
  it. Avoid nested quantifiers (`(a+)+`).

- **`fallback: builtin` mixes two vocabularies** in one response, as described
  in [§3](#fallback). Each affected row says so in its `explanation`, but if you
  aggregate `by_category` blindly you will get our names alongside yours.

- **One statement per request.** No archives — a `.zip` is a `415`. This keeps
  per-file errors, warnings and billing unambiguous.

- **The built-in classifier runs during parsing even when you do not ask for
  it.** The shared parser path invokes the internal rule/ML engine on the way
  through (`app/b2b/parsers/base.py`), and with `fallback: none` its verdict is
  discarded. Measured with no ML artifact loaded that waste is marginal — rule
  evaluation dominates — but on a deployment with a trained model present the
  per-row model inference is real work being thrown away. Removing it means
  threading a "skip classification" flag through a parser shared with
  `/v1/analyze`, which is why it has not been done here rather than left
  unnoticed.

- **Balance continuity is only checkable where the format carries a running
  balance.** OFX and CAMT do not, so `quality.continuity_passed` is `null` for
  them — never `true`. That is inherited from the shared parsers and is not
  specific to this endpoint.

- **Rule matching is textual.** It reads the narration, the amount, the date and
  the direction. It does not resolve counterparties across spellings, learn from
  corrections, or carry memory between requests — every request is independent.
  If you want that, it is what the internal categorisation stack does, reachable
  here only through `fallback: builtin`.

---

## 10. Testing it before you deploy

A runnable end-to-end check against a local server:

```bash
scripts/smoke_classify_api.sh
```

It starts from nothing: creates a client, issues a `classify:write`-only key,
posts a statement with a ruleset, and asserts the categories that come back. See
the script for what each step proves.

The automated suite is `tests/b2b/test_classify_api.py` (55 tests):

```bash
python -m pytest tests/b2b/test_classify_api.py -q
```

Those tests assert the classification each row received, not just status codes.
The two they exist to protect are `test_rule_cannot_alter_money` and
`test_priority_beats_document_order`.
