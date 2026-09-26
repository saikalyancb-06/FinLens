"""Train the transaction PURPOSE classifier (18-category business taxonomy).

Usage:
    python -m mlmodel.train_purpose_classifier --data path/to/bank_transactions_180k.csv

This supersedes `train_categorizer.py`, which trained the 15-name personal-finance
taxonomy. The label space here is `app.categorization.dual_taxonomy.PURPOSES` —
the axis a P&L is actually built from.

Three decisions in this script are not the obvious ones, and each is measured
rather than assumed:

1. EVALUATION IS TEMPLATE-HELD-OUT, NOT ROW-RANDOM.
   The 180k corpus is built from ~278 sentence templates with randomised
   reference numbers. Once reference numbers are stripped by the shared
   normaliser, a random row split puts the *same string* on both sides, and
   accuracy comes back at ~1.00. That number is memorisation, not skill. Every
   headline figure here comes from splits that hold out whole templates, so the
   model is scored on phrasings it has genuinely never seen. The row-random
   figure is still computed, and labelled as the memorisation ceiling.

2. SELECTION USES REPEATED CV, NOT ONE SPLIT.
   Holding out 20% of templates leaves ~55 test items. Single-split macro F1
   swung between 0.59 and 0.83 across seeds during development — wide enough to
   pick the wrong model. Selection therefore averages over REPEATS seeds.

3. TRAINING DEDUPLICATES TO DISTINCT (text, label) PAIRS.
   The 180k rows contain ~278 distinct normalised strings. Fitting on all rows
   does not add information, but it does make the model far more confident:
   measured on held-out templates, full-row training reported a mean top
   probability of 0.85-0.91 and auto-decided 74-86% of unseen phrasings at
   only 84-87% accuracy. Deduplicated training reported ~0.51, auto-decided
   ~17% and was ~97% accurate when it did. For a classifier whose output is
   booked into a ledger, abstaining on an unfamiliar narration is worth far
   more than confidently mislabelling one in eight. The comparison is printed
   at the end of the run so the choice stays auditable.

Training FAILS (non-zero exit) rather than writing an artifact if the label space
is wrong or the model collapses onto a dominant class.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC

# Allow running as a plain script from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.categorization.dual_taxonomy import PURPOSES  # noqa: E402
from app.categorization.normalizer import normalize_for_ml  # noqa: E402
from mlmodel.categorizer.purpose_data_quality import (  # noqa: E402
    check_dataset,
    check_template_split,
    format_report,
    narration_template,
)

RANDOM_STATE = 42
ARTIFACT_DIR = os.path.join(os.path.dirname(__file__), "artifacts")

# Repeats for template-held-out cross-validation during model selection.
CV_REPEATS = 10
MIN_TEMPLATES_PER_CLASS = 3  # the calibrator's internal CV folds
HOLDOUT_FRACTION = 0.20

# Collapse thresholds. The corpus is balanced at 10k rows per class, so a model
# sending more than 40% of predictions to one class has degenerated.
COLLAPSE_MAX_SINGLE_CLASS_SHARE = 0.40
COLLAPSE_MIN_CLASSES_PREDICTED = 0.60  # fraction of the taxonomy that must appear

# Confidence gates the runtime actually applies (app.categorization.config and
# settings.CONFIDENCE_THRESHOLD). Reporting behaviour at these exact values is
# what tells us how the model will behave in the pipeline, as opposed to how it
# scores in the abstract.
RUNTIME_THRESHOLDS = (0.70, 0.80, 0.85)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_dataset(paths: List[str] | str) -> pd.DataFrame:
    """Load one or more labelled CSVs into a single frame.

    `normalize_for_ml` is the same function the inference path calls, so the
    strings fitted here are byte-for-byte the strings predicted on later.

    Multiple paths are concatenated and tagged with a `source` column. That tag
    is not a feature - it never reaches the vectoriser - but it is what lets the
    report below score the domestic and cross-border corpora separately. A
    combined macro F1 can hide a model that learned the new corpus by forgetting
    the old one, and that failure is exactly what adding a second dataset risks.
    """
    if isinstance(paths, str):
        paths = [paths]

    frames = []
    for path in paths:
        part = pd.read_csv(path)
        part["source"] = os.path.basename(path)
        frames.append(part)
    df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    if "transaction_description" not in df.columns and "narration" in df.columns:
        df = df.rename(columns={"narration": "transaction_description"})

    df = df.dropna(subset=["transaction_description", "category"])
    df["transaction_description"] = df["transaction_description"].astype(str)
    df = df[df["transaction_description"].str.strip() != ""]

    df["text"] = df["transaction_description"].map(normalize_for_ml)
    df = df[df["text"].str.strip() != ""]
    df["template"] = df["transaction_description"].map(narration_template)
    return df.reset_index(drop=True)


def source_overlap(df: pd.DataFrame) -> Dict[str, Any]:
    """Templates shared between corpora, and each corpus's class coverage.

    A template present in two sources is not a problem, but a class present in
    only one is: it means the held-out split for that class is drawn from a
    single generator, so its score says nothing about the other.
    """
    if df["source"].nunique() < 2:
        return {}
    by_source = {}
    for src, sub in df.groupby("source"):
        by_source[src] = {
            "rows": int(len(sub)),
            "distinct_templates": int(sub["template"].nunique()),
            "classes": sorted(sub["category"].unique().tolist()),
        }
    template_sets = {s: set(v) for s, v in df.groupby("source")["template"]}
    names = sorted(template_sets)
    shared = set.intersection(*template_sets.values())
    only = {s: sorted(set(df[df["source"] == s]["category"]) -
                      set(df[df["source"] != s]["category"])) for s in names}
    return {
        "per_source": by_source,
        "templates_shared_between_sources": len(shared),
        "classes_unique_to_one_source": {k: v for k, v in only.items() if v},
    }


def distinct_pairs(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse to distinct (text, category) pairs — see decision 3 in the docstring."""
    cols = ["text", "category", "template"] + (["source"] if "source" in df.columns else [])
    return df.drop_duplicates(subset=["text", "category"])[cols].reset_index(drop=True)


def cap_source_pairs(pairs: pd.DataFrame, cap: int, seed: int) -> pd.DataFrame:
    """Limit how many distinct pairs any one corpus contributes.

    Combining corpora of very different template density is not neutral. The
    cross-border generator produces ~15,000 distinct templates; the domestic
    corpus produces a few hundred. Left uncapped, the domestic corpus is 2% of
    what the model fits on, it is 2% of what the held-out split scores, and both
    the selected model and the headline macro F1 are decided almost entirely by
    cross-border phrasings - while the report still reads as if it covered both.

    Capping samples per (source, class) so the trim does not quietly delete a
    thin class. It is off by default: the right cap is an empirical question,
    and the per-source scores printed above are how you answer it.
    """
    if cap <= 0 or "source" not in pairs.columns:
        return pairs
    rng = np.random.RandomState(seed)
    keep = []
    for src, sub in pairs.groupby("source"):
        if len(sub) <= cap:
            keep.append(sub)
            continue
        share = cap / len(sub)
        for _, cls_sub in sub.groupby("category"):
            n = max(MIN_TEMPLATES_PER_CLASS * 2, int(round(len(cls_sub) * share)))
            n = min(n, len(cls_sub))
            idx = rng.choice(cls_sub.index.values, size=n, replace=False)
            keep.append(cls_sub.loc[sorted(idx)])
    return pd.concat(keep).sort_index().reset_index(drop=True)


def split_templates(
    pairs: pd.DataFrame, fraction: float, seed: int
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Hold out `fraction` of each class's templates.

    Splitting per class rather than globally guarantees every class appears on
    both sides; a global shuffle can strip a thin class out of training entirely
    and turn its recall into a coin flip.
    """
    rng = np.random.RandomState(seed)
    held: set = set()
    for _, sub in pairs.groupby("category"):
        templates = sorted(sub["template"].unique())
        rng.shuffle(templates)
        n_hold = max(1, round(len(templates) * fraction))
        held.update(templates[:n_hold])

    test = pairs[pairs["template"].isin(held)]
    train = pairs[~pairs["template"].isin(held)]
    return train, test


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def build_models() -> Dict[str, Pipeline]:
    """Candidate models. Both are linear and inspectable by design.

    The vectoriser step MUST stay a plain `TfidfVectorizer` named "tfidf":
    `MLCategorizerService` reads `named_steps["tfidf"].vocabulary_` to run its
    out-of-distribution guard, and a FeatureUnion there silently yields an empty
    vocabulary, which switches the guard off without any error. A word+char
    union was measured during development and scored 0.71-0.72 macro F1 against
    0.74-0.75 for words alone, so nothing is being given up here.
    """
    def vectorizer() -> TfidfVectorizer:
        return TfidfVectorizer(
            analyzer="word",
            ngram_range=(1, 2),
            sublinear_tf=True,
            min_df=1,
            lowercase=True,
        )

    return {
        "tfidf_logreg": Pipeline([
            ("tfidf", vectorizer()),
            ("clf", LogisticRegression(
                max_iter=2000,
                C=10.0,
                class_weight="balanced",
                random_state=RANDOM_STATE,
            )),
        ]),
        # LinearSVC has no predict_proba, so it is wrapped in a calibrator to
        # produce the probabilities the confidence thresholds are applied to.
        "tfidf_linearsvm_calibrated": Pipeline([
            ("tfidf", vectorizer()),
            ("clf", CalibratedClassifierCV(
                LinearSVC(C=1.0, class_weight="balanced", random_state=RANDOM_STATE),
                method="sigmoid",
                cv=3,
            )),
        ]),
    }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate(model: Pipeline, X, y_true, label: str) -> Dict[str, Any]:
    y_pred = model.predict(X)
    labels = sorted(set(list(y_true) + list(y_pred)))
    return {
        "split": label,
        "n": int(len(y_true)),
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 4),
        "macro_precision": round(float(precision_score(y_true, y_pred, average="macro", zero_division=0)), 4),
        "macro_recall": round(float(recall_score(y_true, y_pred, average="macro", zero_division=0)), 4),
        "macro_f1": round(float(f1_score(y_true, y_pred, average="macro", zero_division=0)), 4),
        "weighted_f1": round(float(f1_score(y_true, y_pred, average="weighted", zero_division=0)), 4),
        "per_class": classification_report(y_true, y_pred, output_dict=True, zero_division=0),
        "confusion_matrix": {
            "labels": labels,
            "matrix": confusion_matrix(y_true, y_pred, labels=labels).tolist(),
        },
        "prediction_distribution": pd.Series(y_pred).value_counts().to_dict(),
    }


def threshold_behaviour(model: Pipeline, X, y_true) -> Dict[str, Dict[str, float]]:
    """How the model behaves at the confidence gates the runtime applies.

    `auto_decide_rate` is the share of inputs it would commit to; `accuracy_when_auto`
    is how often it is right when it does. The second number is the one that
    matters: everything below the gate goes to the review queue, which is a cost,
    while a wrong auto-decision is a mislabelled ledger entry.
    """
    proba = model.predict_proba(X)
    conf = proba.max(axis=1)
    pred = np.asarray(model.classes_)[proba.argmax(axis=1)]
    correct = pred == np.asarray(y_true)

    out: Dict[str, Dict[str, float]] = {}
    for th in RUNTIME_THRESHOLDS:
        sel = conf >= th
        out[f"{th:.2f}"] = {
            "auto_decide_rate": round(float(sel.mean()), 4),
            "accuracy_when_auto": round(float(correct[sel].mean()), 4) if sel.any() else None,
            "n_auto": int(sel.sum()),
        }
    out["mean_top_probability"] = round(float(conf.mean()), 4)
    return out


def check_collapse(prediction_distribution: Dict[str, int], total: int) -> Dict[str, Any]:
    """Detect the failure mode where the model predicts one class for everything."""
    if total == 0:
        return {"collapsed": True, "reason": "no predictions", "problems": ["no predictions"]}

    top_class, top_count = max(prediction_distribution.items(), key=lambda kv: kv[1])
    top_share = top_count / total
    classes_predicted = len(prediction_distribution)
    coverage = classes_predicted / len(PURPOSES)

    problems: List[str] = []
    if top_share > COLLAPSE_MAX_SINGLE_CLASS_SHARE:
        problems.append(
            f"'{top_class}' accounts for {top_share*100:.1f}% of predictions "
            f"(limit {COLLAPSE_MAX_SINGLE_CLASS_SHARE*100:.0f}%)"
        )
    if coverage < COLLAPSE_MIN_CLASSES_PREDICTED:
        problems.append(
            f"only {classes_predicted}/{len(PURPOSES)} purposes ever predicted "
            f"({coverage*100:.0f}%, minimum {COLLAPSE_MIN_CLASSES_PREDICTED*100:.0f}%)"
        )

    return {
        "collapsed": bool(problems),
        "top_class": top_class,
        "top_class_share": round(top_share, 4),
        "classes_predicted": classes_predicted,
        "problems": problems,
    }


def repeated_template_cv(
    name: str, pairs: pd.DataFrame, repeats: int, fraction: float
) -> Dict[str, Any]:
    """Average held-out-template performance over `repeats` different splits."""
    accs, f1s, confs = [], [], []
    auto_rates: Dict[str, List[float]] = {f"{t:.2f}": [] for t in RUNTIME_THRESHOLDS}
    auto_accs: Dict[str, List[float]] = {f"{t:.2f}": [] for t in RUNTIME_THRESHOLDS}

    for seed in range(repeats):
        train, test = split_templates(pairs, fraction, seed)
        model = build_models()[name]
        model.fit(train["text"], train["category"])
        pred = model.predict(test["text"])
        accs.append(accuracy_score(test["category"], pred))
        f1s.append(f1_score(test["category"], pred, average="macro", zero_division=0))

        behaviour = threshold_behaviour(model, test["text"], test["category"])
        confs.append(behaviour["mean_top_probability"])
        for t in RUNTIME_THRESHOLDS:
            key = f"{t:.2f}"
            auto_rates[key].append(behaviour[key]["auto_decide_rate"])
            if behaviour[key]["accuracy_when_auto"] is not None:
                auto_accs[key].append(behaviour[key]["accuracy_when_auto"])

    return {
        "repeats": repeats,
        "holdout_fraction": fraction,
        "accuracy_mean": round(float(np.mean(accs)), 4),
        "accuracy_sd": round(float(np.std(accs)), 4),
        "macro_f1_mean": round(float(np.mean(f1s)), 4),
        "macro_f1_sd": round(float(np.std(f1s)), 4),
        "mean_top_probability": round(float(np.mean(confs)), 4),
        "thresholds": {
            f"{t:.2f}": {
                "auto_decide_rate": round(float(np.mean(auto_rates[f"{t:.2f}"])), 4),
                "accuracy_when_auto": (
                    round(float(np.mean(auto_accs[f"{t:.2f}"])), 4)
                    if auto_accs[f"{t:.2f}"] else None
                ),
            }
            for t in RUNTIME_THRESHOLDS
        },
    }


def duplication_comparison(df: pd.DataFrame, pairs: pd.DataFrame, name: str, repeats: int) -> Dict[str, Any]:
    """Re-measure decision 3 on this dataset instead of trusting the docstring.

    Trains the selected configuration twice per split — once on distinct pairs,
    once on all rows — and compares behaviour at the runtime gates.
    """
    result: Dict[str, Any] = {}
    for mode in ("deduplicated", "all_rows"):
        rates, accs_at, confs = [], [], []
        for seed in range(repeats):
            _, test = split_templates(pairs, HOLDOUT_FRACTION, seed)
            held = set(test["template"])
            if mode == "deduplicated":
                train = pairs[~pairs["template"].isin(held)]
            else:
                train = df[~df["template"].isin(held)]
            model = build_models()[name]
            model.fit(train["text"], train["category"])
            behaviour = threshold_behaviour(model, test["text"], test["category"])
            rates.append(behaviour["0.80"]["auto_decide_rate"])
            if behaviour["0.80"]["accuracy_when_auto"] is not None:
                accs_at.append(behaviour["0.80"]["accuracy_when_auto"])
            confs.append(behaviour["mean_top_probability"])
        result[mode] = {
            "train_rows": int(len(pairs) if mode == "deduplicated" else len(df)),
            "mean_top_probability_on_unseen_templates": round(float(np.mean(confs)), 4),
            "auto_decide_rate_at_0.80": round(float(np.mean(rates)), 4),
            "accuracy_when_auto_at_0.80": round(float(np.mean(accs_at)), 4) if accs_at else None,
        }
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, nargs="+",
                        help="One or more labelled CSVs. Multiple paths are "
                             "concatenated and scored separately in the report.")
    parser.add_argument("--out", default=ARTIFACT_DIR, help="Artifact output directory")
    parser.add_argument("--cv-repeats", type=int, default=CV_REPEATS,
                        help="Template-held-out splits used for model selection")
    parser.add_argument("--cap-source-pairs", type=int, default=0,
                        help="Cap the distinct pairs any one corpus contributes "
                             "(0 = uncapped). Use when one dataset has far more "
                             "template variety than another.")
    parser.add_argument("--skip-duplication-check", action="store_true",
                        help="Skip the deduplicated-vs-all-rows comparison (slow)")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    # ---- 1. Load & quality-check -----------------------------------------
    df = load_dataset(args.data)
    report = check_dataset(df)
    print(format_report(report))

    if report["fatal"]:
        print("\nAborting: dataset has fatal quality problems.")
        return 1

    overlap = source_overlap(df)
    if overlap:
        print("\nCorpora combined:")
        for src, st in overlap["per_source"].items():
            print(f"   {src:<34} {st['rows']:>7,} rows  "
                  f"{st['distinct_templates']:>6,} templates  "
                  f"{len(st['classes']):>2} classes")
        print(f"   templates shared between corpora: "
              f"{overlap['templates_shared_between_sources']}")
        for src, classes in overlap["classes_unique_to_one_source"].items():
            print(f"   only in {src}: {', '.join(classes)}")

    pairs = distinct_pairs(df)
    if args.cap_source_pairs:
        before = len(pairs)
        pairs = cap_source_pairs(pairs, args.cap_source_pairs, RANDOM_STATE)
        print(f"\nSource cap {args.cap_source_pairs:,}: {before:,} -> {len(pairs):,} pairs")
    if "source" in pairs.columns and pairs["source"].nunique() > 1:
        counts = pairs["source"].value_counts()
        ratio = counts.max() / max(1, counts.min())
        if ratio > 10:
            print(f"\n   WARNING: {counts.idxmax()} contributes {ratio:.0f}x more "
                  f"distinct pairs than {counts.idxmin()}. Model selection and the "
                  f"headline macro F1 are therefore decided almost entirely by "
                  f"{counts.idxmax()}. Read the per-source scores below, not the "
                  f"combined figure, and consider --cap-source-pairs.")
    print(f"\n{len(df)} rows -> {len(pairs)} distinct (text, category) pairs used for fitting.")

    # ---- 1b. Pre-flight: enough templates per class to survive a split ----
    # The calibrated candidates run an internal 3-fold CV, so a class left with
    # fewer than 3 training examples after the holdout dies inside sklearn with
    # a message that names no class. Failing here instead names the class and
    # says what to do about it.
    min_train = MIN_TEMPLATES_PER_CLASS
    thin = {
        cat: int(n) for cat, n in pairs["category"].value_counts().items()
        if n - max(1, round(n * HOLDOUT_FRACTION)) < min_train
    }
    if thin:
        print(f"\nAborting: these classes have too few distinct templates to "
              f"survive a {HOLDOUT_FRACTION*100:.0f}% holdout and the calibrator's "
              f"internal {min_train}-fold CV:")
        for cat, n in sorted(thin.items(), key=lambda kv: kv[1]):
            print(f"   {cat:<26} {n} templates")
        print("Add narration variety for these classes, or drop them from the "
              "label space. Multiplying rows off the same phrasings will not help.")
        return 4

    # ---- 2. Model selection on repeated template-held-out CV -------------
    print(f"\nModel selection: {args.cv_repeats} splits, "
          f"{HOLDOUT_FRACTION*100:.0f}% of each class's templates held out each time.")
    cv_results: Dict[str, Any] = {}
    for name in build_models():
        cv = repeated_template_cv(name, pairs, args.cv_repeats, HOLDOUT_FRACTION)
        cv_results[name] = cv
        print(f"   {name:<28} macro_f1 = {cv['macro_f1_mean']:.4f} "
              f"(sd {cv['macro_f1_sd']:.4f})   accuracy = {cv['accuracy_mean']:.4f} "
              f"(sd {cv['accuracy_sd']:.4f})")

    best_name = max(cv_results, key=lambda n: cv_results[n]["macro_f1_mean"])
    best_cv = cv_results[best_name]
    print(f"\nSelected model: {best_name} "
          f"(mean held-out-template macro F1 = {best_cv['macro_f1_mean']:.4f})")

    # ---- 3. Reported test split: templates held out, used once -----------
    train_pairs, test_pairs = split_templates(pairs, HOLDOUT_FRACTION, seed=RANDOM_STATE)
    split_check = check_template_split(train_pairs, test_pairs)
    print("\nTemplate-disjointness of the reported split:")
    for k, v in split_check.items():
        print(f"   {k}: {v}")
    if split_check["overlapping_templates"]:
        print("\nAborting: the held-out split is not template-disjoint.")
        return 3

    eval_model = build_models()[best_name]
    eval_model.fit(train_pairs["text"], train_pairs["category"])
    test_metrics = evaluate(eval_model, test_pairs["text"], test_pairs["category"], "test_unseen_templates")
    test_metrics["threshold_behaviour"] = threshold_behaviour(
        eval_model, test_pairs["text"], test_pairs["category"]
    )

    # ---- 3b. Per-source scores on the same held-out split -----------------
    # One combined macro F1 cannot tell you whether the second corpus taught the
    # model something or simply drowned the first one out. These are the same
    # predictions, partitioned by which generator produced the phrasing.
    per_source_metrics: Dict[str, Any] = {}
    if "source" in test_pairs.columns and test_pairs["source"].nunique() > 1:
        preds = eval_model.predict(test_pairs["text"])
        print("\nHeld-out-template scores, split by corpus:")
        for src, idx in test_pairs.groupby("source").groups.items():
            mask = test_pairs.index.isin(idx)
            y_true = test_pairs.loc[mask, "category"]
            y_pred = preds[mask]
            per_source_metrics[src] = {
                "n": int(mask.sum()),
                "accuracy": round(float(accuracy_score(y_true, y_pred)), 4),
                "macro_f1": round(float(f1_score(y_true, y_pred, average="macro",
                                                 zero_division=0)), 4),
            }
            m = per_source_metrics[src]
            print(f"   {src:<34} n={m['n']:<5} accuracy={m['accuracy']:.4f}  "
                  f"macro F1={m['macro_f1']:.4f}")

    # ---- 4. Row-random split, for reference only -------------------------
    # This is the number a naive script would report. It is kept so the gap
    # between memorisation and generalisation is visible in one place.
    Xr_tr, Xr_te, yr_tr, yr_te = train_test_split(
        df["text"], df["category"], test_size=0.15, stratify=df["category"], random_state=RANDOM_STATE
    )
    ref_model = build_models()[best_name]
    ref_model.fit(Xr_tr, yr_tr)
    random_split_metrics = {
        "accuracy": round(float(accuracy_score(yr_te, ref_model.predict(Xr_te))), 4),
        "macro_f1": round(float(f1_score(yr_te, ref_model.predict(Xr_te), average="macro", zero_division=0)), 4),
        "note": (
            "Row-random split. Reference numbers are stripped before fitting, so "
            "the same normalised string appears on both sides and this measures "
            "recall of memorised templates, not generalisation. Do not quote it "
            "as accuracy."
        ),
    }

    # ---- 5. Collapse check on unseen templates ---------------------------
    collapse = check_collapse(test_metrics["prediction_distribution"], len(test_pairs))
    print("\nDegenerate-model check (held-out templates):")
    if collapse["collapsed"]:
        for p in collapse["problems"]:
            print(f"   FAIL: {p}")
        print("\nAborting: model collapsed onto a dominant class. Artifact NOT written.")
        return 2
    print(f"   OK: top class '{collapse['top_class']}' at {collapse['top_class_share']*100:.1f}%, "
          f"{collapse['classes_predicted']}/{len(PURPOSES)} purposes predicted")

    # ---- 6. Report --------------------------------------------------------
    print("\n" + "=" * 74)
    print("TEST METRICS — held-out templates, split used once after selection")
    print("=" * 74)
    print(f"test items      : {test_metrics['n']} unseen templates")
    print(f"accuracy        : {test_metrics['accuracy']}")
    print(f"macro precision : {test_metrics['macro_precision']}")
    print(f"macro recall    : {test_metrics['macro_recall']}")
    print(f"macro F1        : {test_metrics['macro_f1']}")
    print(f"weighted F1     : {test_metrics['weighted_f1']}")
    print()
    print(classification_report(test_pairs["category"], eval_model.predict(test_pairs["text"]), zero_division=0))

    print("Behaviour at the runtime confidence gates (mean over "
          f"{args.cv_repeats} template-held-out splits):")
    for th, vals in best_cv["thresholds"].items():
        acc = vals["accuracy_when_auto"]
        print(f"   >= {th}: auto-decides {vals['auto_decide_rate']*100:5.1f}% of unseen "
              f"phrasings, correct {acc*100:.1f}% of the time" if acc is not None
              else f"   >= {th}: auto-decides {vals['auto_decide_rate']*100:5.1f}% (never fired)")

    print(f"\nRow-random split (memorisation ceiling, NOT accuracy): "
          f"{random_split_metrics['accuracy']}")

    # ---- 7. Why deduplicated training ------------------------------------
    dup_comparison = None
    if not args.skip_duplication_check:
        print("\nDeduplicated vs all-rows training (both scored on unseen templates):")
        dup_comparison = duplication_comparison(df, pairs, best_name, repeats=min(5, args.cv_repeats))
        for mode, vals in dup_comparison.items():
            acc = vals["accuracy_when_auto_at_0.80"]
            print(f"   {mode:<14} rows={vals['train_rows']:<7} "
                  f"mean confidence on unseen={vals['mean_top_probability_on_unseen_templates']:.3f}  "
                  f"auto-decides {vals['auto_decide_rate_at_0.80']*100:.1f}% at 0.80, "
                  f"correct {acc*100:.1f}%" if acc is not None else "")

    # ---- 8. Refit on every template and persist --------------------------
    # The shipped model sees all 278 templates: withholding a fifth of the
    # vocabulary from production to preserve a test split would trade real
    # coverage for a number already measured above.
    final_model = build_models()[best_name]
    final_model.fit(pairs["text"], pairs["category"])

    model_path = os.path.join(args.out, "categorizer_model.joblib")
    joblib.dump(final_model, model_path)

    metadata = {
        "model_name": best_name,
        "label_space": "purpose",
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "random_state": RANDOM_STATE,
        "dataset": [os.path.basename(p) for p in args.data],
        "dataset_rows": int(len(df)),
        "distinct_templates": int(report["stats"]["distinct_templates"]),
        "fitted_on": {
            "strategy": "distinct (text, category) pairs from the full corpus",
            "n_examples": int(len(pairs)),
        },
        "categories": PURPOSES,
        "classes": sorted(pairs["category"].unique().tolist()),
        "selection": {
            "method": f"{args.cv_repeats} template-held-out splits, mean macro F1",
            "candidates": cv_results,
        },
        "generalization_metrics": best_cv,
        "test_metrics": {k: v for k, v in test_metrics.items() if k != "per_class"},
        "test_metrics_by_source": per_source_metrics or None,
        "source_overlap": overlap or None,
        "random_split_reference": random_split_metrics,
        "collapse_check": collapse,
        "template_split_check": split_check,
        "duplication_comparison": dup_comparison,
        "data_quality": {"warnings": report["warnings"], "stats": report["stats"]},
        "caveat": (
            f"Trained on synthetic data whose {len(df):,} rows collapse to "
            f"{report['stats']['distinct_templates']} narration templates. The honest "
            "generalisation estimate is `generalization_metrics` "
            f"(macro F1 {best_cv['macro_f1_mean']} +/- {best_cv['macro_f1_sd']} on unseen "
            "phrasings), NOT `random_split_reference`. Neither figure is production "
            "accuracy until measured on anonymised real transactions."
        ),
    }
    with open(os.path.join(args.out, "model_metadata.json"), "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    with open(os.path.join(args.out, "evaluation_report.json"), "w", encoding="utf-8") as f:
        json.dump({
            "test_unseen_templates": test_metrics,
            "test_unseen_templates_by_source": per_source_metrics or None,
            "cross_validation": cv_results,
            "random_split_reference": random_split_metrics,
            "duplication_comparison": dup_comparison,
        }, f, indent=2)

    print(f"\nArtifact written : {model_path}")
    print(f"Metadata written : {os.path.join(args.out, 'model_metadata.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
