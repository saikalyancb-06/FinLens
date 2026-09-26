"""Dataset quality checks run before any model is trained.

Every check returns findings rather than raising, so the training script can
print a complete report and then decide what is fatal. The distinction matters:
an unlabelled row is fatal, a duplicated narration is a caveat on the reported
metric.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List

import pandas as pd

from app.categorization.normalizer import normalize_narration
from app.categorization.taxonomy import CATEGORIES, CATEGORY_SET

REQUIRED_COLUMNS = ["transaction_id", "narration", "amount", "debit_credit", "category"]


def check_dataset(df: pd.DataFrame) -> Dict[str, Any]:
    """Run all quality checks and return a structured report."""
    report: Dict[str, Any] = {"fatal": [], "warnings": [], "stats": {}}

    # 1. Schema
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        report["fatal"].append(f"Missing required columns: {missing_cols}")
        return report

    report["stats"]["rows"] = len(df)

    # 2. Class distribution
    dist = df["category"].value_counts()
    report["stats"]["class_distribution"] = dist.to_dict()
    report["stats"]["n_classes"] = int(dist.shape[0])
    if dist.empty:
        report["fatal"].append("No labelled rows.")
        return report

    imbalance = dist.max() / max(dist.min(), 1)
    report["stats"]["imbalance_ratio"] = round(float(imbalance), 2)
    if imbalance > 10:
        report["warnings"].append(
            f"Severe class imbalance: largest/smallest = {imbalance:.1f}x"
        )

    # 3. Invalid categories — fatal, since the label space must match taxonomy
    invalid = sorted(set(df["category"].dropna().unique()) - CATEGORY_SET)
    if invalid:
        report["fatal"].append(f"Categories outside the canonical taxonomy: {invalid}")

    absent = sorted(CATEGORY_SET - set(df["category"].dropna().unique()))
    if absent:
        report["warnings"].append(f"Taxonomy categories with no training examples: {absent}")

    # 4. Empty narrations
    empty_narr = int(df["narration"].isna().sum() + (df["narration"].astype(str).str.strip() == "").sum())
    report["stats"]["empty_narrations"] = empty_narr
    if empty_narr:
        report["warnings"].append(f"{empty_narr} rows have an empty narration and will be dropped.")

    # 5. Duplicate transaction IDs
    dup_ids = int(df["transaction_id"].duplicated().sum())
    report["stats"]["duplicate_transaction_ids"] = dup_ids
    if dup_ids:
        report["warnings"].append(f"{dup_ids} duplicate transaction_id values.")

    # 6. Duplicate narrations
    dup_narr = int(df["narration"].duplicated().sum())
    report["stats"]["duplicate_narrations"] = dup_narr
    if dup_narr:
        report["warnings"].append(
            f"{dup_narr} duplicated narrations ({dup_narr/len(df)*100:.1f}%). "
            "These inflate any accuracy measured on a random split."
        )

    # 7. Contradictory label/direction combinations
    contradictions: List[str] = []
    dirs = df["debit_credit"].astype(str).str.upper()
    salary_debit = int(((df["category"] == "Salary / Income") & (dirs == "DEBIT")).sum())
    if salary_debit:
        contradictions.append(f"{salary_debit} rows labelled Salary / Income with direction DEBIT")
    report["stats"]["contradictory_direction_rows"] = salary_debit
    if contradictions:
        report["warnings"].extend(contradictions)

    # 8. Synthetic template families
    # Collapse each narration to its shape (merchant words only, digits removed)
    # to estimate how many genuinely distinct phrasings exist. A small number of
    # templates means a random split leaks near-identical rows across sides.
    shapes = df["narration"].astype(str).map(lambda s: re.sub(r"\d+", "#", normalize_narration(s)))
    shape_counts = Counter(shapes)
    report["stats"]["distinct_narration_shapes"] = len(shape_counts)
    report["stats"]["rows_per_shape_mean"] = round(len(df) / max(len(shape_counts), 1), 2)
    if len(shape_counts) < len(df) / 5:
        report["warnings"].append(
            f"Only {len(shape_counts)} distinct narration templates for {len(df)} rows "
            f"(~{len(df)/max(len(shape_counts),1):.1f} rows per template). "
            "Metrics from a random split will overstate real-world performance."
        )

    return report


def check_split_leakage(train_df: pd.DataFrame, test_df: pd.DataFrame) -> Dict[str, Any]:
    """Quantify overlap between two splits."""
    train_narr = set(train_df["narration"].astype(str))
    test_narr = set(test_df["narration"].astype(str))
    exact_overlap = train_narr & test_narr

    def shape(s: str) -> str:
        return re.sub(r"\d+", "#", normalize_narration(str(s)))

    train_shapes = set(train_df["narration"].map(shape))
    test_shapes = set(test_df["narration"].map(shape))
    shape_overlap = train_shapes & test_shapes

    return {
        "exact_narration_overlap": len(exact_overlap),
        "exact_overlap_pct_of_test": round(
            len(test_df[test_df["narration"].astype(str).isin(exact_overlap)]) / max(len(test_df), 1) * 100, 2
        ),
        "template_overlap": len(shape_overlap),
        "template_overlap_pct": round(len(shape_overlap) / max(len(test_shapes), 1) * 100, 2),
    }


def format_report(report: Dict[str, Any]) -> str:
    lines = ["=" * 70, "DATASET QUALITY REPORT", "=" * 70]
    stats = report.get("stats", {})

    lines.append(f"Rows                     : {stats.get('rows')}")
    lines.append(f"Classes                  : {stats.get('n_classes')} / {len(CATEGORIES)} in taxonomy")
    lines.append(f"Imbalance ratio          : {stats.get('imbalance_ratio')}x")
    lines.append(f"Duplicate narrations     : {stats.get('duplicate_narrations')}")
    lines.append(f"Duplicate transaction ids: {stats.get('duplicate_transaction_ids')}")
    lines.append(f"Empty narrations         : {stats.get('empty_narrations')}")
    lines.append(f"Distinct templates       : {stats.get('distinct_narration_shapes')} "
                 f"(~{stats.get('rows_per_shape_mean')} rows each)")

    if stats.get("class_distribution"):
        lines.append("")
        lines.append("Class distribution:")
        for cat, n in sorted(stats["class_distribution"].items(), key=lambda kv: -kv[1]):
            lines.append(f"   {cat:<20} {n}")

    if report.get("fatal"):
        lines.append("")
        lines.append("FATAL:")
        lines.extend(f"   - {m}" for m in report["fatal"])

    if report.get("warnings"):
        lines.append("")
        lines.append("WARNINGS:")
        lines.extend(f"   - {m}" for m in report["warnings"])

    lines.append("=" * 70)
    return "\n".join(lines)
