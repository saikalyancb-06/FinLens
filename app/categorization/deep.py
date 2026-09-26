"""Hierarchical classification: resolve the deepest path the evidence supports.

This sits on top of what already exists rather than replacing it. The rule
engine, the model and the hybrid layer already answer "which category", and that
answer is treated here as one input among several — usually the anchor that
fixes level 1. What this module adds is the rest of the path, and, more
importantly, the discipline about when to stop adding.

    upstream says      Food & Dining
    narration says     ZOMATO
    result             Food & Dining > Food Delivery > Zomato

    upstream says      Cost of Goods
    narration says     NEFT-HDFCH250812-MEYER ORGANICS PVT LTD-HDFC BANK
    result             Business & Professional > Supplier Payment > Meyer Organics

    upstream says      nothing
    narration says     NEFT-ABC PVT LTD
    result             Business & Professional > Vendor / Business Transaction
                       > ABC Pvt Ltd            confidence 0.45

That third one is the case worth defending. `ABC PVT LTD` is a company, the
money went out, and that is genuinely everything the statement said. A
classifier that answers `Raw Materials` there is not more useful than one that
answers `Vendor / Business Transaction` — it is less useful, because it looks
equally confident while being wrong most of the time, and a wrong specific
answer is harder to notice than an admitted vague one.

THE DEPTH RULE
--------------
A level is appended only when something in the transaction names it. Never to
fill a column, never because sibling categories are deeper, never because the
tree has a node available. `Transfers > Own Account Transfer` is two levels and
complete. `Other / Uncategorized` is one level and complete.

Order of evidence, strongest first:

  1. What the user already decided about this counterparty (memory). A human
     answered this exact question; nothing computed here outranks that.
  2. A concept in the narration — ATM WDL, GST, EMI, SALARY. The statement is
     naming the purpose outright.
  3. A merchant whose business is the category — Zomato, Indian Oil.
  4. A marketplace, which fixes who but not what, plus a product word if the
     narration happens to carry one.
  5. The upstream classifier's category, mapped onto this tree.
  6. A counterparty that looks like an organisation, filed as an unknown vendor.
  7. Nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.categorization import flow as F
from app.categorization import hierarchy as H
from app.categorization import merchants as M
from app.categorization import trades as T

# ---------------------------------------------------------------------------
# Anchors: how the categories this system already produces map onto the tree.
#
# Three vocabularies are in circulation — the 18 dual-taxonomy purposes, the 15
# canonical categories, and the older 43-term treasury list — and every one of
# them can arrive here on an existing row or from the rule engine. Mapping all
# three in one place is what lets the hierarchy be added without re-labelling
# the database first.
#
# A mapping is only listed where it is honest. `NEFT Transfer` names a rail and
# says nothing about purpose, so it is deliberately absent: an unmapped anchor
# means "derive from the narration instead", which is the correct behaviour.
# ---------------------------------------------------------------------------
ANCHORS: Dict[str, Tuple[str, ...]] = {
    # ---- dual_taxonomy purposes -------------------------------------------
    "sales income":            (H.INCOME, "Business Revenue"),
    "other income":            (H.INCOME, "Other Income"),
    "cost of goods":           (H.BUSINESS, "Inventory"),
    "salary & wages":          (H.BUSINESS, "Salaries & Wages"),
    "rent & premises":         (H.HOUSING, "Rent"),
    "utilities & bills":       (H.BILLS_UTILITIES,),
    "bank fees":               (H.FINANCIAL, "Bank Charges"),
    "interest & finance cost": (H.FINANCIAL, "Interest"),
    "taxes & statutory":       (H.TAXES_GOVERNMENT,),
    "professional fees":       (H.BUSINESS, "Professional Services"),
    "owner funding":           (H.TRANSFERS, "Own Account Transfer"),
    "loans & borrowing":       (H.LOANS_CREDIT,),
    "internal movement":       (H.TRANSFERS, "Own Account Transfer"),
    "food & dining":           (H.FOOD_DINING,),
    "travel":                  (H.TRAVEL,),
    "transportation":          (H.TRANSPORTATION,),
    "healthcare":              (H.HEALTHCARE,),

    # ---- taxonomy.CATEGORIES ----------------------------------------------
    "groceries":               (H.FOOD_DINING, "Groceries"),
    "shopping":                (H.SHOPPING,),
    "entertainment":           (H.ENTERTAINMENT,),
    "education":               (H.EDUCATION,),
    "rent & housing":          (H.HOUSING, "Rent"),
    "bank charges":            (H.FINANCIAL, "Bank Charges"),
    "salary / income":         (H.INCOME, "Salary"),
    "transfers":               (H.TRANSFERS,),
    "investments":             (H.INVESTMENTS,),

    # ---- older treasury vocabulary ----------------------------------------
    "merchant settlement":              (H.INCOME, "Business Revenue"),
    "customer payment / neft transfer": (H.INCOME, "Business Revenue"),
    "vendor payment":                   (H.BUSINESS, "Supplier Payment"),
    "salary payment":                   (H.BUSINESS, "Salaries & Wages"),
    "gst/tax payment":                  (H.TAXES_GOVERNMENT, "GST"),
    "government fee":                   (H.TAXES_GOVERNMENT, "Government Fee"),
    "internal fund transfer":           (H.TRANSFERS, "Own Account Transfer"),
    "cash deposit":                     (H.CASH, "Cash Deposit"),
    "atm withdrawal":                   (H.CASH, "ATM Withdrawal"),
    "loan disbursement":                (H.LOANS_CREDIT, "Loan Disbursement"),
    "loan repayment / emi":             (H.LOANS_CREDIT, "Loan Repayment"),
    "insurance premium":                (H.INSURANCE, "Other Insurance"),
    "interest credit":                  (H.INCOME, "Interest Income"),
    "interest debit":                   (H.FINANCIAL, "Interest"),
    "utility payment":                  (H.BILLS_UTILITIES,),
    "utilities":                        (H.BILLS_UTILITIES,),
    "fuel":                             (H.TRANSPORTATION, "Fuel"),
    "pos/card purchase":                (H.SHOPPING, "Other Shopping"),
    "dividend":                         (H.INCOME, "Dividend"),
    "refund":                           (H.REFUNDS_REVERSALS, "Purchase Refund"),
    "reversal":                         (H.REFUNDS_REVERSALS, "Other Reversal"),
    "software & cloud":                 (H.BUSINESS, "Professional Services", "IT Services"),
    "office expenses":                  (H.BUSINESS, "Office Expenses"),
    "rent payment":                     (H.HOUSING, "Rent"),

    # ---- the tree's own level-1 names, so a re-run is idempotent ----------
    **{name.lower(): (name,) for name in H.ROOTS},
}

# Anchors that name a rail, a wrapper or nothing at all. Listed so the intent is
# explicit — this is a decision not to map, not an oversight.
UNMAPPABLE_ANCHORS = frozenset({
    "neft transfer", "imps transfer", "upi transfer", "rtgs transfer",
    "uncategorized", "other", "others", "misc", "miscellaneous",
})


def anchor_path(category_name: Optional[str]) -> Optional[Tuple[str, ...]]:
    """Where an existing category name lands in the tree, or None if it cannot say."""
    if not category_name:
        return None
    key = str(category_name).strip().lower()
    if not key or key in UNMAPPABLE_ANCHORS:
        return None
    path = ANCHORS.get(key)
    if path and H.is_valid_path(path):
        return path
    return None


# ---------------------------------------------------------------------------
# Counterparty naming
# ---------------------------------------------------------------------------

# Suffixes and words that mark a name as an organisation rather than a person.
# Used only to decide between a business vendor and a person-to-person transfer,
# both of which are honest answers — so a miss here costs a slightly wrong
# level 2, not a fabricated level 3.
_ORG_MARKERS = re.compile(
    r"\b(PVT|PRIVATE|LTD|LIMITED|LLP|INC|CORP|CORPORATION|CO|COMPANY|ENTERPRISES?|"
    r"INDUSTR(?:Y|IES)|TRADERS?|TRADING|AGENC(?:Y|IES)|SERVICES?|SOLUTIONS?|"
    r"TECHNOLOG(?:Y|IES)|SYSTEMS?|ASSOCIATES?|CONSULTANC(?:Y|IES)|"
    r"STORES?|MART|RETAIL|DISTRIBUTORS?|SUPPL(?:Y|IERS?)|EXPORTS?|IMPORTS?|"
    r"MANUFACTUR|ORGANICS?|FOODS?|PHARMA|LABS?|LABORATOR(?:Y|IES)|HOSPITALS?|"
    r"CLINIC|SCHOOL|COLLEGE|UNIVERSITY|INSTITUTE|TRUST|FOUNDATION|SOCIETY)\b",
    re.I,
)


def looks_like_organisation(name: Optional[str]) -> bool:
    if not name:
        return False
    return bool(_ORG_MARKERS.search(str(name)))


def _titlecase(name: str) -> str:
    """Readable form of a SHOUTED counterparty name, leaving acronyms alone."""
    parts = []
    for word in str(name).split():
        if len(word) <= 3 and word.isupper():
            parts.append(word)          # PVT, LTD, ABC
        else:
            parts.append(word.capitalize())
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

# Below this, the row goes to the review queue. Matches the existing
# provisional-answer threshold: a category is still written, because a category
# the user can correct beats a void they cannot see.
REVIEW_THRESHOLD = 0.60


@dataclass
class DeepClassification:
    """One transaction's place in the tree, and how sure we are of it."""

    path: Tuple[str, ...]
    confidence: float
    flow_type: str
    transaction_method: str

    merchant: Optional[str] = None
    counterparty: Optional[str] = None
    source: str = "none"
    evidence: List[str] = field(default_factory=list)
    flow_confidence: float = 0.0
    method_confidence: float = 0.0

    # ---- the path, spelled out -------------------------------------------
    @property
    def category(self) -> str:
        return self.path[0] if self.path else H.RESIDUAL_TRANSFER[0]

    @property
    def subcategory(self) -> Optional[str]:
        return self.path[1] if len(self.path) > 1 else None

    @property
    def specific_category(self) -> Optional[str]:
        return self.path[2] if len(self.path) > 2 else None

    @property
    def detail(self) -> Optional[str]:
        return self.path[3] if len(self.path) > 3 else None

    @property
    def depth(self) -> int:
        return len(self.path)

    @property
    def display_path(self) -> str:
        return H.format_path(self.path)

    @property
    def slug(self) -> str:
        return H.path_slug(self.path)

    @property
    def needs_review(self) -> bool:
        return self.confidence < REVIEW_THRESHOLD

    def to_dict(self) -> Dict[str, Any]:
        """The shape the API and the UI consume.

        `specific_category` is null rather than repeated when the path stops at
        two levels. A caller reading this can tell the difference between "there
        is no third level" and "the third level is the same as the second",
        which a padded path would destroy.
        """
        return {
            "flow_type": self.flow_type,
            "transaction_method": self.transaction_method,
            "category": self.category,
            "subcategory": self.subcategory,
            "specific_category": self.specific_category,
            "detail": self.detail,
            "category_path": self.display_path,
            "category_slug": self.slug,
            "depth": self.depth,
            "merchant": self.merchant,
            "counterparty": self.counterparty,
            "confidence": round(self.confidence, 4),
            "needs_review": self.needs_review,
            "source": self.source,
            "evidence": list(self.evidence),
        }


@dataclass
class _Candidate:
    path: Tuple[str, ...]
    confidence: float
    source: str
    evidence: str
    merchant: Optional[str] = None
    priority: int = 0   # higher wins ties


# A plain-channel extraction is the whole narration with the digits stripped,
# not a parsed payee. That fallback is right for GROUPING — it collapses 120
# rows of one unknown format into a handful of buckets — and wrong for naming,
# because "Some Unreadable String 998877" is not a company. So a plain result
# has to earn the right to be called a party.
_LONG_DIGIT_RUN = re.compile(r"\d{4,}")


def _is_nameable_party(display: str, channel: str) -> bool:
    if channel != "plain":
        # A rail-parsed name came out of the slot the bank puts the payee in.
        return True
    if _LONG_DIGIT_RUN.search(display):
        return False
    if len(display.split()) > 5:
        return False
    return looks_like_organisation(display)


def _extract_counterparty(narration: str) -> Tuple[Optional[str], Optional[str]]:
    """(key, display) for the party in this narration, or (None, None).

    Imported lazily. `counterparty` is a large module and this one is used by
    tests that have no reason to pull it in.
    """
    try:
        from app.categorization.counterparty import extract
    except Exception:                                    # pragma: no cover
        return None, None
    try:
        cp = extract(narration or "")
    except Exception:                                    # pragma: no cover
        return None, None
    if cp is None or not cp.is_usable:
        return None, None
    if not _is_nameable_party(cp.display, cp.channel):
        return None, None
    return cp.key, cp.display


def _deepen_with_name(path: Tuple[str, ...], name: Optional[str]) -> Tuple[str, ...]:
    """Append a party name as the next level, but only where that is meaningful.

    Two guards, and both of them exist because of a specific bad outcome:

    - the node must permit dynamic children. Under `Own Account Transfer` a name
      is the other account, not a category, and minting one node per account
      number produces a drill-down with one transaction in every leaf.
    - the name must not already be in the path. `Food & Dining > Food Delivery >
      Zomato > Zomato` is what happens without this.
    """
    if not name:
        return path
    node = H.node_for_path(path)
    if node is None or not node.allows_dynamic_children:
        return path
    clean = str(name).strip()
    if not clean or len(clean) < 3:
        return path
    if any(clean.lower() == part.lower() for part in path):
        return path
    candidate = path + (clean,)
    return candidate if H.is_valid_path(candidate) else path


def classify_deep(
    narration: str,
    *,
    direction: Optional[str] = None,
    amount: Optional[float] = None,
    declared_method: Optional[str] = None,
    counterparty: Optional[str] = None,
    counterparty_display: Optional[str] = None,
    upstream_category: Optional[str] = None,
    upstream_confidence: float = 0.0,
    upstream_requires_review: bool = False,
    memory_category: Optional[str] = None,
    memory_confirmations: int = 0,
    counterparty_is_self: Optional[bool] = None,
    account_type: Optional[str] = None,
) -> DeepClassification:
    """Place one transaction in the tree.

    Every argument beyond `narration` is optional, because bank statements do
    not agree on which fields exist. A row with only a narration and a direction
    still gets an answer; it just gets a shallower and less confident one, which
    is the correct response to having less to go on.
    """
    text = str(narration or "")
    method = F.detect_method(text, declared_method)

    cp_key, cp_display = (counterparty, counterparty_display)
    if cp_key is None:
        cp_key, cp_display = _extract_counterparty(text)
    if cp_display is None and cp_key:
        cp_display = _titlecase(cp_key)

    # A payment rail is not a party. Carrying "PHONEPE" forward as the
    # counterparty would file every settlement under one merchant and imply a
    # category the aggregator cannot support.
    if cp_key and M.is_pass_through(cp_key):
        cp_key, cp_display = None, None

    candidates: List[_Candidate] = []

    # ---- 1. what the user already decided --------------------------------
    if memory_category:
        mem_path = anchor_path(memory_category) or H.trim_to_valid(
            H.parse_display_path(memory_category))
        if mem_path:
            # Repeated confirmations are worth something, but a single human
            # decision is already better evidence than anything below.
            conf = min(0.99, 0.90 + 0.02 * max(0, memory_confirmations - 1))
            candidates.append(_Candidate(
                mem_path, conf, "memory",
                f"you previously categorised {cp_display or 'this counterparty'} "
                f"as {H.format_path(mem_path)}",
                priority=100,
            ))

    # ---- 2. a concept named outright -------------------------------------
    hit = M.match_concept(text, direction)
    if hit:
        ev, matched = hit
        candidates.append(_Candidate(
            ev.path, ev.confidence, "concept",
            f"the narration contains {matched!r}", merchant=ev.merchant, priority=80,
        ))

    # ---- 3. a single-purpose merchant ------------------------------------
    hit = M.match_merchant(text, direction)
    if hit:
        ev, matched = hit
        merchant_name = ev.merchant or _titlecase(matched)
        path = ev.path
        # Only name the merchant as a level if the node invites one; otherwise
        # it stays in the merchant field where it belongs.
        path = _deepen_with_name(path, merchant_name) if ev.merchant else path
        candidates.append(_Candidate(
            path, ev.confidence, "merchant",
            f"{matched!r} identifies the merchant", merchant=merchant_name, priority=70,
        ))

    # ---- 4. a marketplace, plus a product word if there is one -----------
    hit = M.match_marketplace(text, direction)
    if hit:
        ev, matched = hit
        merchant_name = ev.merchant or _titlecase(matched)
        product = M.match_product_hint(text)
        if product:
            path, product_word = product
            candidates.append(_Candidate(
                path, min(0.90, ev.confidence), "marketplace+product",
                f"{matched!r} with {product_word!r} in the same narration",
                merchant=merchant_name, priority=68,
            ))
        else:
            # Stops at the merchant. This is the Amazon rule: knowing the shop
            # is not knowing the purchase.
            candidates.append(_Candidate(
                ev.path, ev.confidence, "marketplace",
                f"{matched!r} is a marketplace; the narration does not say what was bought",
                merchant=merchant_name, priority=60,
            ))

    # ---- 5. the existing classifier's answer -----------------------------
    up = anchor_path(upstream_category)
    if up:
        # A provisional upstream answer stays provisional here. Deepening a
        # 0.62 guess into a three-level path would launder a weak signal into a
        # specific-looking one.
        conf = upstream_confidence if upstream_confidence > 0 else 0.55
        if upstream_requires_review:
            conf = min(conf, 0.62)
        path = up
        if not upstream_requires_review:
            path = _deepen_with_name(up, cp_display)
        candidates.append(_Candidate(
            path, conf, "upstream",
            f"the existing classifier said {upstream_category}",
            priority=50,
        ))

    # ---- 5.5 the counterparty's name states its trade --------------------
    #
    # `KUMAR FISH` is a fish supplier, `MARUTHI MOTORS` a garage. Weaker than a
    # concept named in the narration and weaker than a merchant we recognise,
    # so it sits below both — but far stronger than filing the row as an
    # unknown vendor and asking a person to type what the name already says.
    trade_hit = T.match(text, direction=direction, account_type=account_type)
    if trade_hit:
        candidates.append(_Candidate(
            trade_hit.path, 0.72, "trade_name",
            trade_hit.explanation, priority=40,
        ))

    # ---- 6. a company we cannot read any further -------------------------
    if not candidates and cp_display:
        if looks_like_organisation(cp_display):
            path = _deepen_with_name(H.VENDOR_UNKNOWN, cp_display)
            candidates.append(_Candidate(
                path, 0.45, "vendor_unknown",
                f"{cp_display} looks like a business, but nothing in the narration "
                f"says what the payment was for",
                priority=20,
            ))
        else:
            sub = "Person to Person"
            candidates.append(_Candidate(
                (H.TRANSFERS, sub), 0.40, "person",
                f"{cp_display} looks like an individual rather than a business",
                priority=15,
            ))

    # ---- 7. nothing names a purpose --------------------------------------
    #
    # There is no `Other / Uncategorized` to fall into any more, and taking it
    # away did not require inventing anything: a row this system cannot read
    # still states its rail and its direction, and those are facts. `SELF 4471`
    # is a cash withdrawal. `ACH D- 88213` out is a transfer to a party. Saying
    # so is strictly more true than saying "Uncategorized", and unlike
    # "Uncategorized" it can be totalled, reconciled and drilled into.
    #
    # The part that is genuinely unknown — what the money was FOR — is reported
    # by the confidence below and by `needs_review`. That is a status on the
    # row. It was never a category, and the tree is better for not pretending
    # it was one.
    if not candidates:
        residual_flow = F.detect_flow(direction, text,
                                      counterparty_is_self=counterparty_is_self)
        residual = H.residual_path(method.method, direction, residual_flow.flow_type)
        candidates.append(_Candidate(
            residual, 0.30, "residual",
            f"nothing in this transaction says what it was for; filed by what "
            f"the row does state — a {method.method} {'credit' if residual_flow.flow_type == 'INFLOW' else 'debit'}",
            priority=0,
        ))

    best = max(candidates, key=lambda c: (round(c.confidence, 3), c.priority, len(c.path)))

    path = H.trim_to_valid(best.path) or H.residual_path(method.method, direction)

    # The employer under Salary, the lender under Loan Repayment, the supplier
    # under Inventory. Applied after the winner is chosen rather than inside
    # each branch, so one guard covers every source. `_deepen_with_name` is a
    # no-op unless the node invites a name, so this cannot deepen a path that
    # should have stopped.
    #
    # Not applied to a weak answer: naming a party under a category we are only
    # 45% sure of makes a vague answer look specific.
    if best.confidence >= REVIEW_THRESHOLD and best.source != "vendor_unknown":
        path = _deepen_with_name(path, cp_display)

    # Account type breaks one specific tie and no others: a large credit that
    # upstream called generic income means different things on a salary account
    # and a current account. It never introduces a level, only chooses between
    # two the evidence already reached.
    path = _apply_account_type(path, account_type, best.source)

    flow = F.detect_flow(
        direction, text,
        counterparty_is_self=counterparty_is_self,
        category_path=path,
    )

    evidence = [best.evidence]
    if method.evidence:
        evidence.append(f"payment method {method.method} from {method.evidence!r}")
    if flow.evidence:
        evidence.append(f"flow {flow.flow_type}: {flow.evidence}")

    # The reported confidence is about the CATEGORY. Method and flow carry their
    # own numbers, because being certain a payment was UPI says nothing about
    # being certain what it bought, and averaging the two would hide both.
    confidence = best.confidence

    return DeepClassification(
        path=path,
        confidence=confidence,
        flow_type=flow.flow_type,
        transaction_method=method.method,
        merchant=best.merchant,
        counterparty=cp_display,
        source=best.source,
        evidence=evidence,
        flow_confidence=flow.confidence,
        method_confidence=method.confidence,
    )


def _apply_account_type(path: Tuple[str, ...], account_type: Optional[str],
                        source: str) -> Tuple[str, ...]:
    """Choose between equally-supported readings using the kind of account.

    The same 2 lakh NEFT credit is salary on a salary account and revenue on a
    current account, and no amount of narration parsing settles that. Applied
    only to a generic income answer that came from the upstream classifier — a
    concept match saying SALARY outright is not second-guessed.
    """
    if not account_type or source not in {"upstream", "residual"}:
        return path
    kind = str(account_type).strip().lower()
    if path[:2] == (H.INCOME, "Other Income"):
        if kind in {"current", "business", "merchant"}:
            return (H.INCOME, "Business Revenue")
        if kind in {"salary"}:
            return (H.INCOME, "Salary")
    return path


def classify_batch(rows: Sequence[Dict[str, Any]]) -> List[DeepClassification]:
    return [
        classify_deep(
            r.get("narration") or r.get("description") or "",
            direction=r.get("direction") or r.get("debit_credit"),
            amount=r.get("amount"),
            declared_method=r.get("payment_method"),
            counterparty=r.get("counterparty"),
            upstream_category=r.get("category"),
            upstream_confidence=float(r.get("confidence") or 0.0),
            upstream_requires_review=bool(r.get("requires_review")),
            account_type=r.get("account_type"),
        )
        for r in rows
    ]


__all__ = [
    "DeepClassification", "classify_deep", "classify_batch",
    "anchor_path", "ANCHORS", "REVIEW_THRESHOLD", "looks_like_organisation",
]
