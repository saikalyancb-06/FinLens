"""Evaluate the trained categoriser and the full hybrid system.

Produces two numbers deliberately, because they answer different questions:

  RANDOM SPLIT      — the conventional held-out score. On this dataset it is
                      inflated: ~40% of test narrations appear verbatim in
                      training, so the model is partly being asked to recall
                      rows it memorised.

  TEMPLATE-DISJOINT — narrations are grouped by their template shape and whole
                      groups are assigned to one side of the split, so no
                      phrasing seen in training appears in test. This is the
                      honest estimate of behaviour on unfamiliar merchants.

Usage:
    python -m mlmodel.evaluate_categorizer --data <csv>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.model_selection import train_test_split

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.categorization.hybrid import classify_transaction  # noqa: E402
from app.categorization.normalizer import normalize_for_ml  # noqa: E402
from app.categorization.rule_engine import rule_engine  # noqa: E402
from app.categorization.taxonomy import CATEGORIES, UNCATEGORIZED  # noqa: E402
from mlmodel.train_categorizer import (  # noqa: E402
    RANDOM_STATE, build_models, load_dataset,
)

ARTIFACT_DIR = os.path.join(os.path.dirname(__file__), "artifacts")


def template_of(narration: str) -> str:
    return re.sub(r"\d+", "#", normalize_for_ml(str(narration)))


def core_metrics(y_true, y_pred) -> Dict[str, float]:
    return {
        "accuracy": round(float(accuracy_score(y_true, y_pred)), 4),
        "macro_precision": round(float(precision_score(y_true, y_pred, average="macro", zero_division=0)), 4),
        "macro_recall": round(float(recall_score(y_true, y_pred, average="macro", zero_division=0)), 4),
        "macro_f1": round(float(f1_score(y_true, y_pred, average="macro", zero_division=0)), 4),
        "weighted_f1": round(float(f1_score(y_true, y_pred, average="weighted", zero_division=0)), 4),
    }


def print_confusion(y_true, y_pred, title: str) -> List[List[int]]:
    labels = sorted(set(list(y_true) + list(y_pred)))
    cm = confusion_matrix(y_true, y_pred, labels=labels)
    print(f"\n{title}")
    print("-" * len(title))
    width = max(len(l) for l in labels) + 1
    header = " " * width + " ".join(f"{i:>4}" for i in range(len(labels)))
    print(header)
    for i, label in enumerate(labels):
        row = " ".join(f"{v:>4}" for v in cm[i])
        print(f"{label:<{width}}{row}   [{i}]")
    return cm.tolist()


def grouped_split(df: pd.DataFrame, test_frac: float = 0.3):
    """Split so that no narration template appears on both sides."""
    df = df.copy()
    df["_template"] = df["narration"].map(template_of)

    # Assign whole templates to train/test, keeping category balance roughly
    # even by walking templates per category.
    by_cat_templates = defaultdict(list)
    for (cat, tmpl), grp in df.groupby(["category", "_template"]):
        by_cat_templates[cat].append((tmpl, len(grp)))

    test_templates = set()
    rng = np.random.RandomState(RANDOM_STATE)
    for cat, tmpls in by_cat_templates.items():
        rng.shuffle(tmpls)
        target = sum(n for _, n in tmpls) * test_frac
        acc = 0
        for tmpl, n in tmpls:
            if acc >= target:
                break
            test_templates.add(tmpl)
            acc += n

    test_mask = df["_template"].isin(test_templates)
    return df[~test_mask], df[test_mask]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", default=ARTIFACT_DIR)
    args = parser.parse_args()

    df = load_dataset(args.data)
    results: Dict[str, Any] = {}

    # ================= 1. RANDOM SPLIT (matches training) =================
    X, y = df["narration_normalized"], df["category"]
    X_tr, X_tmp, y_tr, y_tmp = train_test_split(X, y, test_size=0.30, stratify=y, random_state=RANDOM_STATE)
    X_val, X_te, y_val, y_te = train_test_split(X_tmp, y_tmp, test_size=0.50, stratify=y_tmp, random_state=RANDOM_STATE)

    import joblib
    model = joblib.load(os.path.join(args.out, "categorizer_model.joblib"))
    y_pred_random = model.predict(X_te)

    print("=" * 72)
    print("1. RANDOM SPLIT — held-out test (inflated by narration overlap)")
    print("=" * 72)
    m_random = core_metrics(y_te, y_pred_random)
    for k, v in m_random.items():
        print(f"   {k:<18}: {v}")
    print()
    print(classification_report(y_te, y_pred_random, zero_division=0))
    cm_random = print_confusion(y_te, y_pred_random, "Confusion matrix (random split)")
    results["random_split"] = {**m_random, "confusion_matrix": cm_random}

    # ============ 2. TEMPLATE-DISJOINT SPLIT (honest estimate) ============
    print("\n" + "=" * 72)
    print("2. TEMPLATE-DISJOINT SPLIT — no shared phrasing between train/test")
    print("=" * 72)
    tr_df, te_df = grouped_split(df)
    print(f"   train={len(tr_df)}  test={len(te_df)}")
    overlap = set(tr_df["narration"]) & set(te_df["narration"])
    print(f"   exact narration overlap: {len(overlap)} (should be 0)")

    grouped_model = build_models()["tfidf_linearsvm_calibrated"]
    grouped_model.fit(tr_df["narration_normalized"], tr_df["category"])
    y_pred_grouped = grouped_model.predict(te_df["narration_normalized"])

    m_grouped = core_metrics(te_df["category"], y_pred_grouped)
    for k, v in m_grouped.items():
        print(f"   {k:<18}: {v}")
    print()
    print(classification_report(te_df["category"], y_pred_grouped, zero_division=0))
    results["template_disjoint_split"] = m_grouped

    # ================= 3. RULES vs ML vs HYBRID ==========================
    print("\n" + "=" * 72)
    print("3. RULES vs ML vs HYBRID (on the random test split)")
    print("=" * 72)

    sample = df.loc[X_te.index]
    rows = []
    for _, r in sample.iterrows():
        rule_res = rule_engine.classify(r["narration"], amount=r["amount"], direction=r["debit_credit"])
        hybrid_res = classify_transaction(r["narration"], amount=r["amount"], direction=r["debit_credit"])
        ml_only = model.predict([r["narration_normalized"]])[0]
        rows.append({
            "narration": r["narration"],
            "truth": r["category"],
            "rule": rule_res.category,
            "rule_score": rule_res.rule_score,
            "ml": ml_only,
            "hybrid": hybrid_res.category,
            "method": hybrid_res.classification_method,
            "requires_review": hybrid_res.requires_review,
            "confidence": hybrid_res.classification_confidence,
        })
    ev = pd.DataFrame(rows)

    rule_covered = ev[ev["rule"].notna()]
    print(f"   rule engine fired on      : {len(rule_covered)}/{len(ev)} ({len(rule_covered)/len(ev)*100:.1f}%)")
    print(f"   rule accuracy (when fired): {(rule_covered['rule'] == rule_covered['truth']).mean():.4f}")
    print(f"   ML accuracy (all rows)    : {(ev['ml'] == ev['truth']).mean():.4f}")

    decided = ev[~ev["requires_review"]]
    print(f"   hybrid auto-decided       : {len(decided)}/{len(ev)} ({len(decided)/len(ev)*100:.1f}%)")
    print(f"   hybrid accuracy (decided) : {(decided['hybrid'] == decided['truth']).mean():.4f}")
    print(f"   sent to manual review     : {int(ev['requires_review'].sum())}")
    print()
    print("   method breakdown:")
    for method, n in ev["method"].value_counts().items():
        sub = ev[ev["method"] == method]
        acc = (sub["hybrid"] == sub["truth"]).mean()
        print(f"      {method:<8} {n:>5}  accuracy={acc:.4f}")

    results["comparison"] = {
        "rule_coverage": round(len(rule_covered) / len(ev), 4),
        "rule_accuracy_when_fired": round(float((rule_covered["rule"] == rule_covered["truth"]).mean()), 4),
        "ml_accuracy": round(float((ev["ml"] == ev["truth"]).mean()), 4),
        "hybrid_auto_decided_pct": round(len(decided) / len(ev), 4),
        "hybrid_accuracy_when_decided": round(float((decided["hybrid"] == decided["truth"]).mean()), 4),
        "sent_to_review": int(ev["requires_review"].sum()),
    }

    # ---- Where rules beat ML, and vice versa -----------------------------
    rules_win = ev[(ev["rule"] == ev["truth"]) & (ev["ml"] != ev["truth"])]
    ml_wins = ev[(ev["ml"] == ev["truth"]) & (ev["rule"] != ev["truth"]) & ev["rule"].notna()]

    print(f"\n   Rules correct where ML wrong: {len(rules_win)}")
    for _, r in rules_win.head(5).iterrows():
        print(f"      '{r['narration'][:44]:<44}' truth={r['truth']:<17} ml={r['ml']}")

    print(f"\n   ML correct where rules wrong: {len(ml_wins)}")
    for _, r in ml_wins.head(5).iterrows():
        print(f"      '{r['narration'][:44]:<44}' truth={r['truth']:<17} rule={r['rule']} (score {r['rule_score']})")

    # ---- Top failure cases ------------------------------------------------
    failures = ev[(~ev["requires_review"]) & (ev["hybrid"] != ev["truth"])]
    print(f"\n   Hybrid auto-decided but WRONG: {len(failures)}")
    for _, r in failures.head(10).iterrows():
        print(f"      '{r['narration'][:40]:<40}' truth={r['truth']:<17} got={r['hybrid']:<17} via {r['method']}")

    conf_pairs = Counter(zip(failures["truth"], failures["hybrid"]))
    if conf_pairs:
        print("\n   Most common confusions (truth -> predicted):")
        for (t, p), n in conf_pairs.most_common(6):
            print(f"      {t:<18} -> {p:<18} {n}")

    # ---- Review examples --------------------------------------------------
    review = ev[ev["requires_review"]]
    print(f"\n   Examples routed to manual review: {len(review)}")
    for _, r in review.head(5).iterrows():
        print(f"      '{r['narration'][:44]:<44}' truth={r['truth']}")

    results["failure_examples"] = failures.head(20).to_dict("records")
    results["rules_beat_ml"] = rules_win.head(10).to_dict("records")
    results["ml_beats_rules"] = ml_wins.head(10).to_dict("records")

    out_path = os.path.join(args.out, "evaluation_full.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull report written: {out_path}")

    print("\n" + "!" * 72)
    print("The random-split figure is NOT a production estimate: this dataset is")
    print("synthetic and ~40% of test narrations appear verbatim in training.")
    print("Use the template-disjoint number as the realistic lower bound, and")
    print("re-measure on anonymised real transactions before trusting either.")
    print("!" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
