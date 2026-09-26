"""STAGE 16: how many duplicate parties does the resolver actually remove?

The objective is MAXIMUM SAFE REDUCTION, not a target number, so the only
honest way to talk about it is to run it on real statements and print what
happened — including what it chose NOT to merge.

    python scripts/entity_resolution_report.py --csv data.csv
    python scripts/entity_resolution_report.py --user someone@example.com
    python scripts/entity_resolution_report.py --csv data.csv --show 40

With `--csv` it reads a narration column from a file. With `--user` it reads
that user's live ledger, and then it can also use the CONTEXT the database
carries — direction and rail — which a bare CSV does not have.

Nothing is written in either mode.
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.entity_resolution import EntityMention, EntityResolver
from app.entity_resolution.evaluate import score_against_truth


def _from_csv(path: str, column: str, truth_column: str):
    """Read narrations, and the ground-truth entity when the file carries one.

    Column names vary between test files, so both the deposit/credit and the
    withdrawal/debit spellings are accepted rather than requiring one layout.
    """
    with open(path, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get(column) or "").strip()
            if not raw:
                continue
            dep = (row.get("deposit") or row.get("credit") or "").strip()
            incoming = dep not in {"", "0", "0.0", "0.00"}
            yield (
                EntityMention(
                    raw=raw,
                    direction="credit" if incoming else "debit",
                    method=(row.get("transaction_method") or None),
                ),
                (row.get(truth_column) or "").strip() or None,
            )


def _from_ledger(email):
    from app.database.session import SessionLocal
    from app.models.transaction import Transaction
    from app.models.user import User

    db = SessionLocal()
    try:
        users = db.query(User)
        if email:
            users = users.filter(User.email == email)
        for user in users.all():
            rows = (db.query(Transaction)
                    .filter(Transaction.user_id == user.id,
                            Transaction.superseded_by_id.is_(None))
                    .all())
            yield user.email, [
                EntityMention(
                    raw=t.narration_clean or t.narration_raw or "",
                    row_id=str(t.id),
                    direction="credit" if t.credit_paise else "debit",
                    method=t.transaction_method,
                    utr=t.reference_no,
                    amount=(t.debit_paise or t.credit_paise or 0) / 100.0,
                    date=t.txn_date,
                )
                for t in rows
                if (t.narration_clean or t.narration_raw)
            ]
    finally:
        db.close()


def report(label: str, mentions, show: int, truths=None) -> None:
    resolver = EntityResolver()
    result = resolver.resolve(mentions)
    d = result.as_dict()

    print(f"\n{'=' * 78}\nENTITY RESOLUTION — {label}\n{'=' * 78}")
    print(f"  mentions processed           : {len(mentions)}")
    print(f"  raw_unique_strings           : {d['raw_unique_entities']}")
    print(f"  canonical_entities           : {d['canonical_entities']}")
    print(f"  reduction_count              : {d['entities_merged']}")
    print(f"  reduction_percentage         : {d['merge_percentage']}%")
    print(f"  high_confidence_merges       : {d['high_confidence_merges']}")
    print(f"  medium_confidence_candidates : {d['medium_confidence_candidates']}")
    print(f"  unresolved_entities          : {d['unresolved_entities']}")

    # ---- ground truth, when the dataset carries it ----------------------
    if truths and any(truths):
        cluster_of = {}
        for cluster in result.clusters:
            for mention in cluster.mentions:
                cluster_of[id(mention)] = cluster.canonical
        pairs = [(cluster_of.get(id(m), "UNRESOLVED"), t)
                 for m, t in zip(mentions, truths) if t]
        gt = score_against_truth(pairs)
        g = gt.as_dict()
        print(f"\n  GROUND TRUTH")
        print(f"  true_entities                : {g['true_entities']}")
        print(f"  false_merge_count            : {g['false_merge_count']}"
              f"   <- two real parties welded into one")
        print(f"  false_split_count            : {g['false_split_count']}"
              f"   <- one real party broken up")
        print(f"  pairwise_precision           : {g['pairwise_precision']}")
        print(f"  pairwise_recall              : {g['pairwise_recall']}")
        print(f"  pairwise_f1                  : {g['pairwise_f1']}")
        print(f"  clusters exactly right       : {g['exact_clusters']}"
              f" of {g['true_entities']}")
        if gt.merged_examples:
            print(f"\n  FALSE MERGES — the expensive mistake, because nothing on")
            print(f"  screen tells the user one party's money is under another's name:")
            for cluster, entities in gt.merged_examples:
                print(f"    {cluster}  <-  {entities}")
        if gt.split_examples:
            print(f"\n  FALSE SPLITS — one party asked about more than once:")
            for entity, clusters in gt.split_examples:
                print(f"    {entity}  ->  {clusters}")

    multi = [c for c in result.clusters if len(c.aliases) > 1]
    print(f"\n  Entities assembled from more than one spelling: {len(multi)}")
    for c in multi[:show]:
        print(f"\n    {c.canonical}   ({c.size} rows, {len(c.aliases)} spellings)")
        for alias in c.aliases[:8]:
            print(f"        {alias}")
        if len(c.aliases) > 8:
            print(f"        ... and {len(c.aliases) - 8} more")

    if result.suggestions:
        print(f"\n  MEDIUM CONFIDENCE — a person should confirm these "
              f"({len(result.suggestions)}):")
        for a, b, v in result.suggestions[:show]:
            why = ", ".join(f"{k}={val}" for k, val in
                            sorted(v.signals.items(), key=lambda kv: -kv[1])[:4])
            print(f"    {v.score:.3f}  {a[:32]:<34} <-> {b[:32]}")
            print(f"           {why}")
            if v.conflicts:
                print(f"           conflicts: {'; '.join(v.conflicts)}")

    print("\n  Nothing was written.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--column", default="narration")
    ap.add_argument("--truth-column", default="expected_entity",
                    help="ground-truth entity column, when the file has one")
    ap.add_argument("--user")
    ap.add_argument("--show", type=int, default=25)
    args = ap.parse_args()

    if args.csv:
        pairs = list(_from_csv(args.csv, args.column, args.truth_column))
        report(args.csv, [m for m, _t in pairs], args.show,
               truths=[t for _m, t in pairs])
        return 0

    for email, mentions in _from_ledger(args.user):
        if mentions:
            report(email, mentions, args.show)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
