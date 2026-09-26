"""Rule-engine accuracy on REAL labelled bank narrations.

WHY THIS EXISTS. The ML model ships with an honest generalisation score
(macro F1 0.7488 on unseen phrasings) and a caveat saying it was trained on
synthetic data whose 180,000 rows collapse to 278 templates. The RULE ENGINE —
which answers most rows in production, because the ML is gated out on
unfamiliar vocabulary — had no number at all. "244 of 569 categorised" is
COVERAGE, and a classifier that guesses on everything has 100% coverage.

`data.csv` in the project root is 2,862 real narrations from real Indian bank
statements with a human-assigned `target_category`. That is ground truth, and
this scores against it.

    python scripts/eval_rule_engine.py
    python scripts/eval_rule_engine.py --csv data_combined.csv
    python scripts/eval_rule_engine.py --show-errors 40

THE ONE THING THAT MAKES THIS FAIR.

The labels use the user's own vocabulary, and some of it names a payment RAIL
rather than a purpose: `NEFT Transfer`, `UPI Transfer`, `IMPS Transfer`,
`UPI Received`. This engine deliberately refuses to emit those — see
`test_a_rail_named_category_is_deliberately_not_mapped`, written after guessing
a purpose from a rail put 853 unrelated rows in one bucket. Scoring a refusal
as a wrong answer would punish the engine for the behaviour it was designed to
have, so those rows are reported in their OWN bucket and excluded from the
headline number. Both figures are printed; neither is hidden.

Nothing touches the database. Reads a CSV, prints numbers.
"""
import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.categorization.hybrid import classify_transaction
from app.categorization.ml_service import ml_service
from app.categorization.taxonomy import UNCATEGORIZED

# The user's labels, mapped to the vocabulary this engine actually speaks.
# Written out rather than fuzzy-matched so a disputed mapping is visible and
# arguable instead of buried in a similarity score.
LABEL_MAP = {
    "settlement":                       "Sales Income",
    "merchant settlement":              "Sales Income",
    "customer payment / neft transfer": "Sales Income",
    "salary":                           "Salary & Wages",
    "bank charges":                     "Bank Fees",
    "food":                             "Food & Dining",
    "utilities":                        "Utilities & Bills",
    "tax":                              "Taxes & Statutory",
    "government fee":                   "Taxes & Statutory",
    "vendor payment":                   "Cost of Goods",
    "interest":                         "Interest & Finance Cost",
    "investment":                       "Owner Funding",
    "loan disbursement":                "Loans & Borrowing",
    "loan repayment / emi":             "Loans & Borrowing",
    "internal fund transfer":           "Internal Movement",
    "self transfer":                    "Internal Movement",
    "cash deposit":                     "Internal Movement",
    "refund":                           "Other Income",
    "others":                           "Other",
}

# Labels that name HOW the money moved, not what it was for. The engine refuses
# these on purpose; counting the refusal as an error would be scoring it against
# a design it deliberately does not have.
RAIL_LABELS = {
    "neft transfer", "upi transfer", "imps transfer", "rtgs transfer",
    "upi received",
}

# The SAME answer written two ways. This codebase carries a legacy vocabulary
# alongside the current one, and `Bank Charges` / `Bank Fees` are one category.
# Scoring these as misses measures the rename, not the classifier.
EQUIVALENT = {
    frozenset(("Bank Fees", "Bank Charges")),
    frozenset(("Salary & Wages", "Salary / Income")),
    frozenset(("Utilities & Bills", "Utilities")),
    frozenset(("Food & Dining", "Food")),
}

# A REAL disagreement about what the right answer is, not a bug in either side.
# On a restaurant's current account, buying food IS cost of goods — the engine
# is arguably more correct than the label. Counted separately and printed,
# because quietly scoring it either way would be picking a side in an argument
# the reader should get to see.
DISPUTED = {
    ("Food & Dining", "Cost of Goods"),
}


def _same(want, got):
    return want == got or frozenset((want, got)) in EQUIVALENT


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data.csv")
    ap.add_argument("--show-errors", type=int, default=20)
    ap.add_argument("--account-type", default="current")
    args = ap.parse_args()

    path = args.csv
    if not os.path.exists(path):
        print(f"No such file: {path}")
        return 1

    rows = [r for r in csv.DictReader(open(path, encoding="utf-8-sig"))
            if (r.get("narration") or "").strip()
            and (r.get("target_category") or "").strip()]
    if not rows:
        print(f"{path} has no rows with both a narration and a target_category.")
        return 1

    print(f"\n{'=' * 78}")
    print(f"RULE ENGINE ACCURACY — {path}")
    print(f"{'=' * 78}")
    print(f"  labelled rows        : {len(rows)}")
    print(f"  ML model loaded      : {ml_service.is_available}"
          f"{'' if ml_service.is_available else '   (rules only)'}")

    right = wrong = abstained = disputed = 0
    # A third state that matters more than it looks. Many rules answer
    # PROVISIONALLY — `certain=False` in `purpose_rules` — meaning "here is a
    # category, please confirm". Lumping those in with a blank abstention hides
    # the fact that the engine already knows the answer and is only asking for
    # a nod, which is a completely different amount of human work.
    prov_right = prov_wrong = prov_blank = 0
    rail_rows = rail_abstained = 0
    unmapped = Counter()
    confusion = Counter()
    per_class = defaultdict(lambda: [0, 0, 0])   # want -> [right, wrong, abstain]
    errors = []

    for r in rows:
        label = (r["target_category"] or "").strip().lower()
        narration = r["narration"].strip()
        dep = float(r.get("deposit") or 0 or 0)
        wdl = float(r.get("withdrawal") or 0 or 0)
        direction = "CREDIT" if dep > 0 else "DEBIT"
        amount = dep or wdl

        result = classify_transaction(narration, amount=amount,
                                      direction=direction,
                                      account_type=args.account_type)
        gave_up = result.requires_review or result.category == UNCATEGORIZED

        if label in RAIL_LABELS:
            rail_rows += 1
            rail_abstained += gave_up
            continue

        want = LABEL_MAP.get(label)
        if not want:
            unmapped[label] += 1
            continue

        slot = per_class[want]
        if gave_up:
            abstained += 1
            slot[2] += 1
            if result.category == UNCATEGORIZED:
                prov_blank += 1
            elif _same(want, result.category):
                prov_right += 1
            elif (want, result.category) not in DISPUTED:
                prov_wrong += 1
        elif _same(want, result.category):
            right += 1
            slot[0] += 1
        elif (want, result.category) in DISPUTED:
            disputed += 1
        else:
            wrong += 1
            slot[1] += 1
            confusion[(want, result.category)] += 1
            if len(errors) < args.show_errors:
                errors.append((narration, want, result.category,
                               result.classification_rule))

    scored = right + wrong + abstained
    answered = right + wrong

    def pct(n, d):
        return f"{100.0 * n / d:5.1f}%" if d else "    - "

    print(f"\n  SCORED ROWS          : {scored}")
    print(f"    answered           : {answered:>5}  {pct(answered, scored)}"
          f"   <- coverage")
    print(f"    abstained          : {abstained:>5}  {pct(abstained, scored)}")
    print(f"\n  ACCURACY (of the rows it answered)")
    print(f"    correct            : {right:>5}  {pct(right, answered)}"
          f"   <<< THE NUMBER")
    print(f"    wrong              : {wrong:>5}  {pct(wrong, answered)}")
    print(f"\n  END-TO-END (abstentions counted as misses)")
    print(f"    correct            : {right:>5}  {pct(right, scored)}")

    prov_named = prov_right + prov_wrong
    print(f"\n  OF THE {abstained} ABSTENTIONS — what the engine already knew")
    print(f"    named a category, asked to confirm : {prov_named:>5}"
          f"  {pct(prov_named, abstained)}")
    print(f"      ...and it was RIGHT              : {prov_right:>5}"
          f"  {pct(prov_right, prov_named)}")
    print(f"      ...and it was wrong              : {prov_wrong:>5}"
          f"  {pct(prov_wrong, prov_named)}")
    print(f"    genuinely blank                    : {prov_blank:>5}"
          f"  {pct(prov_blank, abstained)}")
    print(f"\n  IF PROVISIONAL ANSWERS WERE ACCEPTED")
    print(f"    correct out of {scored:<5}               : "
          f"{right + prov_right:>5}  {pct(right + prov_right, scored)}")

    if disputed:
        print(f"\n  EXCLUDED — {disputed} rows where the label and the engine "
              f"disagree about the\n    right answer rather than one being "
              f"wrong. See DISPUTED above.")

    if rail_rows:
        print(f"\n  EXCLUDED — labels that name a rail, not a purpose: {rail_rows}")
        print(f"    the engine refused {rail_abstained} of them, which is the "
              f"designed behaviour.")
        print(f"    Counting these as errors would score the engine against a "
              f"design it\n    deliberately does not have. See RAIL_LABELS above.")

    if unmapped:
        print(f"\n  EXCLUDED — labels with no mapping into this vocabulary:")
        for lab, n in unmapped.most_common():
            print(f"    {n:>5}  {lab!r}")

    if confusion:
        print(f"\n  Where it goes wrong:")
        for (want, got), n in confusion.most_common(15):
            print(f"    {n:>4}x  label {want[:24]:<26} engine {got[:24]}")

    print(f"\n  Per category:")
    print(f"    {'category':<26} {'right':>6} {'wrong':>6} {'abstain':>8} {'acc':>7}")
    for want in sorted(per_class, key=lambda k: -sum(per_class[k])):
        r_, w_, a_ = per_class[want]
        print(f"    {want:<26} {r_:>6} {w_:>6} {a_:>8} {pct(r_, r_ + w_):>7}")

    if errors:
        print(f"\n  Sample mistakes:")
        for narration, want, got, rule in errors:
            print(f"    {narration[:52]:54}")
            print(f"      label {want:<24} engine {got:<24} via {rule}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
