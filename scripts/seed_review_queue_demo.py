"""Seed a demo user with genuinely difficult transactions for review-queue testing.

Creates an isolated demo user, one bank account, and a corpus of transactions
chosen to exercise every path through the hybrid classifier — especially the
abstain path that populates the review queue.

    python -m scripts.seed_review_queue_demo --corpus mlmodel/artifacts/hard_corpus.json
    python -m scripts.seed_review_queue_demo --report-only

This writes ONLY to its own demo user. It never touches existing ledger rows.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import uuid
from typing import Any, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.categorization.hybrid import classify_transaction  # noqa: E402
from app.categorization.taxonomy import UNCATEGORIZED  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.models.category import Category  # noqa: E402
from app.models.prediction import Prediction  # noqa: E402
from app.models.transaction import Direction, SourceType, Transaction  # noqa: E402
from app.models.user import User  # noqa: E402
from app.utils.security import hash_password  # noqa: E402

DEMO_EMAIL = "review.demo@kredo.local"
DEMO_PASSWORD = "ReviewDemo123!"


# Fallback corpus used when no --corpus file is supplied. Every entry is chosen
# for a specific reason, noted inline, so the demo exercises real behaviour
# rather than random noise.
FALLBACK_CORPUS: List[Dict[str, Any]] = [
    # --- Local/regional merchants absent from the rule config ---------------
    {"narration": "UPI-SRI LAKSHMI TRADERS-8871", "amount": 2450, "direction": "DEBIT"},
    {"narration": "POS ANNAPURNA STORES BLR", "amount": 1180, "direction": "DEBIT"},
    {"narration": "UPI/VENKATESHWARA ENTERPRISES/5521", "amount": 6700, "direction": "DEBIT"},
    {"narration": "NEFT-M/S RAGHAV AND SONS", "amount": 18900, "direction": "DEBIT"},
    {"narration": "UPI-NEW MODERN AGENCIES", "amount": 3400, "direction": "DEBIT"},
    # --- Pure reference codes: no signal at all -----------------------------
    {"narration": "TXN 99381726 REF 5512", "amount": 890, "direction": "DEBIT"},
    {"narration": "MMT/IMPS/409218831/", "amount": 12000, "direction": "DEBIT"},
    {"narration": "CLG/000212/INW", "amount": 45000, "direction": "CREDIT"},
    {"narration": "BY CASH DEPOSIT MACHINE", "amount": 25000, "direction": "CREDIT"},
    # --- Generic corporate names spanning several plausible categories ------
    {"narration": "NEFT-GLOBAL SOLUTIONS PVT LTD", "amount": 78000, "direction": "DEBIT"},
    {"narration": "UPI-PRIME SERVICES INDIA", "amount": 5600, "direction": "DEBIT"},
    {"narration": "RTGS-UNITED VENTURES LIMITED", "amount": 250000, "direction": "DEBIT"},
    {"narration": "NEFT SUNRISE HOLDINGS", "amount": 96000, "direction": "CREDIT"},
    # --- Contradictory signals: merchant inside a transfer instruction ------
    {"narration": "UPI TRANSFER TO ZOMATO DELIVERY PARTNER", "amount": 900, "direction": "DEBIT"},
    {"narration": "IMPS FUND TRANSFER AMAZON SELLER AC", "amount": 34000, "direction": "DEBIT"},
    # --- Fee-like wording that is NOT a bank fee ---------------------------
    {"narration": "UPI-CONVENIENCE FEE MOVIE BOOKING", "amount": 45, "direction": "DEBIT"},
    {"narration": "DOCTOR CONSULTATION FEE PAID", "amount": 800, "direction": "DEBIT"},
    {"narration": "COURIER HANDLING CHARGE", "amount": 120, "direction": "DEBIT"},
    # --- Bank fees phrased unusually ---------------------------------------
    {"narration": "DR-CHRG-NONMAINT-QTR", "amount": 590, "direction": "DEBIT"},
    {"narration": "COLL CHGS PLUS GST 18PCT", "amount": 236, "direction": "DEBIT"},
    # --- Refunds/reversals where the merchant decides ----------------------
    {"narration": "REVERSAL UPI SRI LAKSHMI TRADERS", "amount": 2450, "direction": "CREDIT"},
    {"narration": "RETURN OF FUNDS FAILED TXN", "amount": 1500, "direction": "CREDIT"},
    # --- Concatenated tokens, unknown merchant ----------------------------
    {"narration": "UPI-KRISHNAMEDICALHALL-2231", "amount": 640, "direction": "DEBIT"},
    {"narration": "POSSHREEDEVIFANCYSTORE", "amount": 310, "direction": "DEBIT"},
    {"narration": "NEFTGREENVALLEYSCHOOLFEES", "amount": 42000, "direction": "DEBIT"},
    # --- Genuinely ambiguous between two categories ------------------------
    {"narration": "UPI-RELIANCE SMART POINT CAFE", "amount": 780, "direction": "DEBIT"},
    {"narration": "AIRPORT LOUNGE RESTAURANT BLR", "amount": 1900, "direction": "DEBIT"},
    {"narration": "HOTEL GRAND CANTEEN PAYMENT", "amount": 2200, "direction": "DEBIT"},
    # --- Should be auto-decided (proves the queue filters correctly) -------
    {"narration": "UPI-SWIGGY ORDER-88213", "amount": 430, "direction": "DEBIT"},
    {"narration": "NETFLIX SUBSCRIPTION MONTHLY", "amount": 649, "direction": "DEBIT"},
    {"narration": "SALARY CREDIT MAR 2026", "amount": 185000, "direction": "CREDIT"},
    {"narration": "UBER INDIA SYSTEMS", "amount": 380, "direction": "DEBIT"},
    {"narration": "ATM CASH WITHDRAWAL FEE", "amount": 21, "direction": "DEBIT"},
    {"narration": "APOLLO PHARMACY BANGALORE", "amount": 1240, "direction": "DEBIT"},
    {"narration": "UPI TRANSFER TO SELF HDFC", "amount": 50000, "direction": "DEBIT"},
    {"narration": "ZERODHA BROKING LTD", "amount": 30000, "direction": "DEBIT"},
]


def ensure_demo_user(db) -> tuple:
    user = db.query(User).filter(User.email == DEMO_EMAIL).first()
    if not user:
        user = User(
            id=uuid.uuid4(), email=DEMO_EMAIL,
            hashed_password=hash_password(DEMO_PASSWORD),
            full_name="Review Queue Demo",
        )
        db.add(user)
        db.commit()

    account = db.query(Account).filter(
        Account.user_id == user.id, Account.deleted_at == None
    ).first()
    if not account:
        account = Account(
            id=uuid.uuid4(), user_id=user.id, bank_code="HDFC",
            account_number_masked="****4242", account_type="CURRENT", currency="INR",
        )
        db.add(account)
        db.commit()

    return user, account


def wipe_demo_transactions(db, user) -> int:
    tx_ids = [t.id for t in db.query(Transaction).filter(Transaction.user_id == user.id).all()]
    if tx_ids:
        db.query(Prediction).filter(Prediction.transaction_id.in_(tx_ids)).delete(synchronize_session=False)
    n = db.query(Transaction).filter(Transaction.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    return n


def seed(corpus: List[Dict[str, Any]], report_only: bool = False) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        user, account = ensure_demo_user(db)
        if not report_only:
            removed = wipe_demo_transactions(db, user)
            print(f"Cleared {removed} previous demo transactions for {DEMO_EMAIL}")

        stats = {"review": 0, "rule": 0, "ml": 0, "hybrid": 0, "none": 0}
        rows = []
        base_date = datetime.date(2026, 3, 1)

        for i, case in enumerate(corpus):
            narration = case["narration"]
            amount = float(case.get("amount", 1000))
            direction = str(case.get("direction", "DEBIT")).upper()

            result = classify_transaction(narration, amount=amount, direction=direction)
            stats[result.classification_method] = stats.get(result.classification_method, 0) + 1
            if result.requires_review:
                stats["review"] += 1

            rows.append((case, result))

            if report_only:
                continue

            paise = int(round(amount * 100))
            is_credit = direction == "CREDIT"
            tx = Transaction(
                id=uuid.uuid4(), user_id=user.id, account_id=account.id,
                direction=Direction.CREDIT if is_credit else Direction.DEBIT,
                debit_paise=None if is_credit else paise,
                credit_paise=paise if is_credit else None,
                narration_raw=narration, narration_clean=narration.upper(),
                txn_date=base_date + datetime.timedelta(days=i % 28),
                source_type=SourceType.STATEMENT, source_channel="upload",
                row_index=i,
            )

            category_id = None
            if not result.requires_review and result.category != UNCATEGORIZED:
                cat = db.query(Category).filter(Category.name == result.category).first()
                if not cat:
                    cat = Category(id=uuid.uuid4(), name=result.category)
                    db.add(cat)
                    db.flush()
                category_id = cat.id
                tx.category_id = category_id

            db.add(tx)
            db.flush()

            db.add(Prediction(
                id=uuid.uuid4(), transaction_id=tx.id, category_id=category_id,
                predicted_category=result.category,
                confidence=result.classification_confidence,
                rule_used=result.classification_rule,
                classification_method=result.classification_method,
                model_name=result.model_name,
                model_confidence=result.model_confidence,
                rule_score=result.rule_score,
                rule_category=result.rule_category,
                ml_category=result.ml_category,
                top_3=[[c, round(p, 4)] for c, p in result.top_3],
                explanation=result.explanation,
                requires_review=result.requires_review,
            ))

        if not report_only:
            db.commit()

        return {"user": user, "account": account, "stats": stats, "rows": rows}
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", help="JSON file with a 'cases' list")
    parser.add_argument("--report-only", action="store_true", help="Classify and report, write nothing")
    args = parser.parse_args()

    corpus = FALLBACK_CORPUS
    if args.corpus and os.path.exists(args.corpus):
        with open(args.corpus, encoding="utf-8") as f:
            data = json.load(f)
        corpus = data.get("cases", data) if isinstance(data, dict) else data
        print(f"Loaded {len(corpus)} cases from {args.corpus}")
    else:
        print(f"Using built-in corpus of {len(corpus)} cases")

    out = seed(corpus, report_only=args.report_only)
    stats, rows = out["stats"], out["rows"]

    print("\n" + "=" * 92)
    print(f"{'NARRATION':<44} {'RESULT':<18} {'METHOD':<8} {'CONF':>6}  REVIEW")
    print("=" * 92)
    for case, r in rows:
        flag = "YES" if r.requires_review else ""
        print(f"{case['narration'][:43]:<44} {r.category[:17]:<18} "
              f"{r.classification_method:<8} {r.classification_confidence:>6.2f}  {flag}")

    total = len(rows)
    print("=" * 92)
    print(f"Total: {total}")
    print(f"  Sent to review : {stats['review']}  ({stats['review']/total*100:.0f}%)")
    print(f"  Auto-decided   : {total - stats['review']}  ({(total-stats['review'])/total*100:.0f}%)")
    print(f"  By method      : " + ", ".join(f"{k}={v}" for k, v in stats.items() if k != "review"))

    if not args.report_only:
        print(f"\nDemo login: {DEMO_EMAIL} / {DEMO_PASSWORD}")
        print("Open /review-queue and select the 'Uncategorized Transactions' tab.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
