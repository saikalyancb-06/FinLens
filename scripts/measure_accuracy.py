"""How accurate is the classifier, measured against answers a HUMAN gave.

READ THIS BEFORE QUOTING A NUMBER FROM IT.

There are two questions people mean by "accuracy" and they have very different
answers:

    COVERAGE   of the rows in this ledger, how many did the system answer at
               all without asking a person?
    ACCURACY   of the rows it answered, how many did it get RIGHT?

Coverage is easy to measure and easy to game — a classifier that guesses on
everything has 100% coverage. Accuracy needs GROUND TRUTH, and the only real
ground truth this system has is the decisions the user made themselves:

    1. `predictions.classification_method == 'manual'` — a person picked this
       category on this row in the Review Queue.
    2. `counterparty_memory` — a person said "everything from this party is X".

This script replays the classifier over exactly those rows and reports how often
it would have reached the same answer unaided. That is a real held-out score on
real Indian bank narrations, which is more than the ML model's own metadata can
claim (it was trained on synthetic data — see the caveat it ships with).

WHAT IT IS NOT. It is measured on the rows a person BOTHERED to review, which
are by definition the harder ones. Expect it to read lower than the true figure
across a whole statement, and treat it as a floor.

    python scripts/measure_accuracy.py
    python scripts/measure_accuracy.py --user someone@example.com

Nothing is written. This only reads.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.categorization.counterparty import group_for
from app.categorization.decisions import REVIEW_THRESHOLD, needs_decision
from app.categorization.deep import anchor_path, classify_deep
from app.categorization.hybrid import METHOD_MANUAL, classify_transaction
from app.categorization.ml_service import ml_service
from app.categorization.taxonomy import UNCATEGORIZED, normalize_category
from app.database.session import SessionLocal
from app.models.account import Account
from app.models.counterparty_memory import CounterpartyMemory
from app.models.prediction import Prediction
from app.models.transaction import Transaction
from app.models.user import User


def _pct(n, d):
    return f"{100.0 * n / d:5.1f}%" if d else "    - "


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user")
    ap.add_argument("--show", type=int, default=15)
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

        print(f"\nML model loaded: {ml_service.is_available}")
        if not ml_service.is_available:
            print("  ^ no artifact found — every number below is the RULE ENGINE alone.")

        for user in users:
            pairs = (db.query(Transaction, Prediction)
                     .outerjoin(Prediction, Prediction.transaction_id == Transaction.id)
                     .filter(Transaction.user_id == user.id,
                             Transaction.superseded_by_id.is_(None))
                     .all())
            if not pairs:
                print(f"\n{user.email}: no transactions.")
                continue

            account_types = {
                a.id: getattr(a, "account_type", None)
                for a in db.query(Account).filter(Account.user_id == user.id).all()
            }

            print(f"\n{'=' * 78}\n{user.email}\n{'=' * 78}")

            # ---------------------------------------------------------------
            # 1. COVERAGE — no labels needed, and not the same as accuracy
            # ---------------------------------------------------------------
            total = len(pairs)
            answered = sum(1 for tx, pred in pairs if not needs_decision(tx, pred))
            ml_admitted = ml_won = 0
            for _tx, pred in pairs:
                if pred is None:
                    continue
                if (pred.ml_category or None) is not None:
                    ml_admitted += 1
                if (pred.classification_method or "").startswith("ml"):
                    ml_won += 1

            print("\n  COVERAGE  (how much was answered without asking anyone)")
            print(f"    transactions                    : {total}")
            print(f"    purpose established             : {answered:>5}  {_pct(answered, total)}")
            print(f"    still needs a person            : {total - answered:>5}"
                  f"  {_pct(total - answered, total)}")
            print(f"    rows where ML had an opinion    : {ml_admitted:>5}  {_pct(ml_admitted, total)}")
            print(f"    rows the ML answer WON          : {ml_won:>5}  {_pct(ml_won, total)}")

            # ---------------------------------------------------------------
            # 2. GROUND TRUTH — what a person actually said
            # ---------------------------------------------------------------
            truth = {}          # transaction id -> human's category
            source = {}
            for tx, pred in pairs:
                if pred is not None and pred.classification_method == METHOD_MANUAL:
                    if pred.predicted_category and pred.predicted_category != UNCATEGORIZED:
                        truth[tx.id] = pred.predicted_category
                        source[tx.id] = "manual review"

            memory = {m.counterparty_key: m.category
                      for m in db.query(CounterpartyMemory)
                      .filter(CounterpartyMemory.user_id == user.id).all()}
            if memory:
                for tx, _pred in pairs:
                    if tx.id in truth:
                        continue
                    cp = group_for(tx.narration_clean or tx.narration_raw or "")
                    if cp and cp.key in memory and memory[cp.key] != UNCATEGORIZED:
                        truth[tx.id] = memory[cp.key]
                        source[tx.id] = "counterparty decision"

            if not truth:
                print("\n  ACCURACY  — CANNOT BE MEASURED YET.")
                print("    Nobody has categorised anything by hand on this account, so")
                print("    there is no ground truth to score against. Answer a dozen")
                print("    counterparties in the Review Queue and run this again.")
                continue

            # ---------------------------------------------------------------
            # 3. ACCURACY — replay the classifier and compare
            # ---------------------------------------------------------------
            right = wrong = abstained = 0
            tree_right = tree_wrong = tree_abstained = 0
            confusion = Counter()
            per_class = defaultdict(lambda: [0, 0])   # label -> [right, total]

            for tx, _pred in pairs:
                if tx.id not in truth:
                    continue
                human = normalize_category(truth[tx.id]) or truth[tx.id]
                narration = tx.narration_clean or tx.narration_raw or ""
                direction = "CREDIT" if tx.credit_paise else "DEBIT"
                acct = account_types.get(tx.account_id)

                flat = classify_transaction(
                    narration,
                    amount=(tx.debit_paise or tx.credit_paise or 0) / 100.0,
                    direction=direction, account_type=acct)

                per_class[human][1] += 1
                if flat.requires_review or flat.category == UNCATEGORIZED:
                    abstained += 1
                elif flat.category == human:
                    right += 1
                    per_class[human][0] += 1
                else:
                    wrong += 1
                    confusion[(human, flat.category)] += 1

                # The same question of the TREE, compared at level 1 — the tree
                # speaks a different vocabulary, so anything deeper would be
                # comparing two things that were never meant to match.
                want = anchor_path(human)
                deep = classify_deep(narration, direction=direction.lower(),
                                     account_type=acct)
                if not want:
                    tree_abstained += 1
                elif deep.confidence < REVIEW_THRESHOLD:
                    tree_abstained += 1
                elif deep.path[0] == want[0]:
                    tree_right += 1
                else:
                    tree_wrong += 1

            n = right + wrong + abstained
            decided = right + wrong
            print(f"\n  ACCURACY  (scored against {n} rows a HUMAN categorised)")
            print(f"    ground truth from               : "
                  f"{Counter(source.values()).most_common()}")
            print(f"\n    RULE ENGINE + ML")
            print(f"      abstained (asked a person)    : {abstained:>5}  {_pct(abstained, n)}")
            print(f"      answered                      : {decided:>5}  {_pct(decided, n)}")
            print(f"      ...of those, CORRECT          : {right:>5}  {_pct(right, decided)}"
                  f"   <- this is the accuracy figure")
            print(f"      ...of those, wrong            : {wrong:>5}  {_pct(wrong, decided)}")

            tn = tree_right + tree_wrong
            print(f"\n    HIERARCHY (level 1 only)")
            print(f"      not comparable / abstained    : {tree_abstained:>5}")
            print(f"      answered                      : {tn:>5}")
            print(f"      ...of those, CORRECT          : {tree_right:>5}  {_pct(tree_right, tn)}")

            if confusion:
                print(f"\n  Where it goes wrong (top {args.show}):")
                for (human, got), c in confusion.most_common(args.show):
                    print(f"    {c:>4}x  human said {human[:24]:<26} "
                          f"engine said {got[:24]}")

            weak = sorted(((lab, r, t) for lab, (r, t) in per_class.items() if t >= 3),
                          key=lambda x: x[1] / x[2])
            if weak:
                print(f"\n  Per category (only those with 3+ labelled rows):")
                for lab, r, t in weak:
                    print(f"    {lab[:30]:<32} {r:>3}/{t:<3} {_pct(r, t)}")

            print("\n  Nothing was written.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
