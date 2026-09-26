"""The category tree: a general-purpose taxonomy of variable depth.

This is the shape of the thing, and the shape is the point.

A flat taxonomy answers "what kind of spending was this" and stops. That is
enough for a chart and not enough for a question. `Food & Dining` covers a
supermarket run and a Zomato order and a coffee, and once a user asks *which*,
a flat label has nothing left to say.

So the taxonomy here is a tree. But it is emphatically **not** a fixed-depth
tree, and every part of this module exists to keep it from becoming one.

    Food & Dining > Restaurants > Fast Food > McDonald's      four levels
    Transportation > Fuel > Petrol                            three
    Income > Salary > ABC Technologies                        three
    Transfers > Own Account Transfer                           two
    Other / Uncategorized                                      one

All five are complete answers. `Transfers > Own Account Transfer` is not a
three-level answer that lost a level — there is nothing below it worth naming,
and inventing `Transfers > Own Account Transfer > Transfer` to square the
columns would add a word and no information.

The rule that follows from that, and the one every caller must respect:

    A PATH STOPS WHERE THE EVIDENCE STOPS.

Depth is a property of what the narration actually said, not of the category it
landed in. Two Amazon transactions can legitimately end at different depths:
`AMAZON` alone gives `Shopping > Online Shopping > Amazon`, while
`AMAZON.IN MOBILE PHONE` earns `Shopping > Electronics > Mobile`. The tree
below describes what is *available*; the classifier decides what is *supported*.

WHAT THIS MODULE IS NOT
-----------------------
It is not a merchant list. `Zomato` appears in the tree as a permitted node,
but the fact that a narration mentions Zomato is a matter for the classifier,
and merchant identity is stored separately from category (a merchant is who you
paid, a category is what for — Amazon is not a category). Leaf nodes named after
companies are here only where the company *is* the meaningful subdivision, and
the tree stays open: `allows_dynamic_children` marks the nodes under which the
classifier may mint a node from the narration (a supplier name, an employer, a
lender) rather than being restricted to what is hardcoded.

It is not tied to one bank, one country or one kind of account. A statement from
a student's savings account and one from a manufacturer's current account are
classified against the same tree; which branches get used differs, and that is
the only difference.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Level 1 — the top of the tree.
#
# These are broad enough that every transaction belongs under exactly one, and
# few enough that a person can hold the list in their head. Adding a 24th should
# feel hard; that is deliberate. A taxonomy grows at the leaves, not the root.
# ---------------------------------------------------------------------------
INCOME              = "Income"
FOOD_DINING         = "Food & Dining"
SHOPPING            = "Shopping"
HOUSING             = "Housing"
TRANSPORTATION      = "Transportation"
BILLS_UTILITIES     = "Bills & Utilities"
HEALTHCARE          = "Healthcare"
EDUCATION           = "Education"
ENTERTAINMENT       = "Entertainment"
TRAVEL              = "Travel"
FINANCIAL           = "Financial"
TRANSFERS           = "Transfers"
INVESTMENTS         = "Investments"
LOANS_CREDIT        = "Loans & Credit"
INSURANCE           = "Insurance"
TAXES_GOVERNMENT    = "Taxes & Government"
BUSINESS            = "Business & Professional"
PERSONAL_FAMILY     = "Personal & Family"
CASH                = "Cash"
DONATIONS           = "Donations & Charity"
FEES_CHARGES        = "Fees & Charges"
REFUNDS_REVERSALS   = "Refunds & Reversals"


# ---------------------------------------------------------------------------
# The tree itself.
#
# Written as nested (name, children) pairs. A node with no children is a leaf
# *of the fixed tree* — which is not the same as the end of a valid path, since
# nodes marked below may grow named children at classification time.
#
# Read the omissions as deliberate. `Transfers > Own Account Transfer` has no
# children because there is nothing underneath it that a bank statement can
# tell you. `Housing > Rent` has none because "rent" is the whole answer.
# ---------------------------------------------------------------------------

# A node spec is either a bare string (leaf) or (name, [children...]).
_NodeSpec = object  # str | tuple[str, list]

TREE: List[Tuple[str, List[_NodeSpec]]] = [
    (INCOME, [
        # Level 3 under these is the payer — employer, client, tenant. Which is
        # a name from the narration, not something that can be listed here.
        "Salary",
        "Business Revenue",
        "Freelance Income",
        "Interest Income",
        "Rental Income",
        "Dividend",
        "Pension",
        "Government Benefit",
        "Refund",
        "Other Income",
    ]),

    (FOOD_DINING, [
        ("Restaurants", ["Fast Food", "Casual Dining", "Fine Dining", "QSR"]),
        ("Food Delivery", ["Online Delivery", "Cloud Kitchen"]),
        ("Cafes & Beverages", ["Cafe", "Coffee", "Bakery", "Desserts"]),
        ("Groceries", ["Supermarket", "Online Grocery", "Local Grocery"]),
        "Other Food",
    ]),

    (SHOPPING, [
        ("Clothing", ["Men's", "Women's", "Kids"]),
        ("Electronics", ["Mobile", "Laptop", "Accessories", "Appliances"]),
        "Personal Care",
        "Home & Furniture",
        "Books",
        "Jewelry",
        # The honest home for a marketplace. Amazon sells laptops and lentils;
        # the merchant name alone cannot tell you which, so it stops here.
        "Online Shopping",
        "Other Shopping",
    ]),

    (HOUSING, [
        "Rent",
        "Maintenance",
        "Property Purchase",
        "Home Improvement",
        "Society & Association Fees",
        "Other Housing",
    ]),

    (TRANSPORTATION, [
        ("Fuel", ["Petrol", "Diesel", "CNG", "EV Charging"]),
        ("Public Transport", ["Bus", "Metro", "Train", "Tram"]),
        ("Ride Hailing", ["Taxi", "Cab", "Bike Taxi"]),
        ("Air Travel", ["Domestic", "International"]),
        ("Vehicle Expenses", ["Parking", "Toll", "Servicing", "Repairs"]),
        "Other Transportation",
    ]),

    (BILLS_UTILITIES, [
        "Electricity",
        "Water",
        "Gas",
        "Internet",
        "Mobile",
        "DTH",
        "Other Utilities",
    ]),

    (HEALTHCARE, [
        "Pharmacy",
        "Hospital",
        "Doctor",
        "Diagnostics",
        "Dental",
        "Optical",
        "Medical Insurance",
        "Other Healthcare",
    ]),

    (EDUCATION, [
        "Tuition & Fees",
        "Courses & Training",
        "Books & Materials",
        "Exam Fees",
        "Student Loan",
        "Other Education",
    ]),

    (ENTERTAINMENT, [
        "OTT / Streaming",
        "Movies",
        "Gaming",
        "Music",
        "Events",
        "Sports",
        "Other Entertainment",
    ]),

    (TRAVEL, [
        "Flights",
        "Hotels",
        "Trains",
        "Buses",
        "Travel Agencies",
        "Car Rental",
        "Other Travel",
    ]),

    (FINANCIAL, [
        "Bank Charges",
        "Processing Fees",
        "Interest",
        "Service Charges",
        "ATM Charges",
        "Payment Gateway Charges",
        "Other Financial",
    ]),

    (TRANSFERS, [
        # Nothing below these. A transfer between your own accounts is fully
        # described by saying so; the destination account is an account, not a
        # category, and it is carried on the transaction's counterparty field.
        "Own Account Transfer",
        "Person to Person",
        "Business Transfer",
        "External Transfer",
    ]),

    (INVESTMENTS, [
        ("Mutual Funds", ["SIP", "Lump Sum"]),
        ("Stocks", ["Purchase", "Sale"]),
        "Bonds",
        "Fixed Deposit",
        "Recurring Deposit",
        "PPF",
        "NPS",
        "Other Investments",
    ]),

    (LOANS_CREDIT, [
        # Level 3 under these is the lender, and level 4 the loan account —
        # both come from the narration's reference, so neither is listed.
        "Loan Disbursement",
        "Loan Repayment",
        "EMI",
        "Interest",
        "Processing Fee",
        "Late Fee",
        ("Credit Card", ["Payment", "Interest", "Annual Fee", "Late Fee"]),
    ]),

    (INSURANCE, [
        "Life Insurance",
        "Health Insurance",
        "Motor Insurance",
        "Property Insurance",
        "Other Insurance",
    ]),

    (TAXES_GOVERNMENT, [
        "Income Tax",
        "GST",
        "TDS",
        "Property Tax",
        "Government Fee",
        "Fines / Penalties",
        "Other Government Payment",
    ]),

    (BUSINESS, [
        "Supplier Payment",
        "Inventory",
        "Raw Materials",
        ("Professional Services", ["Legal", "Accounting", "Consulting", "IT Services"]),
        "Marketing & Advertising",
        "Office Expenses",
        "Salaries & Wages",
        "Business Rent",
        "Business Travel",
        "Business Revenue",
        # Where a payment to a company goes when the narration names the company
        # and nothing else. See the note on VENDOR_UNKNOWN below — this node is
        # load-bearing, not a dumping ground.
        "Vendor / Business Transaction",
        "Other Business",
    ]),

    (PERSONAL_FAMILY, [
        "Family Transfer",
        "Gifts",
        "Allowance",
        "Personal Services",
        "Other Personal",
    ]),

    (CASH, [
        "ATM Withdrawal",
        "Cash Deposit",
        "Other Cash Transaction",
    ]),

    (DONATIONS, [
        "Charity",
        "Religious",
        "Crowdfunding",
        "Other Donations",
    ]),

    (FEES_CHARGES, [
        "Late Payment Fee",
        "Penalty",
        "Subscription Fee",
        "Convenience Fee",
        "Other Fees",
    ]),

    (REFUNDS_REVERSALS, [
        "Purchase Refund",
        "Failed Transaction Reversal",
        "Chargeback",
        "Other Reversal",
    ]),

]


# ---------------------------------------------------------------------------
# Nodes that may grow children the tree does not list.
#
# `Income > Salary > ABC Technologies` is a good path, and `ABC Technologies`
# can obviously never appear in source code. The same is true of a lender under
# `Loan Repayment`, a supplier under `Supplier Payment`, and a restaurant name
# under `Restaurants`.
#
# The permission is per-node rather than global, because the places where a name
# is a genuine subdivision are specific. Under `Transfers > Own Account Transfer`
# a counterparty name is not a deeper category — it is the other account, and it
# already has a field. Minting a node there would produce one category per
# account number and a drill-down nobody can use.
# ---------------------------------------------------------------------------
DYNAMIC_CHILD_PATHS: Tuple[Tuple[str, ...], ...] = (
    (INCOME, "Salary"),
    (INCOME, "Business Revenue"),
    (INCOME, "Freelance Income"),
    (INCOME, "Rental Income"),
    (INCOME, "Pension"),

    (FOOD_DINING, "Restaurants"),
    (FOOD_DINING, "Restaurants", "Fast Food"),
    (FOOD_DINING, "Restaurants", "Casual Dining"),
    (FOOD_DINING, "Restaurants", "Fine Dining"),
    (FOOD_DINING, "Restaurants", "QSR"),
    (FOOD_DINING, "Food Delivery"),
    (FOOD_DINING, "Food Delivery", "Online Delivery"),
    (FOOD_DINING, "Cafes & Beverages"),
    (FOOD_DINING, "Groceries", "Supermarket"),
    (FOOD_DINING, "Groceries", "Online Grocery"),

    (SHOPPING, "Online Shopping"),
    (SHOPPING, "Clothing"),
    (SHOPPING, "Electronics"),
    (SHOPPING, "Jewelry"),

    (HEALTHCARE, "Pharmacy"),
    (HEALTHCARE, "Hospital"),
    (HEALTHCARE, "Diagnostics"),

    (BILLS_UTILITIES, "Electricity"),
    (BILLS_UTILITIES, "Internet"),
    (BILLS_UTILITIES, "Mobile"),
    (BILLS_UTILITIES, "DTH"),
    (BILLS_UTILITIES, "Gas"),

    (ENTERTAINMENT, "OTT / Streaming"),
    (ENTERTAINMENT, "Gaming"),

    (TRAVEL, "Flights"),
    (TRAVEL, "Hotels"),
    (TRAVEL, "Travel Agencies"),

    (TRANSPORTATION, "Ride Hailing"),
    (TRANSPORTATION, "Ride Hailing", "Cab"),
    (TRANSPORTATION, "Fuel"),

    (LOANS_CREDIT, "Loan Disbursement"),
    (LOANS_CREDIT, "Loan Repayment"),
    (LOANS_CREDIT, "EMI"),
    (LOANS_CREDIT, "Credit Card"),
    (LOANS_CREDIT, "Credit Card", "Payment"),

    (INSURANCE, "Life Insurance"),
    (INSURANCE, "Health Insurance"),
    (INSURANCE, "Motor Insurance"),

    (INVESTMENTS, "Mutual Funds"),
    (INVESTMENTS, "Stocks"),

    (BUSINESS, "Supplier Payment"),
    (BUSINESS, "Inventory"),
    (BUSINESS, "Raw Materials"),
    (BUSINESS, "Office Expenses"),
    (BUSINESS, "Business Rent"),
    (BUSINESS, "Professional Services"),
    (BUSINESS, "Professional Services", "Legal"),
    (BUSINESS, "Professional Services", "Accounting"),
    (BUSINESS, "Professional Services", "Consulting"),
    (BUSINESS, "Professional Services", "IT Services"),
    (BUSINESS, "Marketing & Advertising"),
    (BUSINESS, "Salaries & Wages"),
    (BUSINESS, "Vendor / Business Transaction"),

    (DONATIONS, "Charity"),
    (DONATIONS, "Religious"),

    (EDUCATION, "Tuition & Fees"),
    (EDUCATION, "Courses & Training"),
)


# The landing place for "money went to a company and the narration says nothing
# else". Naming it explicitly matters: the alternative is a classifier that
# guesses `Raw Materials` from a supplier's name, which is a wrong answer
# wearing the clothes of a right one. This path plus a low confidence is an
# honest report of what was known.
VENDOR_UNKNOWN: Tuple[str, ...] = (BUSINESS, "Vendor / Business Transaction")

# ---------------------------------------------------------------------------
# Where a row goes when nothing in it names a purpose.
#
# There used to be an `Other / Uncategorized` root here and it was the wrong
# shape of answer. "Uncategorized" is not a category — it is the absence of one
# — and putting it in the taxonomy meant a quarter of a statement drilled down
# into a node that could never be opened, could never be reconciled, and told
# the reader nothing they did not already know.
#
# It is gone. Every transaction now lands somewhere that is a TRUE STATEMENT
# ABOUT THE ROW, derived only from facts the statement itself carries:
#
#     the rail        ATM, cash, card, UPI, NEFT, cheque...
#     the direction   money in or money out
#     the other side  a company, a person, or nobody nameable
#
# `SELF 1234` is a cash withdrawal. That is not a guess about what the cash was
# spent on — it is what the row says, and it is exactly as much as we know. A
# card swipe with an unreadable merchant is money to a business. An IMPS out to
# a name we cannot resolve is a transfer to a party. All three are facts.
#
# What is NOT known — the purpose — is carried where it belongs: a low
# confidence and `needs_review`, which is a STATUS on the row, not a place in
# the tree. A person can still find and fix every one of them from the review
# queue; they just are not pretending to be a category any more.
# ---------------------------------------------------------------------------

RESIDUAL_ATM_OUT: Tuple[str, ...]   = (CASH, "ATM Withdrawal")
RESIDUAL_CASH_IN: Tuple[str, ...]   = (CASH, "Cash Deposit")
RESIDUAL_CASH_OUT: Tuple[str, ...]  = (CASH, "Other Cash Transaction")
RESIDUAL_VENDOR: Tuple[str, ...]    = VENDOR_UNKNOWN
RESIDUAL_PERSON: Tuple[str, ...]    = (TRANSFERS, "Person to Person")
RESIDUAL_TRANSFER: Tuple[str, ...]  = (TRANSFERS, "External Transfer")
RESIDUAL_REVERSAL: Tuple[str, ...]  = (REFUNDS_REVERSALS, "Other Reversal")

# Rails that ARE cash. Held here rather than imported from `flow` so this
# module stays dependency-free and importable from anywhere.
_CASH_RAILS = {"ATM", "Cash"}
_CARD_RAILS = {"Card"}


def residual_path(
    method: Optional[str] = None,
    direction: Optional[str] = None,
    flow_type: Optional[str] = None,
) -> Tuple[str, ...]:
    """The most specific TRUE thing that can be said about an unreadable row.

    Never invents a purpose. Each branch below is a restatement of something
    printed on the statement, which is why every one of them is safe to show
    without a person checking it first.

    Deliberately knows nothing about counterparties. When a name CAN be read,
    `deep.classify_deep` has already split business from individual by then and
    answers before it ever gets here; this is the floor beneath that.
    """
    m = (method or "").strip()
    d = (direction or "").strip().lower()
    ft = (flow_type or "").strip().upper()
    incoming = d in {"credit", "cr", "in"} or ft == "INFLOW"

    if ft == "REVERSAL":
        return RESIDUAL_REVERSAL

    if m == "ATM":
        # An ATM credit is a deposit at the machine; the debit is the classic
        # withdrawal. Both are cash movements and neither says what for.
        return RESIDUAL_CASH_IN if incoming else RESIDUAL_ATM_OUT

    if m in _CASH_RAILS:
        return RESIDUAL_CASH_IN if incoming else RESIDUAL_CASH_OUT

    if m in _CARD_RAILS and not incoming:
        # A card was presented to a merchant. That the merchant is unreadable
        # does not make the fact that it was a merchant any less true.
        return RESIDUAL_VENDOR

    # Money crossed the account boundary and nothing else is legible. Saying
    # "a transfer happened" is the honest floor; saying "Uncategorized" was not
    # more honest, only less useful.
    return RESIDUAL_TRANSFER


# ---------------------------------------------------------------------------
# Compiled form
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Node:
    """One node in the tree, with its full path from the root."""
    name: str
    path: Tuple[str, ...]
    level: int
    slug: str
    parent_slug: Optional[str]
    children: Tuple[str, ...] = field(default_factory=tuple)  # child slugs
    allows_dynamic_children: bool = False

    @property
    def display_path(self) -> str:
        return " > ".join(self.path)

    @property
    def is_leaf(self) -> bool:
        """No children in the fixed tree AND none may be minted."""
        return not self.children and not self.allows_dynamic_children


def slugify(value: str) -> str:
    """Stable, URL-safe key for a node name.

    Used as the identity of a node in the database and in API paths, so it must
    not change when the display name is re-cased or re-punctuated. Accents are
    folded rather than dropped so a merchant like "Café Coffee" does not collide
    with "Cafe Coffee".
    """
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "node"


def path_slug(path: Sequence[str]) -> str:
    """Identity of a full path. Distinct branches with the same leaf name stay distinct.

    `Loans & Credit > Credit Card > Interest` and `Financial > Interest` are
    different things that happen to share a word, so the slug carries the whole
    path rather than just the leaf.
    """
    return "/".join(slugify(part) for part in path)


def _iter_specs(specs: Iterable[_NodeSpec]) -> Iterator[Tuple[str, List[_NodeSpec]]]:
    for spec in specs:
        if isinstance(spec, str):
            yield spec, []
        else:
            name, children = spec
            yield name, list(children)


def _build() -> Dict[str, Node]:
    dynamic = {tuple(p) for p in DYNAMIC_CHILD_PATHS}
    nodes: Dict[str, Node] = {}

    def visit(name: str, children_spec: List[_NodeSpec],
              parent_path: Tuple[str, ...], parent_slug: Optional[str]) -> str:
        path = parent_path + (name,)
        slug = path_slug(path)
        child_slugs: List[str] = []
        for child_name, grandchildren in _iter_specs(children_spec):
            child_slugs.append(visit(child_name, grandchildren, path, slug))
        nodes[slug] = Node(
            name=name,
            path=path,
            level=len(path),
            slug=slug,
            parent_slug=parent_slug,
            children=tuple(child_slugs),
            allows_dynamic_children=path in dynamic,
        )
        return slug

    for root_name, children in TREE:
        visit(root_name, children, (), None)
    return nodes


NODES: Dict[str, Node] = _build()

ROOTS: Tuple[str, ...] = tuple(name for name, _ in TREE)
ROOT_SET = set(ROOTS)

# Every fixed path in the tree, by its display form, for name-based lookup.
_BY_DISPLAY_PATH: Dict[str, Node] = {n.display_path.lower(): n for n in NODES.values()}

# Level-1 lookup by name, case-insensitively.
_ROOT_BY_NAME: Dict[str, str] = {name.lower(): name for name in ROOTS}


# ---------------------------------------------------------------------------
# Lookup and validation
# ---------------------------------------------------------------------------

def get(slug: str) -> Optional[Node]:
    return NODES.get(slug)


def node_for_path(path: Sequence[str]) -> Optional[Node]:
    """The fixed-tree node at `path`, or None if the path is not in the tree.

    A path ending in a dynamically-minted child (an employer, a supplier) will
    not be found here — by construction, since it is not in the tree. Use
    `is_valid_path` when you need to know whether a path is *permitted*.
    """
    if not path:
        return None
    return NODES.get(path_slug(path))


def is_valid_path(path: Sequence[str]) -> bool:
    """Is this a path the tree permits?

    True when the path is in the fixed tree, or when it is one dynamic child
    below a node that allows them. Two invented levels are never permitted:
    the classifier may name a merchant, not a whole sub-taxonomy.
    """
    if not path:
        return False
    if node_for_path(path) is not None:
        return True
    parent = node_for_path(path[:-1])
    return parent is not None and parent.allows_dynamic_children and bool(str(path[-1]).strip())


def children(path: Sequence[str] = ()) -> List[Node]:
    """Fixed children of a path. Empty path returns the roots."""
    if not path:
        return [NODES[path_slug((name,))] for name in ROOTS]
    node = node_for_path(path)
    if node is None:
        return []
    return [NODES[slug] for slug in node.children]


def normalize_root(name: Optional[str]) -> Optional[str]:
    """Canonical level-1 name for any casing, or None if it is not a root."""
    if not name:
        return None
    return _ROOT_BY_NAME.get(str(name).strip().lower())


def parse_display_path(text: Optional[str]) -> Tuple[str, ...]:
    """Split a stored `A > B > C` string back into a path tuple."""
    if not text:
        return ()
    parts = [p.strip() for p in str(text).split(">")]
    return tuple(p for p in parts if p)


def format_path(path: Sequence[str]) -> str:
    return " > ".join(path)


def trim_to_valid(path: Sequence[str]) -> Tuple[str, ...]:
    """Cut a path back to its longest prefix the tree permits.

    The safety net behind the depth rule. If some caller assembles a path with a
    level the tree does not have, the answer is to return the part that is true
    rather than to reject the whole thing — a correct two-level answer beats an
    exception, and beats a made-up third level even more.
    """
    best: Tuple[str, ...] = ()
    for i in range(1, len(path) + 1):
        candidate = tuple(str(p).strip() for p in path[:i])
        if is_valid_path(candidate):
            best = candidate
        else:
            break
    return best


def ancestors(path: Sequence[str]) -> List[Tuple[str, ...]]:
    """Every prefix of `path`, shortest first, including the path itself."""
    return [tuple(path[:i]) for i in range(1, len(path) + 1)]


def iter_nodes() -> Iterator[Node]:
    """Every node in the fixed tree, parents always before their children.

    Ordered so a seeder can insert straight through without deferring foreign
    keys.
    """
    seen: set = set()

    def walk(node: Node) -> Iterator[Node]:
        if node.slug in seen:
            return
        seen.add(node.slug)
        yield node
        for child_slug in node.children:
            yield from walk(NODES[child_slug])

    for root in ROOTS:
        yield from walk(NODES[path_slug((root,))])


def max_depth() -> int:
    return max(n.level for n in NODES.values())


__all__ = [
    "TREE", "NODES", "ROOTS", "Node",
    "VENDOR_UNKNOWN", "DYNAMIC_CHILD_PATHS", "residual_path",
    "RESIDUAL_ATM_OUT", "RESIDUAL_CASH_IN", "RESIDUAL_CASH_OUT",
    "RESIDUAL_VENDOR", "RESIDUAL_PERSON", "RESIDUAL_TRANSFER",
    "RESIDUAL_REVERSAL",
    "slugify", "path_slug", "get", "node_for_path", "is_valid_path",
    "children", "normalize_root", "parse_display_path", "format_path",
    "trim_to_valid", "ancestors", "iter_nodes", "max_depth",
    # Level-1 names
    "INCOME", "FOOD_DINING", "SHOPPING", "HOUSING", "TRANSPORTATION",
    "BILLS_UTILITIES", "HEALTHCARE", "EDUCATION", "ENTERTAINMENT", "TRAVEL",
    "FINANCIAL", "TRANSFERS", "INVESTMENTS", "LOANS_CREDIT", "INSURANCE",
    "TAXES_GOVERNMENT", "BUSINESS", "PERSONAL_FAMILY", "CASH", "DONATIONS",
    "FEES_CHARGES", "REFUNDS_REVERSALS",
]
