"""Train and evaluate the transaction category classifier.

Usage:
    python -m mlmodel.train_categorizer --data path/to/transaction_ml_training_10k.csv

Guarantees enforced by this script:

* The test split is touched exactly once, at the end, for reporting. Model
  selection and threshold work happen on validation only.
* No feature derived from the rule engine is used, so evaluating the hybrid
  system against rules is not circular.
* Training FAILS (non-zero exit) if the resulting model is degenerate — i.e.
  collapses onto one dominant class — rather than writing an artifact that
  silently mislabels production data, which is how the previous model shipped.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict

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

from app.categorization.normalizer import normalize_for_ml  # noqa: E402
from app.categorization.taxonomy import CATEGORIES  # noqa: E402
from mlmodel.categorizer.data_quality import (  # noqa: E402
    check_dataset,
    check_split_leakage,
    format_report,
)

RANDOM_STATE = 42
ARTIFACT_DIR = os.path.join(os.path.dirname(__file__), "artifacts")

# A model is considered collapsed if any single class takes more than this share
# of predictions while the reference distribution is roughly balanced.
COLLAPSE_MAX_SINGLE_CLASS_SHARE = 0.40
COLLAPSE_MIN_CLASSES_PREDICTED = 0.60  # fraction of taxonomy that must appear


def load_dataset(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.dropna(subset=["narration", "category"])
    df = df[df["narration"].astype(str).str.strip() != ""]
    df["narration_normalized"] = df["narration"].astype(str).map(normalize_for_ml)
    df = df[df["narration_normalized"].str.strip() != ""]
    return df.reset_index(drop=True)


def build_models() -> Dict[str, Pipeline]:
    """Candidate models. Both are linear and inspectable by design."""
    def vectorizer() -> TfidfVectorizer:
        # Word + character n-grams: character grams give robustness to the
        # merchant-name variations and truncations common in bank narrations.
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
                C=4.0,
                class_weight="balanced",
                random_state=RANDOM_STATE,
            )),
        ]),
        # LinearSVC has no predict_proba, so it is wrapped in a calibrator to
        # produce usable probabilities for the confidence threshold.
        "tfidf_linearsvm_calibrated": Pipeline([
            ("tfidf", vectorizer()),
            ("clf", CalibratedClassifierCV(
                LinearSVC(C=1.0, class_weight="balanced", random_state=RANDOM_STATE),
                method="sigmoid",
                cv=3,
            )),
        ]),
    }


def evaluate(model: Pipeline, X, y_true, label: str) -> Dict[str, Any]:
    y_pred = model.predict(X)
    labels = sorted(set(list(y_true) + list(y_pred)))

    metrics = {
        "split": label,
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 4),
        "macro_precision": round(float(precision_score(y_true, y_pred, average="macro", zero_division=0)), 4),
        "macro_recall": round(float(recall_score(y_true, y_pred, average="macro", zero_division=0)), 4),
        "macro_f1": round(float(f1_score(y_true, y_pred, average="macro", zero_division=0)), 4),
        "weighted_f1": round(float(f1_score(y_true, y_pred, average="weighted", zero_division=0)), 4),
        "per_class": classification_report(
            y_true, y_pred, output_dict=True, zero_division=0
        ),
        "confusion_matrix": {
            "labels": labels,
            "matrix": confusion_matrix(y_true, y_pred, labels=labels).tolist(),
        },
        "prediction_distribution": pd.Series(y_pred).value_counts().to_dict(),
    }
    return metrics


def check_collapse(prediction_distribution: Dict[str, int], total: int) -> Dict[str, Any]:
    """Detect the failure mode where the model predicts one class for everything.

    This is exactly what the previous production model did (Bank Charges for
    nearly every narration), and it shipped because only overall accuracy was
    being looked at.
    """
    if total == 0:
        return {"collapsed": True, "reason": "no predictions"}

    top_class, top_count = max(prediction_distribution.items(), key=lambda kv: kv[1])
    top_share = top_count / total
    classes_predicted = len(prediction_distribution)
    coverage = classes_predicted / len(CATEGORIES)

    problems = []
    if top_share > COLLAPSE_MAX_SINGLE_CLASS_SHARE:
        problems.append(
            f"'{top_class}' accounts for {top_share*100:.1f}% of predictions "
            f"(limit {COLLAPSE_MAX_SINGLE_CLASS_SHARE*100:.0f}%)"
        )
    if coverage < COLLAPSE_MIN_CLASSES_PREDICTED:
        problems.append(
            f"only {classes_predicted}/{len(CATEGORIES)} categories ever predicted "
            f"({coverage*100:.0f}%, minimum {COLLAPSE_MIN_CLASSES_PREDICTED*100:.0f}%)"
        )

    return {
        "collapsed": bool(problems),
        "top_class": top_class,
        "top_class_share": round(top_share, 4),
        "classes_predicted": classes_predicted,
        "problems": problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, help="Path to the labelled CSV")
    parser.add_argument("--out", default=ARTIFACT_DIR, help="Artifact output directory")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    # ---- 1. Load & quality-check -----------------------------------------
    df = load_dataset(args.data)
    report = check_dataset(df)
    print(format_report(report))

    if report["fatal"]:
        print("\nAborting: dataset has fatal quality problems.")
        return 1

    X = df["narration_normalized"]
    y = df["category"]

    # ---- 2. Stratified 70/15/15 split ------------------------------------
    X_train, X_temp, y_train, y_temp, idx_train, idx_temp = train_test_split(
        X, y, df.index, test_size=0.30, stratify=y, random_state=RANDOM_STATE
    )
    X_val, X_test, y_val, y_test, idx_val, idx_test = train_test_split(
        X_temp, y_temp, idx_temp, test_size=0.50, stratify=y_temp, random_state=RANDOM_STATE
    )
    print(f"\nSplit sizes -> train={len(X_train)}  val={len(X_val)}  test={len(X_test)}")

    leakage = check_split_leakage(df.loc[idx_train], df.loc[idx_test])
    print("\nTrain/test leakage:")
    for k, v in leakage.items():
        print(f"   {k}: {v}")

    # ---- 3. Model selection on VALIDATION only ---------------------------
    results = {}
    for name, model in build_models().items():
        print(f"\nTraining {name} ...")
        model.fit(X_train, y_train)
        val_metrics = evaluate(model, X_val, y_val, "validation")
        results[name] = {"model": model, "val": val_metrics}
        print(f"   val macro_f1={val_metrics['macro_f1']}  accuracy={val_metrics['accuracy']}")

    best_name = max(results, key=lambda n: results[n]["val"]["macro_f1"])
    best = results[best_name]
    best_model = best["model"]
    print(f"\nSelected model: {best_name} (val macro F1 = {best['val']['macro_f1']})")

    # ---- 4. Collapse check on validation ---------------------------------
    val_collapse = check_collapse(best["val"]["prediction_distribution"], len(X_val))
    print("\nDegenerate-model check (validation):")
    if val_collapse["collapsed"]:
        for p in val_collapse["problems"]:
            print(f"   FAIL: {p}")
        print("\nAborting: model collapsed onto a dominant class. Artifact NOT written.")
        return 2
    print(f"   OK: top class '{val_collapse['top_class']}' at "
          f"{val_collapse['top_class_share']*100:.1f}%, "
          f"{val_collapse['classes_predicted']}/{len(CATEGORIES)} categories predicted")

    # ---- 5. Final, single evaluation on the untouched TEST split ----------
    test_metrics = evaluate(best_model, X_test, y_test, "test")
    test_collapse = check_collapse(test_metrics["prediction_distribution"], len(X_test))
    if test_collapse["collapsed"]:
        for p in test_collapse["problems"]:
            print(f"   FAIL (test): {p}")
        print("\nAborting: model collapsed on the test split. Artifact NOT written.")
        return 2

    print("\n" + "=" * 70)
    print("TEST METRICS (test set used once, after model selection)")
    print("=" * 70)
    print(f"accuracy        : {test_metrics['accuracy']}")
    print(f"macro precision : {test_metrics['macro_precision']}")
    print(f"macro recall    : {test_metrics['macro_recall']}")
    print(f"macro F1        : {test_metrics['macro_f1']}")
    print(f"weighted F1     : {test_metrics['weighted_f1']}")
    print()
    print(classification_report(y_test, best_model.predict(X_test), zero_division=0))

    # ---- 6. Persist artifact + evaluation report -------------------------
    model_path = os.path.join(args.out, "categorizer_model.joblib")
    joblib.dump(best_model, model_path)

    metadata = {
        "model_name": best_name,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "random_state": RANDOM_STATE,
        "dataset": os.path.basename(args.data),
        "dataset_rows": int(len(df)),
        "split": {"train": len(X_train), "validation": len(X_val), "test": len(X_test)},
        "categories": CATEGORIES,
        "classes": sorted(y.unique().tolist()),
        "validation_metrics": {k: v for k, v in best["val"].items() if k != "per_class"},
        "test_metrics": {k: v for k, v in test_metrics.items() if k != "per_class"},
        "collapse_check": {"validation": val_collapse, "test": test_collapse},
        "leakage": leakage,
        "data_quality": {"warnings": report["warnings"], "stats": report["stats"]},
        "caveat": (
            "Trained on synthetic data. These metrics describe performance on "
            "held-out synthetic examples and must not be presented as production "
            "accuracy until measured on anonymised real transactions."
        ),
    }
    with open(os.path.join(args.out, "model_metadata.json"), "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    with open(os.path.join(args.out, "evaluation_report.json"), "w", encoding="utf-8") as f:
        json.dump({"validation": best["val"], "test": test_metrics}, f, indent=2)

    print(f"\nArtifact written : {model_path}")
    print(f"Metadata written : {os.path.join(args.out, 'model_metadata.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
