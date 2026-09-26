"""What is left after `Other / Uncategorized` was removed, and what shape is it?

THE POINT. Deleting that node did not make the unknown go away — it stopped the
unknown pretending to be a category. Rows that name no purpose are now filed by
the rail they used, which is a fact about them, and flagged for review, which is
a status. This script lists them.

It is written to be READ FOR PATTERNS, not for totals. Narrations are collapsed
into a SHAPE — digits become `#`, long hex/reference runs become `<ref>` — so a
thousand rows reading `IMPS/P2A/512345678/...` collapse to one line with a count
next to it. A shape with 200 rows behind it is one rule away from being solved;
a shape with 1 row behind it is not worth a rule and should stay in the queue.

    python scripts/unreadable_narrations.py
    python scripts/unreadable_narrations.py --user someone@example.com --show 60

Nothing is written. This only reads.
"""
import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import or_

from app.categorization import hierarchy as H
from app.categorization.deep import REVIEW_THRESHOLD, classify_deep
from app.database.session import SessionLocal
from app.models.account import Account
from app.models.transaction import Transaction
from app.models.user import User

# The branches a row reaches when nothing named a purpose. Kept in step with
# `hierarchy.residual_path`; a placement outside this set came from evidence.
RESIDUAL_PATHS = {
    " > ".join(p) for p in (
        H.RESIDUAL_ATM_OUT, H.RESIDUAL_CASH_IN, H.RESIDUAL_CASH_OUT,
        H.RESIDUAL_VENDOR, H.RESIDUAL_PERSON, H.RESIDUAL_TRANSFER,
    )
}

_REF = re.compile(r"\b[A-Z0-9]{8,}\b")
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def shape(narration: str) -> str:
    """Collapse a narration to the part a RULE could key on.

    Reference numbers and amounts are the parts that differ between two rows
    that are otherwise the same transaction. Removing them is what turns 854
    unreadable rows into a dozen readable questions.
    """
    s = (narration or "").upper()
    s = _REF.sub("<ref>", s)
    s = _NUM.sub("#", s)
    s = re.sub(r"#+", "#", s)
    return _WS.sub(" ", s).strip()[:70]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="limit to one user's email")
    ap.add_argument("--show", type=int, default=30)
    ap.add_argument("--min-rows", type=int, default=2,
                    help="hide shapes with fewer rows than this (default 2)")
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
            rows = (db.query(Transaction)
                    .filter(Transaction.user_id == user.id,
                            Transaction.superseded_by_id.is_(None))
                    .all())
            if not rows:
                print(f"\n{user.email}: no transactions.")
                continue

            account_types = {
                a.id: getattr(a, "account_type", None)
                for a in db.query(Account).filter(Account.user_id == user.id).all()
            }

            unclear = []
            for tx in rows:
                conf = tx.category_confidence
                if conf is not None:
                    if float(conf) < REVIEW_THRESHOLD:
                        unclear.append(tx)
                elif not (tx.category or tx.legacy_category):
                    unclear.append(tx)

            shapes = defaultdict(lambda: {"n": 0, "sample": "", "where": set()})
            for tx in unclear:
                narration = tx.narration_clean or tx.narration_raw or ""
                g = shapes[shape(narration)]
                g["n"] += 1
                g["sample"] = g["sample"] or narration
                g["where"].add(tx.category_path or tx.category or "-")

            print(f"\n{'=' * 78}")
            print(f"{user.email}")
            print(f"{'=' * 78}")
            print(f"  transactions                     : {len(rows)}")
            print(f"  purpose not established          : {len(unclear)}"
                  f"  ({100 * len(unclear) // max(1, len(rows))}%)")
            print(f"  distinct narration shapes        : {len(shapes)}")

            ranked = sorted(shapes.items(), key=lambda kv: -kv[1]["n"])
            worth_a_rule = [(k, v) for k, v in ranked if v["n"] >= args.min_rows]
            once_only = len(ranked) - len(worth_a_rule)

            print(f"  shapes worth writing a rule for  : {len(worth_a_rule)}")
            print(f"  one-off shapes (leave in queue)  : {once_only}")

            print(f"\n  Top {args.show} shapes — each is one rule away from being solved:")
            print(f"  {'rows':>5}  {'shape':<58}  currently filed at")
            for key, v in worth_a_rule[:args.show]:
                where = sorted(v["where"])[0]
                print(f"  {v['n']:>5}  {key:<58}  {where[:34]}")

            # What the classifier says about each shape's sample, so a shape
            # that ALREADY resolves (and is only here because the ledger is
            # stale) is distinguishable from one that needs a new rule.
            stale = 0
            for key, v in worth_a_rule:
                r = classify_deep(v["sample"],
                                  direction="debit",
                                  account_type=account_types.get(None))
                if r.source not in {"residual", "vendor_unknown", "person"}:
                    stale += 1
            if stale:
                print(f"\n  {stale} of these shapes ALREADY resolve with today's rules — "
                      f"the ledger is stale.\n  Run 'Re-categorize existing' in the "
                      f"Review Queue before writing any new rule.")

            print("\n  Nothing was written.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
