"""Why does the Categories page say 221 decisions and the Review Queue say 0?

RUN THIS FIRST when the two screens disagree. It reproduces both queries
side by side on the real ledger and prints the population each one sees, so the
gap has a number and a cause instead of a theory.

    python scripts/queue_vs_categories.py
    python scripts/queue_vs_categories.py --user someone@example.com

THE TWO AXES. This codebase classifies every transaction twice and the two
results live in different columns:

    FLAT   transactions.category_id, predictions.requires_review,
           predictions.predicted_category      <- what the Review Queue read
    TREE   transactions.category_confidence,
           transactions.category_path          <- what the Categories page read

They can disagree, and when they do the user is told there is work to do and
shown an empty screen to do it on. The fix was to give both screens one shared
predicate (`app.categorization.decisions.needs_decision`); this script is how
you confirm the ledger agrees with it, and how you see which axis was wrong.

Nothing is written. This only reads.
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.categorization.counterparty import group_for
from app.categorization.decisions import (
    REVIEW_THRESHOLD, decision_weight, needs_decision, needs_decision_clause,
)
from app.categorization.taxonomy import UNCATEGORIZED as FLAT_UNCATEGORIZED
from app.database.session import SessionLocal
from app.models.prediction import Prediction
from app.models.transaction import Transaction
from app.models.user import User

RETIRED_ROOT = "Other / Uncategorized"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user")
    ap.add_argument("--show", type=int, default=20)
    args = ap.parse_args()

    db = SessionLocal()
    try:
        users = db.query(User)
        if args.user:
            users = users.filter(User.email == args.user)
        users = users.all()
        if not users:
            print("No matching user.")
            return 1

        for user in users:
            pairs = (db.query(Transaction, Prediction)
                     .outerjoin(Prediction, Prediction.transaction_id == Transaction.id)
                     .filter(Transaction.user_id == user.id,
                             Transaction.superseded_by_id.is_(None))
                     .all())
            if not pairs:
                print(f"\n{user.email}: no transactions.")
                continue

            print(f"\n{'=' * 78}\n{user.email}\n{'=' * 78}")
            print(f"  live transactions : {len(pairs)}")

            # ---- what each axis says, independently -----------------------
            flat_unsure = tree_unsure = both = neither = 0
            no_prediction = 0
            stale_node = 0
            for tx, pred in pairs:
                if pred is None:
                    no_prediction += 1
                f = (tx.category_id is None
                     or (pred is not None and (pred.requires_review
                         or pred.predicted_category == FLAT_UNCATEGORIZED)))
                conf = tx.category_confidence
                t = (float(conf) < REVIEW_THRESHOLD) if conf is not None else not (
                    tx.category or tx.legacy_category)
                flat_unsure += f
                tree_unsure += t
                both += (f and t)
                neither += (not f and not t)
                if (tx.category_path or tx.category or "").startswith(RETIRED_ROOT):
                    stale_node += 1

            print(f"\n  THE FLAT AXIS (what the Review Queue used to read)")
            print(f"    rows it calls unsure          : {flat_unsure}")
            print(f"    rows with NO prediction row   : {no_prediction}"
                  f"   <- invisible to every prediction test")
            print(f"\n  THE TREE AXIS (what the Categories page reads)")
            print(f"    rows it calls unsure          : {tree_unsure}")
            print(f"\n  THE DISAGREEMENT")
            print(f"    both axes unsure              : {both}")
            print(f"    only the TREE is unsure       : {tree_unsure - both}"
                  f"   <- shown on Categories, missing from the queue")
            print(f"    only the FLAT axis is unsure  : {flat_unsure - both}"
                  f"   <- in the queue, looks settled on Categories")
            print(f"    both content                  : {neither}")

            if stale_node:
                print(f"\n  {stale_node} rows are still stamped '{RETIRED_ROOT}'."
                      f"\n  That node was deleted from the taxonomy — run "
                      f"`alembic upgrade head` to move them.")

            # ---- what the SHARED predicate now returns --------------------
            unified = (db.query(Transaction, Prediction)
                       .outerjoin(Prediction, Prediction.transaction_id == Transaction.id)
                       .filter(Transaction.user_id == user.id,
                               Transaction.superseded_by_id.is_(None))
                       .filter(needs_decision_clause(Transaction, Prediction))
                       .all())
            in_python = [tx for tx, pred in pairs if needs_decision(tx, pred)]

            print(f"\n  AFTER THE FIX (one shared predicate)")
            print(f"    the queue's SQL now returns   : {len(unified)}")
            print(f"    the Python predicate agrees on: {len(in_python)}")
            if len(unified) != len(in_python):
                print("    ^ these MUST match. They do not — the SQL and the "
                      "Python spelling of the rule have drifted.")

            # ---- and how many questions that actually is ------------------
            groups = {}
            for tx, pred in pairs:
                if not needs_decision(tx, pred):
                    continue
                cp = group_for(tx.narration_clean or tx.narration_raw or "")
                if not cp:
                    continue
                g = groups.setdefault(cp.group_key, {"n": 0, "paise": 0,
                                                     "name": cp.display,
                                                     "sample": ""})
                g["n"] += 1
                g["paise"] += int(tx.debit_paise or 0) + int(tx.credit_paise or 0)
                # The raw narration behind the group. Without it a nonsense
                # group name is a mystery; with it the cause is obvious — this
                # is how `07:22:38` was found to be a clock time the channel
                # patterns were handing over as the payee.
                g["sample"] = g["sample"] or (
                    tx.narration_clean or tx.narration_raw or "")

            ranked = sorted(groups.values(),
                            key=lambda g: decision_weight(g["n"], g["paise"]),
                            reverse=True)
            singles = sum(1 for g in ranked if g["n"] == 1)
            covered = sum(g["n"] for g in ranked[:50])

            print(f"\n  THE HUMAN COST")
            print(f"    counterparties to decide      : {len(ranked)}")
            print(f"    ...seen exactly once          : {singles}"
                  f"   <- one answer clears one row")
            print(f"    top 50 answers would clear    : {covered} of "
                  f"{sum(g['n'] for g in ranked)} rows")

            print(f"\n  Worth asking about (top {args.show}):")
            for g in ranked[:args.show]:
                print(f"    {g['n']:>4} rows  ₹{g['paise'] / 100:>14,.2f}  "
                      f"{g['name'][:32]:<34}{g['sample'][:46]}")

            singles_sample = [g for g in ranked if g["n"] == 1][:args.show]
            if singles_sample:
                print(f"\n  Seen once — the tail that must NOT become questions:")
                for g in singles_sample:
                    print(f"    {g['name'][:32]:<34}{g['sample'][:56]}")

            print("\n  Nothing was written.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
