"""Dataset quality checks for the PURPOSE-labelled transaction corpus.

Sibling of `data_quality.py`, which validates the older personal-finance schema
(`transaction_id` / `narration` / `debit_credit`) against the 15-name taxonomy.
This module validates the business schema

    date, transaction_description, credit, debit, balance, category,
    account_id, account_type

against the 18 PURPOSE names in `app.categorization.dual_taxonomy`.

Like its sibling, every check returns findings rather than raising, so the
training script can print one complete report and then decide what is fatal.

The check that matters most here is template collapse. A row count says nothing
about how much a corpus actually teaches: 180,000 rows built from a few hundred
sentence templates with randomised reference numbers contain a few hundred
distinct lessons, not 180,000. Reporting rows-per-template up front is what stops
a random-split accuracy of ~1.00 from being read as a generalisation result.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List

import pandas as pd

from app.categorization.dual_taxonomy import PURPOSES, PURPOSE_SET
from app.categorization.normalizer import normalize_narration

REQUIRED_COLUMNS = ["transaction_description", "category"]
OPTIONAL_COLUMNS = ["date", "credit", "debit", "balance", "account_id",
                    "account_type", "currency", "direction", "source"]

# Below this many distinct templates per row, a random split is measuring recall
# of memorised strings rather than generalisation.
TEMPLATE_COLLAPSE_ROWS_PER_TEMPLATE = 5


def narration_template(text: str) -> str:
    """Collapse a narration to its template: normalised, with digits masked.

    "Rapido ride Ref 612736" and "Rapido ride UTR420114115383" are one lesson,
    not two. Grouping on this is what makes a held-out split honest.
    """
    return re.sub(r"\d+", "#", normalize_narration(str(text)))


def check_dataset(df: pd.DataFrame) -> Dict[str, Any]:
    """Run all quality checks and return a structured report."""
    report: Dict[str, Any] = {"fatal": [], "warnings": [], "stats": {}}

    # 1. Schema
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        report["fatal"].append(f"Missing required columns: {missing}")
        return report
    report["stats"]["rows"] = len(df)
    report["stats"]["optional_columns_present"] = [c for c in OPTIONAL_COLUMNS if c in df.columns]

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
        report["warnings"].append(f"Severe class imbalance: largest/smallest = {imbalance:.1f}x")

    # 3. Label space must be exactly the purpose taxonomy — a stray name here
    #    ships a model whose output no downstream consumer can map to a category.
    observed = set(df["category"].dropna().unique())
    invalid = sorted(observed - PURPOSE_SET)
    if invalid:
        report["fatal"].append(f"Categories outside the PURPOSE taxonomy: {invalid}")

    absent = sorted(PURPOSE_SET - observed)
    if absent:
        report["warnings"].append(f"Purposes with no training examples: {absent}")

    # 4. Empty narrations
    desc = df["transaction_description"]
    empty = int(desc.isna().sum() + (desc.fillna("").astype(str).str.strip() == "").sum())
    report["stats"]["empty_narrations"] = empty
    if empty:
        report["warnings"].append(f"{empty} rows have an empty description and will be dropped.")

    # 5. Template diversity — the headline number for this corpus.
    templates = desc.astype(str).map(narration_template)
    counts = Counter(templates)
    n_templates = len(counts)
    rows_per_template = len(df) / max(n_templates, 1)
    report["stats"]["distinct_templates"] = n_templates
    report["stats"]["rows_per_template_mean"] = round(rows_per_template, 1)
    report["stats"]["distinct_raw_descriptions"] = int(desc.nunique())

    if rows_per_template > TEMPLATE_COLLAPSE_ROWS_PER_TEMPLATE:
        report["warnings"].append(
            f"{len(df)} rows collapse to {n_templates} distinct narration templates "
            f"(~{rows_per_template:.0f} rows each). The corpus teaches {n_templates} "
            f"lessons, not {len(df)}. Accuracy from a random row split is a "
            "memorisation score and is reported here only for reference; the "
            "template-held-out figures are the generalisation estimate."
        )

    # 6. Templates carrying more than one label. These are unlearnable by a
    #    narration-only model — it cannot do better than the majority label —
    #    so they cap achievable accuracy and must be visible.
    per_template_labels = df.assign(_t=templates).groupby("_t")["category"].nunique()
    ambiguous = per_template_labels[per_template_labels > 1]
    report["stats"]["ambiguous_templates"] = int(len(ambiguous))
    if len(ambiguous):
        affected = int(df.assign(_t=templates)["_t"].isin(ambiguous.index).sum())
        report["warnings"].append(
            f"{len(ambiguous)} templates carry more than one category "
            f"({affected} rows). A description-only model cannot separate these."
        )

    # 7. Templates per class. A class taught by very few templates will not
    #    survive a held-out-template split, and that is a property of the data,
    #    not of the model.
    per_class = df.assign(_t=templates).groupby("category")["_t"].nunique().sort_values()
    report["stats"]["templates_per_class"] = per_class.to_dict()
    thin = per_class[per_class < 5]
    if len(thin):
        report["warnings"].append(
            f"Classes taught by fewer than 5 templates: {dict(thin)}. "
            "Held-out-template scores for these will be noisy."
        )

    # 8. Direction contradictions, where the columns exist. An income purpose
    #    recorded as a debit is a labelling error worth surfacing.
    if {"credit", "debit"} <= set(df.columns):
        from app.categorization.dual_taxonomy import INCOME_PURPOSES

        credit = pd.to_numeric(df["credit"], errors="coerce").fillna(0)
        debit = pd.to_numeric(df["debit"], errors="coerce").fillna(0)
        income_as_debit = int(((df["category"].isin(INCOME_PURPOSES)) & (debit > 0) & (credit == 0)).sum())
        report["stats"]["income_rows_recorded_as_debit"] = income_as_debit
        if income_as_debit:
            report["warnings"].append(
                f"{income_as_debit} rows carry an income purpose but only a debit amount."
            )

        both_zero = int(((debit == 0) & (credit == 0)).sum())
        report["stats"]["rows_with_no_amount"] = both_zero
        if both_zero:
            report["warnings"].append(f"{both_zero} rows have neither a debit nor a credit amount.")

    return report


def check_template_split(train_df: pd.DataFrame, test_df: pd.DataFrame, template_col: str = "template") -> Dict[str, Any]:
    """Confirm a split really is template-disjoint, and quantify any overlap.

    A split is only worth reporting if this returns zero overlap; the function
    exists so the training script can prove that rather than assert it.
    """
    train_t = set(train_df[template_col])
    test_t = set(test_df[template_col])
    overlap = train_t & test_t
    leaked_rows = int(test_df[template_col].isin(overlap).sum())
    return {
        "train_templates": len(train_t),
        "test_templates": len(test_t),
        "overlapping_templates": len(overlap),
        "leaked_test_rows": leaked_rows,
        "leaked_pct_of_test": round(leaked_rows / max(len(test_df), 1) * 100, 2),
    }


def format_report(report: Dict[str, Any]) -> str:
    lines = ["=" * 74, "DATASET QUALITY REPORT (purpose taxonomy)", "=" * 74]
    stats = report.get("stats", {})

    lines.append(f"Rows                      : {stats.get('rows')}")
    lines.append(f"Classes                   : {stats.get('n_classes')} / {len(PURPOSES)} purposes")
    lines.append(f"Imbalance ratio           : {stats.get('imbalance_ratio')}x")
    lines.append(f"Distinct raw descriptions : {stats.get('distinct_raw_descriptions')}")
    lines.append(f"Distinct templates        : {stats.get('distinct_templates')} "
                 f"(~{stats.get('rows_per_template_mean')} rows each)")
    lines.append(f"Ambiguous templates       : {stats.get('ambiguous_templates')}")
    lines.append(f"Empty descriptions        : {stats.get('empty_narrations')}")

    if stats.get("templates_per_class"):
        lines.append("")
        lines.append("Templates per class (this, not row count, is the teaching signal):")
        for cat, n in sorted(stats["templates_per_class"].items(), key=lambda kv: kv[1]):
            rows = stats.get("class_distribution", {}).get(cat, 0)
            lines.append(f"   {cat:<26} {n:>4} templates   {rows:>7} rows")

    if report.get("fatal"):
        lines.append("")
        lines.append("FATAL:")
        lines.extend(f"   - {m}" for m in report["fatal"])

    if report.get("warnings"):
        lines.append("")
        lines.append("WARNINGS:")
        lines.extend(f"   - {m}" for m in report["warnings"])

    lines.append("=" * 74)
    return "\n".join(lines)
