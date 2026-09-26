"""How many counterparties is the review queue actually asking about?

The feature exists to remove human work, so the number that matters is not
"how many transactions were categorised" — it is **how many questions a person
still has to answer**. On a real 1,823-row statement that number was 73, which
is worse than filing the statement by hand.

This reports it, and shows what the trade-name rules would change, WITHOUT
writing anything. Run it before and after `Re-categorize existing` to see the
number move.

    python scripts/counterparty_review_load.py
    python scripts/counterparty_review_load.py --user someone@example.com
    python scripts/counterparty_review_load.py --show 40

The breakdown is the useful part: a counterparty that would newly resolve is
listed with the word that resolved it, so a wrong rule is visible here rather
than after it has relabelled a thousand rows.
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import or_

from app.categorization import trades
from app.categorization.counterparty import group_for as group_for_review
from app.categorization.hybrid import classify_transaction
from app.database.session import SessionLocal
from app.models.account import Account
from app.models.prediction import Prediction
from app.models.transaction import Transaction
from app.models.user import User

UNCATEGORIZED = "Uncategorized"


def outstanding_rows(db, user_id):
    """Exactly what the review queue asks about — same filter, same result."""
    return (
        db.query(Transaction, Prediction)
        .outerjoin(Prediction, Prediction.transaction_id == Transaction.id)
        .filter(
            Transaction.user_id == user_id,
            Transaction.superseded_by_id.is_(None),
        )
        .filter(or_(
            Prediction.requires_review.is_(True),
            Transaction.category_id.is_(None),
            Prediction.predicted_category == UNCATEGORIZED,
        ))
        .all()
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="limit to one user's email")
    ap.add_argument("--show", type=int, default=25,
                    help="how many counterparties to list (default 25)")
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
            rows = outstanding_rows(db, user.id)
            if not rows:
                print(f"\n{user.email}: review queue is empty.")
                continue

            account_types = {
                a.id: getattr(a, "account_type", None)
                for a in db.query(Account).filter(Account.user_id == user.id).all()
            }

            groups = {}
            for tx, _pred in rows:
                narration = tx.narration_clean or tx.narration_raw or ""
                cp = group_for_review(narration)
                if not cp:
                    continue
                g = groups.setdefault(cp.key, {
                    "display": cp.display, "count": 0, "sample": narration,
                    "direction": "credit" if tx.credit_paise else "debit",
                    "account_type": account_types.get(tx.account_id),
                })
                g["count"] += 1

            resolved, remaining = [], []
            for key, g in groups.items():
                outcome = classify_transaction(
                    g["sample"], direction=g["direction"].upper(),
                    account_type=g["account_type"],
                )
                if outcome.requires_review or outcome.category == UNCATEGORIZED:
                    remaining.append((key, g, None))
                else:
                    hit = trades.match(g["sample"], direction=g["direction"],
                                       account_type=g["account_type"])
                    resolved.append((key, g, outcome, hit))

            total = len(groups)
            print(f"\n{'=' * 72}")
            print(f"{user.email}")
            print(f"{'=' * 72}")
            print(f"  transactions waiting      : {len(rows)}")
            print(f"  counterparties to decide  : {total}")
            print(f"  the classifier can now do : {len(resolved)}")
            print(f"  LEFT FOR A HUMAN          : {len(remaining)}")

            if resolved:
                print(f"\n  Resolved without asking (top {args.show}):")
                for key, g, outcome, hit in sorted(
                        resolved, key=lambda r: -r[1]["count"])[:args.show]:
                    why = (f'"{hit.matched_word}"' if hit
                           else outcome.classification_rule or outcome.classification_method)
                    print(f"    {g['count']:>4} rows  {g['display'][:34]:<36} "
                          f"-> {outcome.category:<22} ({why})")

            if remaining:
                print(f"\n  Still needs a person (top {args.show}):")
                for key, g, _ in sorted(remaining, key=lambda r: -r[1]["count"])[:args.show]:
                    print(f"    {g['count']:>4} rows  {g['display'][:34]:<36} "
                          f"{g['sample'][:44]}")

            # Which trades are carrying the load. A single keyword doing all the
            # work is a sign the table is overfitted to one statement.
            words = Counter(hit.matched_word for _k, _g, _o, hit in resolved if hit)
            if words:
                print("\n  Trade words that fired: "
                      + ", ".join(f"{w}×{n}" for w, n in words.most_common(12)))

            print("\n  Nothing was written. Run 'Re-categorize existing' in the "
                  "Review Queue to apply this.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
