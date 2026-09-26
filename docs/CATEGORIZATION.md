# Transaction Categorization System

Hybrid rule + ML classifier that assigns one of 15 canonical categories to a
bank transaction, and abstains rather than guessing when the evidence is weak.

## Contents

- [Architecture](#architecture)
- [Category taxonomy](#category-taxonomy)
- [Rule engine](#rule-engine)
- [ML model](#ml-model)
- [Hybrid decision layer](#hybrid-decision-layer)
- [Provenance](#provenance)
- [Review queue](#review-queue)
- [Training and evaluation](#training-and-evaluation)
- [Results](#results)
- [Configuration](#configuration)
- [Known limitations](#known-limitations)

---

## Architecture

```
narration
    │
    ▼
normalizer.py ──────── uppercase, strip separators & reference numbers,
    │                  preserve merchant + semantic words
    ▼
rule_engine.py ─────── score every matching rule, rank, detect contradictions
    │                       │
    │                       └── rule_score, matched_rule, matched_terms, explanation
    ▼
ml_service.py ──────── TF-IDF + calibrated LinearSVM, top-3 probabilities
    │
    ▼
hybrid.py ──────────── decide: rule / ml / hybrid / abstain
    │
    ▼
ClassificationResult  category, requires_review, classification_method,
                      classification_confidence, explanation
```

| File | Responsibility |
|---|---|
| `app/categorization/taxonomy.py` | The 15 canonical categories + alias mapping |
| `app/categorization/normalizer.py` | Narration normalisation (shared by rules and ML) |
| `app/categorization/rules_config.py` | Rule data — merchants, phrases, keywords, tier scores |
| `app/categorization/rule_engine.py` | Matching, scoring, ranking, contradiction detection |
| `app/categorization/ml_service.py` | Model loading and prediction with top-3 |
| `app/categorization/hybrid.py` | Final decision + provenance |
| `app/categorization/config.py` | All thresholds, environment-overridable |
| `mlmodel/train_purpose_classifier.py` | **Current** training script (18 purposes) |
| `mlmodel/categorizer/purpose_data_quality.py` | Dataset checks for the purpose corpus |
| `mlmodel/train_categorizer.py` | Superseded training script (15 categories) |
| `mlmodel/evaluate_categorizer.py` | Evaluation, honest split, failure analysis |
| `app/api/review_queue.py` | List and correct uncategorised transactions |

---

## Category taxonomy

`Food & Dining`, `Groceries`, `Transportation`, `Shopping`, `Entertainment`,
`Utilities & Bills`, `Healthcare`, `Education`, `Travel`, `Rent & Housing`,
`Bank Charges`, `Salary / Income`, `Transfers`, `Investments`, `Other`.

> These 15 names are what the **rule engine** emits. Since the 18-category
> retrain the **ML model** emits the purpose taxonomy instead — see
> *Label space* under [ML model](#ml-model).

Two rules govern usage:

1. **One spelling.** Import names from `taxonomy.py`; never write them inline.
   `normalize_category()` maps legacy spellings ("food", "SALARY", "transfer")
   onto canonical form.
2. **`Uncategorized` is not a category.** It means *no decision was made* and
   always pairs with `requires_review=True`. `Other` means *a decision was made
   and none of the 14 specific categories fit*. The rule engine can never assert
   `Other` — it is only reached through absence of evidence.

---

## Rule engine

### Normalisation

```
"upi-swIGgy-ORDER-12345"  →  "UPI SWIGGY ORDER"
```

Removes separators, bank filler (`REF`, `RRN`, `UTR`, `TXN`), pure-digit runs of
4+, and long alphanumeric identifiers. Preserves merchant words and the semantic
set: `REFUND`, `REVERSAL`, `CHARGE`, `FEE`, `SALARY`, `RENT`, `TRANSFER`,
`CREDIT`, `DEBIT`, `CASH`, `WITHDRAWAL`, `INTEREST`, `BONUS`, `SIP`.

### Priority tiers

| Tier | Score | Meaning |
|---|---|---|
| `intent_phrase` | 110 | States what the transaction *is*; overrides merchant identity |
| `exact_merchant` | 100 | Merchant named at a word boundary |
| `merchant_in_token` | 90 | Merchant concatenated into a token (`UPI-NAMMAYATRI`) |
| `strong_phrase` | 80 | Unambiguous multi-word phrase |
| `phrase_in_token` | 80 | Strong phrase concatenated (`NEFT-TRANSACTIONCHARGE`) |
| `strong_keyword` | 60 | Single strongly-implying word |
| `contextual` | 40 | Combination meaningful only together |
| `weak_keyword` | 20 | Suggestive only |
| amount/direction | +10 | Boost to an *existing* candidate; never creates one |

**All rules are evaluated, then ranked by score.** This is what prevents a weak
keyword from overriding a contradictory strong signal — it is not first-match.

### Designed-in disambiguations

| Input | Result | Why |
|---|---|---|
| `SWIGGY CHARGE` | Food & Dining | `Bank Charges` is phrase-only; a bare `CHARGE` token has no rule |
| `UPI SWIGGY` | Food & Dining | Payment rails are weak keywords (20) and lose to the merchant (100) |
| `UPI TRANSFER TO SWIGGY` | Transfers | `intent_phrase` (110) outranks `exact_merchant` (100) |
| `SWIGGY REFUND` | Food & Dining | `REFUND` never decides; the merchant does |
| `BANK CHARGE REVERSAL` | Bank Charges | `BANK CHARGE` is a strong phrase |
| `ATM CASH WITHDRAWAL FEE` | Bank Charges | `CASH WITHDRAWAL FEE` phrase |

When two categories tie at the top score, the result is marked
`is_ambiguous=True` and the hybrid layer consults ML rather than trusting the
tie-break.

---

## ML model

TF-IDF (word 1–2 grams, sublinear TF) → **logistic regression** (`C=10`).
Selected over calibrated LinearSVC on mean macro F1 across repeated
template-held-out splits. `CalibratedClassifierCV` wraps the SVM candidate
because `LinearSVC` has no `predict_proba`, and the confidence threshold needs
real probabilities.

Both candidates use `class_weight="balanced"`. Training input is the **normalised
narration only** — the same `normalize_for_ml()` used at inference, so training
and serving cannot drift. No rule-engine output is used as a feature, so
evaluating the hybrid against rules is not circular.

### Label space: the model predicts PURPOSES, the rule engine does not

As of the 18-category retrain the model emits the **purpose** taxonomy from
`app/categorization/dual_taxonomy.py` — what the money was *for*:

`Sales Income`, `Other Income`, `Cost of Goods`, `Salary & Wages`,
`Rent & Premises`, `Utilities & Bills`, `Bank Fees`, `Interest & Finance Cost`,
`Taxes & Statutory`, `Professional Fees`, `Owner Funding`, `Loans & Borrowing`,
`Internal Movement`, `Food & Dining`, `Travel`, `Transportation`, `Healthcare`,
`Other`.

Both rule engines still emit their older vocabularies — the 15 names in
`taxonomy.py` (`app/categorization/rule_engine.py`) and the 28 rail/event names
in `mlmodel/rules.py` (`app/rules/rule_classifier.py`). Two consequences follow,
and neither is hidden by the code:

* In `HybridDecisionEngine`, a rule match scores 0.97 and short-circuits ML
  entirely, so rule-matched rows still come back labelled `Salary Payment` or
  `GST/Tax Payment` rather than `Salary & Wages` or `Taxes & Statutory`.
* In `hybrid.classify_transaction`, rule and ML labels are drawn from different
  vocabularies, so the `agreement="agree"` path only fires for the six names the
  two taxonomies share.

Migrating the rule engines onto purposes — `dual_taxonomy.LEGACY_MAP` already
exists for exactly this — is a separate change, because it alters rule semantics
and the expectations pinned in `tests/test_rule_integration.py`.

`app/services/category_seeder.py` seeds all three vocabularies, so every label
any classifier can emit resolves to a `Category` row rather than a NULL
`category_id`.

### Collapse detection

Training **fails with a non-zero exit and writes no artifact** if:

- any single class exceeds **40%** of predictions, or
- fewer than **60%** of taxonomy categories are ever predicted.

This is the specific failure that shipped previously: a model predicting
`Bank Charges` for nearly every narration, which passed unnoticed because only
overall accuracy was being watched.

---

## Hybrid decision layer

| Rule evidence | ML evidence | Outcome |
|---|---|---|
| score ≥ 80, not ambiguous | agrees | **hybrid** — both signals concur |
| score ≥ 80, not ambiguous | disagrees / unavailable | **rule** — deterministic wins |
| score ≥ 80 but marginal | very confident, disagrees | **abstain** — neither is safe |
| 50 ≤ score < 80 | agrees, ≥ 0.70 | **hybrid** |
| 50 ≤ score < 80 | ≥ 0.85, disagrees | **ml** |
| 50 ≤ score < 80 | weak | **abstain** |
| < 50 or none | ≥ 0.70 | **ml** |
| < 50 or none | < 0.70 | **abstain** |

Abstaining sets `category="Uncategorized"`, `requires_review=True`,
`classification_confidence=0.0`, and keeps the top-3 ML alternatives so a
reviewer sees what the model was considering.

Confidence is **derived, never hardcoded**: `rule_score / 110`, capped at 0.99.
A tier-1 merchant match and a tier-3 keyword match therefore report different
certainty — the previous engine returned 0.97 for every match regardless.

---

## Provenance

Two independent questions, two independent fields:

| Field | Question | Values |
|---|---|---|
| `Transaction.source_channel` | Where did it come from? | `upload`, `gmail`, `rpa`, `aa` |
| `Prediction.classification_method` | How was the category decided? | `rule`, `ml`, `hybrid`, `manual`, `none` |

Supporting columns on `Prediction`: `classification_rule`, `model_name`,
`model_confidence`, `rule_score`, `rule_category`, `ml_category`, `top_3`,
`explanation`, `requires_review`, `reviewed_at`, `reviewed_by_user_id`.

All are nullable — existing rows stay valid without backfill.

---

## Review queue

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/v1/review-queue` | Transactions awaiting categorisation, with live suggestions |
| GET | `/v1/review-queue/summary` | Counts for the dashboard badge |
| GET | `/v1/review-queue/categories` | Canonical list for the dropdown |
| PATCH | `/v1/review-queue/{transaction_id}` | Assign a category manually |

A manual assignment records `classification_method="manual"`, `confidence=1.0`,
the reviewing user and a timestamp, so it is never mistaken for model output and
is skipped by future reclassification runs.

### Frontend

`/review-queue` in the UI now opens on an **Uncategorized Transactions** tab
(`CategorizationReviewTab` in `app/static/index.html`), alongside the existing
duplicate and BRS tabs. Each row shows the narration, amount, the classifier's
reason for abstaining, the top-3 suggestions as one-click buttons, and a
dropdown of all 15 categories with a Save button. Resolved rows disappear
immediately and the tab badge decrements; paging is 25 at a time.

To populate it with realistic difficult cases for testing:

```bash
python -m scripts.seed_review_queue_demo --report-only   # classify only, write nothing
python -m scripts.seed_review_queue_demo                 # seed an isolated demo user
```

The demo script writes only to its own `review.demo@kredo.local` user and never
touches existing ledger rows.

---

## Training and evaluation

```bash
python -m mlmodel.train_purpose_classifier --data path/to/bank_transactions_180k.csv
```

Artifacts land in `mlmodel/artifacts/`: `categorizer_model.joblib`,
`model_metadata.json`, `evaluation_report.json`.

**Splits hold out whole narration templates, not random rows.** The corpus is
generated from a few hundred sentence templates with randomised reference
numbers; because `normalize_for_ml()` strips those numbers before fitting, a
random row split puts the *same string* on both sides and returns accuracy
1.0000. That figure is recorded in `model_metadata.json` as
`random_split_reference` and is explicitly not accuracy.

Model selection averages macro F1 over 10 template-held-out splits — a single
split of ~55 templates swung between 0.59 and 0.83 macro F1 across seeds, wide
enough to select the wrong candidate. The reported test split is
template-disjoint, proven so by `check_template_split`, and used once.

The shipped model is refit on every distinct `(text, category)` pair, so
production coverage includes the templates held out for measurement.

### Why training deduplicates

The 180,000 rows contain ~278 distinct normalised strings. Fitting on all rows
adds no information but makes the model far more confident, and the training
script re-measures this on every run:

| Training set | Mean confidence on unseen templates | Auto-decides at 0.80 | Correct when it does |
|---|---|---|---|
| 278 distinct pairs | 0.509 | 16.7% | **97.1%** |
| all 180,000 rows | 0.845 | 74.2% | 87.2% |

Fitting on all rows would auto-book roughly one wrong category in eight. For
labels that land in a ledger, abstaining on an unfamiliar narration is worth
more than the extra coverage, so the deduplicated fit ships.

---

## Results

Dataset: 180,000 synthetic labelled transactions, 18 balanced classes
(10,000 rows each) — which collapse to **278 distinct narration templates**,
10–20 per class. The corpus teaches 278 lessons, not 180,000.

### Model (`tfidf_logreg`)

| Measurement | Accuracy | Macro P | Macro R | Macro F1 |
|---|---|---|---|---|
| Held-out templates, 10-split mean | 0.7545 ± 0.0529 | — | — | **0.7488 ± 0.0522** |
| Held-out templates, reported split (55 unseen) | 0.7818 | 0.8222 | 0.7778 | **0.7690** |
| Random row split | 1.0000 | — | — | 1.0000 |

**The 0.75 figure is the model's skill; the 1.0000 is its memory.** Read the
first row, not the third.

Collapse check: top class 10.9% of predictions, 18/18 purposes predicted.

### Behaviour at the confidence gates

Measured on unseen phrasings, averaged over 10 template-held-out splits:

| Threshold | Auto-decides | Correct when it does |
|---|---|---|
| ≥ 0.70 (`ML_CONFIDENCE_ACCEPT`) | 28.5% | 96.7% |
| ≥ 0.80 (`CONFIDENCE_THRESHOLD`) | 17.6% | 95.5% |
| ≥ 0.85 (`ML_CONFIDENCE_STRONG`) | 13.3% | 95.2% |

On the 278 templates it was fitted on, the model is exact. The table above
describes the case that matters in production: a narration phrased in a way the
training corpus never contained. It commits to roughly one in six of those and
is right ~96% of the time when it does; the rest reach the review queue.

### End-to-end system

> **Stale.** The figures below were measured on the superseded 15-category model
> and its 10k dataset. They have **not** been re-measured since the 18-purpose
> retrain, and the rule/ML label spaces now differ (see *Label space* above),
> which changes the `agreement` paths these numbers came from. Re-run
> `mlmodel/evaluate_categorizer.py` against the purpose taxonomy before quoting
> anything here.

| Metric | Value (superseded model) |
|---|---|
| Rule engine fires on | 89.2% of transactions |
| Rule accuracy when it fires | 92.2% |
| ML accuracy standalone | 95.1% |
| **Auto-decided** | **94.8%** |
| **Accuracy when auto-decided** | **100.0%** |
| Routed to manual review | 82 / 1567 (5.2%) |

By method: `hybrid` 1117 rows @ 100%, `rule` 45 @ 100%, `ml` 323 @ 100%.

The out-of-distribution guard (below) trades 1.6 points of auto-decide rate for
the elimination of every remaining silent error on this split. For a ledger that
is the right direction: a transaction in the review queue costs a few seconds of
attention, a silently miscategorised one corrupts a report.

### Out-of-distribution guard

A calibrated probability describes confidence *given the training distribution*.
It says nothing about input the model has never seen, and this model was measured
being confidently wrong on unfamiliar narrations:

| Narration | Model said | Recognised terms |
|---|---|---|
| `DOCTOR CONSULTATION FEE PAID` | Bank Charges @ **0.98** | 1 of 4 (`fee`) |
| `COURIER HANDLING CHARGE` | Bank Charges @ **0.97** | 1 of 3 (`charge`) |
| `UPI-SRI LAKSHMI TRADERS-8871` | Transfers @ **0.85** | 1 of 4 (`upi`) |
| `NEFT-M/S RAGHAV AND SONS` | Transfers @ **0.93** | 1 of 6 (`neft`) |
| `RTGS-UNITED VENTURES LIMITED` | Transfers @ **0.96** | 1 of 4 (`rtgs`) |

Every one is the model extrapolating from a single generic token. Correct
predictions, by contrast, had two or more recognised terms and ≥ 0.5 coverage.

`ml_service.py` therefore reports `known_token_count` and `vocab_coverage`
alongside the probability, and `hybrid.py` refuses to admit the model's opinion
below `ML_MIN_KNOWN_TOKENS` (2) and `ML_MIN_VOCAB_COVERAGE` (0.5) — regardless
of how confident it claims to be. All five rows above now route to review.

### Where each approach wins

**Rules beat ML (45 cases)** — concatenated merchants the model reads as a
payment rail: `POS-MYNTRADESIGNS-836595`, `UPI-NAMMAYATRI`, `BWSSBINDIA`.

**ML beats rules (105 cases)** — merchants absent from the rule config, where
only a weak rail keyword matched: `UPI/STEAM/474122`, `POS TEXTBOOK STORE REFUND`.

**Requiring review (56)** — genuinely uninformative narrations:
`ONLINESERVICEINDIA`, `PVRONLINE`, `HOUSEMAINTENANCEONLINE`.

---

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `RULE_SCORE_HIGH` | 80 | At/above, rule is trusted outright |
| `RULE_SCORE_MEDIUM` | 50 | Below, rule is ignored as a decision |
| `ML_CONFIDENCE_ACCEPT` | 0.70 | Minimum to accept an ML prediction |
| `ML_CONFIDENCE_STRONG` | 0.85 | Above, ML can outweigh a medium rule |
| `ML_MIN_KNOWN_TOKENS` | 2 | Recognised terms required before ML is trusted |
| `ML_MIN_VOCAB_COVERAGE` | 0.5 | Fraction of terms the model must recognise |
| `PREFER_RULE_ON_TIE` | true | Deterministic rules win ties |
| `CATEGORIZER_ARTIFACT_DIR` | `mlmodel/artifacts` | Model location |

Raising `ML_CONFIDENCE_ACCEPT` sends more to review and reduces silent errors;
lowering it does the reverse. Tune on validation, never on test.

---

## Known limitations

**The reported metrics are not production accuracy.** The dataset is synthetic
and, far more importantly, thin: 180,000 rows carry only 278 distinct lessons,
10–20 phrasings per class. The template-held-out figure (macro F1 0.749 ± 0.052)
is the honest estimate, and even it is measured on generated text. The ± 0.052
is real — the corpus is small enough that the number moves meaningfully with the
split. **Re-measure on anonymised real transactions before treating any of this
as a production number.**

**More templates beat more rows.** Adding another 180,000 rows from the same
generator would not move the model at all, since it already sees every distinct
string. Ten additional *phrasings* per class would. If the corpus is
regenerated, vary sentence structure and merchant names, not reference numbers.

**The rule config is India-centric.** Merchant lists cover Indian banking and
e-commerce. Other markets need their own `rules_config.py` entries.

**Existing ledger data is untouched.** Nothing reclassifies historical rows. A
migration to apply the new classifier to existing transactions is a separate,
explicitly-approved job — see `scripts/db_cleanup.py` for the dry-run pattern.

**`SWIGGY INSTAMART` is genuinely ambiguous.** It matches both a Food & Dining
merchant and a Groceries merchant at score 100. The engine flags it ambiguous
and defers to ML rather than picking arbitrarily.
