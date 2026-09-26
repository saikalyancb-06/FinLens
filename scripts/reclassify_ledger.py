"""Re-run the hybrid categoriser over the existing ledger.

DRY-RUN BY DEFAULT. Prints every category change it would make, grouped and
counted, and writes nothing without --apply --confirm.

    python -m scripts.reclassify_ledger                        # preview everything
    python -m scripts.reclassify_ledger --user <uuid>          # preview one user
    python -m scripts.reclassify_ledger --apply --confirm      # write

Safety rules, enforced in code rather than left to the operator:

* A human decision is never overwritten. Any Prediction with
  classification_method == 'manual' is skipped outright, and the count of skipped
  rows is reported so the operator can see the job respected them.
* An upstream classification (Account Aggregator, email alert) is only replaced
  when the new engine produces a confident answer, never when it abstains.
* Nothing is deleted. Categories are reassigned and provenance rewritten in
  place; transaction amounts, dates, accounts and hashes are untouched.
* Reclassification is idempotent: running twice produces the same result.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import uuid
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.categorization.hybrid import METHOD_MANUAL, classify_transaction  # noqa: E402
from app.categorization.taxonomy import UNCATEGORIZED  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.models.category import Category  # noqa: E402
from app.models.prediction import Prediction  # noqa: E402
from app.models.transaction import Transaction  # noqa: E402

# Methods that represent a decision made outside this classifier and which are
# therefore only superseded by a confident result.
UPSTREAM_METHODS = {"upstream"}


def _amount_rupees(t: Transaction) -> float:
    if t.debit_paise:
        return t.debit_paise / 100.0
    if t.credit_paise:
        return t.credit_paise / 100.0
    return 0.0


def build_plan(
    db,
    user_id: Optional[str] = None,
    limit: Optional[int] = None,
    only_uncategorized: bool = False,
) -> Dict[str, Any]:
    """Compute the reclassification plan.

    `only_uncategorized` restricts the job to transactions that carry no usable
    category today. This exists because the live ledger uses a 43-term B2B
    treasury vocabulary ("Customer Payment / NEFT Transfer", "Merchant
    Settlement", "Vendor Payment", "GST/Tax Payment") while the classifier
    speaks the 15-term consumer taxonomy. Running unscoped would replace ~3,700
    business classifications with Uncategorized and flatten customer payments
    into a generic "Transfers" bucket — a loss of meaning a treasury product
    cannot absorb. Scoped, the job only ever adds classification where there was
    none, so it is safe to run before that taxonomy question is settled.
    """
    q = (
        db.query(Transaction, Prediction)
        .outerjoin(Prediction, Prediction.transaction_id == Transaction.id)
        .filter(Transaction.superseded_by_id == None)
    )
    if user_id:
        q = q.filter(Transaction.user_id == uuid.UUID(str(user_id)))
    if limit:
        q = q.limit(limit)

    categories = {c.id: c.name for c in db.query(Category).all()}

    changes: List[Dict[str, Any]] = []
    unchanged = 0
    skipped_manual = 0
    skipped_upstream = 0
    skipped_categorized = 0
    transitions = Counter()
    new_review = 0
    resolved_review = 0

    for tx, pred in q.all():
        method = pred.classification_method if pred else None

        if method == METHOD_MANUAL:
            skipped_manual += 1
            continue

        narration = tx.narration_clean or tx.narration_raw or ""
        result = classify_transaction(
            narration,
            amount=_amount_rupees(tx),
            direction=tx.direction.value if tx.direction else None,
        )

        current = categories.get(tx.category_id) if tx.category_id else (
            pred.predicted_category if pred else None
        ) or UNCATEGORIZED

        # Scoped mode: never touch a transaction that already carries a category.
        if only_uncategorized and current != UNCATEGORIZED:
            skipped_categorized += 1
            continue

        # An upstream decision is kept unless the classifier is now confident.
        if method in UPSTREAM_METHODS and result.requires_review:
            skipped_upstream += 1
            continue

        new_category = result.category

        # A transaction with no Prediction row has no provenance at all: it shows
        # up in the review queue with a blank explanation and no suggestions, so
        # the reviewer has nothing to act on. Always write one, even when the
        # category itself is unchanged.
        if pred is None:
            transitions[(current, new_category)] += 1 if current != new_category else 0
            changes.append({
                "transaction_id": tx.id,
                "narration": narration,
                "amount": _amount_rupees(tx),
                "from": current,
                "to": new_category,
                "method": result.classification_method,
                "confidence": result.classification_confidence,
                "requires_review": result.requires_review,
                "provenance_backfill": True,
                "result": result,
            })
            continue

        if current == new_category:
            unchanged += 1
            continue

        if new_category == UNCATEGORIZED:
            new_review += 1
        elif current == UNCATEGORIZED:
            resolved_review += 1

        transitions[(current, new_category)] += 1
        changes.append({
            "transaction_id": tx.id,
            "narration": narration,
            "amount": _amount_rupees(tx),
            "from": current,
            "to": new_category,
            "method": result.classification_method,
            "confidence": result.classification_confidence,
            "requires_review": result.requires_review,
            "result": result,
        })

    return {
        "changes": changes,
        "unchanged": unchanged,
        "skipped_manual": skipped_manual,
        "skipped_upstream": skipped_upstream,
        "skipped_categorized": skipped_categorized,
        "transitions": transitions,
        "new_review": new_review,
        "resolved_review": resolved_review,
    }


def apply_plan(db, plan: Dict[str, Any]) -> int:
    cat_by_name = {c.name: c for c in db.query(Category).all()}
    now = datetime.datetime.utcnow()
    applied = 0

    for change in plan["changes"]:
        tx = db.query(Transaction).filter(Transaction.id == change["transaction_id"]).first()
        if tx is None:
            continue
        result = change["result"]

        if result.requires_review or result.category == UNCATEGORIZED:
            tx.category_id = None
        else:
            cat = cat_by_name.get(result.category)
            if cat is None:
                cat = Category(id=uuid.uuid4(), name=result.category)
                db.add(cat)
                db.flush()
                cat_by_name[result.category] = cat
            tx.category_id = cat.id

        pred = db.query(Prediction).filter(Prediction.transaction_id == tx.id).first()
        if pred is None:
            pred = Prediction(id=uuid.uuid4(), transaction_id=tx.id, predicted_category=result.category, confidence=0.0)
            db.add(pred)

        pred.category_id = tx.category_id
        pred.predicted_category = result.category
        pred.confidence = result.classification_confidence
        pred.rule_used = result.classification_rule
        pred.classification_method = result.classification_method
        pred.model_name = result.model_name
        pred.model_confidence = result.model_confidence
        pred.rule_score = result.rule_score
        pred.rule_category = result.rule_category
        pred.ml_category = result.ml_category
        pred.top_3 = [[c, round(p, 4)] for c, p in result.top_3]
        pred.explanation = result.explanation
        pred.requires_review = result.requires_review
        pred.created_at = pred.created_at or now
        applied += 1

    db.commit()
    return applied


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--user", help="Restrict to one user id")
    parser.add_argument("--limit", type=int, help="Only consider N transactions")
    parser.add_argument("--show", type=int, default=25, help="How many example changes to print")
    parser.add_argument("--only-uncategorized", action="store_true",
                        help="Only classify transactions that have no category today (safe: never overwrites)")
    args = parser.parse_args()

    writing = args.apply and args.confirm
    if args.apply and not args.confirm:
        print("--apply requires --confirm. Nothing was written.\n")

    db = SessionLocal()
    try:
        print("=" * 80)
        print(f"LEDGER RECLASSIFICATION {'[APPLY]' if writing else '[DRY RUN - nothing will be written]'}")
        print("=" * 80)

        plan = build_plan(db, user_id=args.user, limit=args.limit,
                          only_uncategorized=args.only_uncategorized)
        if args.only_uncategorized:
            print("\nSCOPE: only transactions with no existing category "
                  "(existing business categories are left untouched)")
        changes = plan["changes"]
        total_considered = (len(changes) + plan["unchanged"] + plan["skipped_manual"]
                            + plan["skipped_upstream"] + plan["skipped_categorized"])

        print(f"\nTransactions considered      : {total_considered}")
        print(f"  unchanged                  : {plan['unchanged']}")
        print(f"  WOULD CHANGE               : {len(changes)}")
        print(f"  skipped (manual decision)  : {plan['skipped_manual']}  <- never overwritten")
        print(f"  skipped (upstream, abstain): {plan['skipped_upstream']}")
        print(f"  skipped (already categorised): {plan['skipped_categorized']}")
        print(f"\n  newly sent to review       : {plan['new_review']}")
        print(f"  review resolved by change  : {plan['resolved_review']}")

        if plan["transitions"]:
            print("\nCategory transitions (from -> to), most common first:")
            for (frm, to), n in plan["transitions"].most_common(30):
                print(f"   {n:>6}  {frm:<20} -> {to}")

        if changes:
            print(f"\nExample changes (showing {min(args.show, len(changes))} of {len(changes)}):")
            for c in changes[: args.show]:
                print(f"   {c['narration'][:44]:<46} {c['from']:<16} -> {c['to']:<16} "
                      f"[{c['method']}] {c['confidence']:.2f}")

        if not writing:
            print("\nDRY RUN - no changes were made.")
            print("To apply:")
            print("    python -m scripts.reclassify_ledger --apply --confirm")
            print("\nBack up the database first. Manual decisions are always preserved.")
            return 0

        applied = apply_plan(db, plan)
        print(f"\nDone. {applied} transactions reclassified.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
