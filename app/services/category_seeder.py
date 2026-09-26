import logging
import uuid
from typing import Dict, List, Optional
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.prediction import Prediction

logger = logging.getLogger(__name__)

STANDARD_CATEGORIES: List[str] = [
    "Food & Dining",
    "Utilities",
    "Utility Payment",
    "Salary Payment",
    "Vendor Payment",
    "Bank Charges",
    "Internal Fund Transfer",
    "Customer Payment / NEFT Transfer",
    "Merchant Settlement",
    "Loan Disbursement",
    "Loan Repayment / EMI",
    "Government Fee",
    "Fuel",
    "POS/Card Purchase",
    "Travel",
    "IMPS Transfer",
    "NEFT Transfer",
    "RTGS Transfer",
    "UPI Transfer",
    "GST/Tax Payment",
    "Cash Deposit",
    "ATM Withdrawal",
    "Interest Credit",
    "Interest Debit",
    "Dividend",
    "Insurance Premium",
    "Rent Payment",
    "Refund",
    "Reversal",
    "Software & Cloud",
    "Sales Income",
    "Professional Fees",
    "Office Expenses",
    "Others",
    "Uncategorized"
]

CATEGORY_ALIASES: Dict[str, str] = {
    "settlement": "Merchant Settlement",
    "tax": "GST/Tax Payment",
    "self transfer": "Internal Fund Transfer",
    "food": "Food & Dining",
    "salary": "Salary Payment",
    "utilities": "Utility Payment",
    "interest": "Interest Debit",
    "investment": "Internal Fund Transfer",
    "upi received": "Merchant Settlement",
}

def normalize_category_name(name: str) -> str:
    """Normalizes ML category names / aliases to canonical database category names."""
    if not name:
        return "Uncategorized"
    cleaned = name.strip()
    return CATEGORY_ALIASES.get(cleaned.lower(), cleaned)


def seed_categories(db: Session) -> Dict[str, uuid.UUID]:
    """
    Seeds standard category taxonomy into the database if missing.
    Returns a dictionary mapping lowercase category names (including aliases) to Category UUIDs.

    Only TOP-LEVEL and legacy rows go into the map. Since the categories table
    became a tree, `name` is no longer unique — `Interest` exists under both
    `Financial` and `Loans & Credit > Credit Card`, and `Mobile` under both
    `Bills & Utilities` and `Shopping > Electronics`. A flat name -> id map
    cannot represent that, and every caller of this function is resolving a
    flat name emitted by the rule engine or the model, which is always a
    level-1 or legacy name. Deeper nodes are resolved by slug through
    `resolve_path` instead.
    """
    existing = (
        db.query(Category)
        .filter(or_(Category.level.is_(None), Category.level == 1))
        .all()
    )
    cat_map: Dict[str, uuid.UUID] = {c.name.lower(): c.id for c in existing}

    # Every name any classifier can emit must exist as a Category row, or a
    # confident classification cannot be written: the classifier names a category,
    # the FK lookup misses, and the transaction is left with category_id NULL
    # while its prediction claims a category. That mismatch stranded 30 rows.
    #
    # Three vocabularies coexist and all three must be seeded:
    #   PURPOSES            - what the ML model predicts as of the 18-category
    #                         retrain (mlmodel/train_purpose_classifier.py)
    #   CANONICAL_CATEGORIES- the 15 names the rule engine still emits
    #   STANDARD_CATEGORIES - the older 43-term treasury vocabulary
    from app.categorization.dual_taxonomy import PURPOSES
    from app.categorization.taxonomy import CATEGORIES as CANONICAL_CATEGORIES

    seed_names: List[str] = []
    seen: set = set()
    for name in list(PURPOSES) + list(CANONICAL_CATEGORIES) + list(STANDARD_CATEGORIES):
        if name.lower() not in seen:
            seen.add(name.lower())
            seed_names.append(name)

    added = False
    for name in seed_names:
        if name.lower() not in cat_map:
            cat_obj = Category(
                id=uuid.uuid4(),
                name=name,
                description=f"Standard system category: {name}",
                is_custom=False
            )
            db.add(cat_obj)
            cat_map[name.lower()] = cat_obj.id
            added = True

    if added:
        try:
            db.commit()
            logger.info("Successfully seeded canonical categories into categories table.")
        except Exception as e:
            db.rollback()
            logger.error(f"Error seeding categories: {e}")
            existing = db.query(Category).all()
            cat_map = {c.name.lower(): c.id for c in existing}

    # Register aliases pointing to canonical category UUIDs
    for alias, canonical_name in CATEGORY_ALIASES.items():
        canonical_id = cat_map.get(canonical_name.lower())
        if canonical_id:
            cat_map[alias.lower()] = canonical_id

    return cat_map


def resolve_transaction_categories(db: Session) -> int:
    """
    Resolves category_id for transactions where category_id IS NULL
    by linking Prediction.predicted_category to Category.id.
    
    Precedence:
    1. Explicit User Category (Transaction.category_id already set -> skip)
    2. Prediction.predicted_category -> Category.id
    3. Uncategorized (category_id remains NULL or assigned to Uncategorized Category)
    
    Returns the count of transactions resolved.
    """
    cat_map = seed_categories(db)
    uncat_id = cat_map.get("uncategorized")

    # Fetch transactions needing resolution
    unresolved_txns = db.query(Transaction).filter(Transaction.category_id.is_(None)).all()
    if not unresolved_txns:
        return 0

    resolved_count = 0
    for tx in unresolved_txns:
        # Check linked prediction first
        pred = db.query(Prediction).filter(Prediction.transaction_id == tx.id).first()
        target_cat_name = None
        if pred and pred.predicted_category:
            target_cat_name = pred.predicted_category
        elif tx.narration_clean:
            target_cat_name = tx.narration_clean

        if target_cat_name:
            matched_id = cat_map.get(target_cat_name.lower())
            if matched_id and matched_id != uncat_id:
                tx.category_id = matched_id
                resolved_count += 1
                if pred:
                    pred.category_id = matched_id
            elif target_cat_name.lower() == "uncategorized" and uncat_id:
                # Optionally link to explicit Uncategorized category or leave null
                tx.category_id = uncat_id
                if pred:
                    pred.category_id = uncat_id

    if resolved_count > 0:
        try:
            db.commit()
            logger.info(f"Resolved category_id for {resolved_count} transactions.")
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to commit resolved transaction categories: {e}")

    return resolved_count


# ===========================================================================
# The category tree
# ===========================================================================

def seed_category_tree(db: Session) -> Dict[str, uuid.UUID]:
    """Write `app/categorization/hierarchy.py` into the categories table.

    Idempotent, and careful about one thing: a level-1 node whose name already
    exists as a legacy flat row is UPGRADED IN PLACE rather than duplicated.
    Inserting a second `Food & Dining` would leave every transaction that
    already points at the first one outside the tree, which is the same class
    of bug as a prediction naming a category the database has no row for.

    Returns slug -> id for every node in the fixed tree.
    """
    from app.categorization import hierarchy as H

    by_slug: Dict[str, uuid.UUID] = {
        c.slug: c.id for c in db.query(Category).filter(Category.slug.isnot(None)).all()
    }
    # Legacy rows have no slug and no parent. Matched by name so their id — and
    # therefore every transaction pointing at them — survives.
    legacy_by_name: Dict[str, Category] = {}
    for c in db.query(Category).filter(Category.slug.is_(None)).all():
        legacy_by_name.setdefault(c.name.strip().lower(), c)

    created = upgraded = 0
    order: Dict[Optional[uuid.UUID], int] = {}

    for node in H.iter_nodes():          # parents always before children
        parent_id = by_slug.get(node.parent_slug) if node.parent_slug else None
        seq = order.get(parent_id, 0)
        order[parent_id] = seq + 1

        row = None
        if node.slug in by_slug:
            row = db.query(Category).filter(Category.id == by_slug[node.slug]).first()
        elif node.level == 1:
            row = legacy_by_name.get(node.name.strip().lower())

        if row is None:
            row = Category(id=uuid.uuid4(), name=node.name)
            db.add(row)
            created += 1
        else:
            upgraded += 1

        row.name = node.name
        row.parent_id = parent_id
        row.slug = node.slug
        row.level = node.level
        row.path = node.display_path
        row.sort_order = seq
        row.is_active = True
        if row.is_custom is None:
            row.is_custom = False

        db.flush()
        by_slug[node.slug] = row.id

    if created or upgraded:
        try:
            db.commit()
            logger.info(
                "[Categories] tree seeded: %d node(s) created, %d existing row(s) "
                "linked into the tree.", created, upgraded,
            )
        except Exception as exc:
            db.rollback()
            logger.error("[Categories] tree seed failed: %s", exc)
            by_slug = {
                c.slug: c.id
                for c in db.query(Category).filter(Category.slug.isnot(None)).all()
            }

    return by_slug


def resolve_path(db: Session, path, tree_map: Optional[Dict[str, uuid.UUID]] = None):
    """The tree node for a classified path, and the path actually stored.

    Returns `(node_id, stored_path)`.

    NOTE the node id is NOT what goes into `Transaction.category_id` — that
    column carries the flat category, because the review queue and the reports
    resolve a category NAME through it. This is here for callers that genuinely
    want the tree row.

    The id is the deepest node that exists in the FIXED tree. A level that came
    from a narration — an employer under Salary, a supplier under Inventory —
    gets no row, because this table has no user_id and a row named after one
    user's supplier would show up in every other user's tree. That level lives
    in `Transaction.category_path`, which is user-scoped, and the drill-down
    aggregates it from there.

    `stored_path` is the full path including any such level, so nothing the
    classifier concluded is thrown away.
    """
    from app.categorization import hierarchy as H

    path = tuple(str(p).strip() for p in (path or ()) if str(p).strip())
    if not path:
        return None, None

    if tree_map is None:
        tree_map = {
            c.slug: c.id
            for c in db.query(Category).filter(Category.slug.isnot(None)).all()
        }

    category_id = None
    for prefix in H.ancestors(path):
        slug = H.path_slug(prefix)
        if slug in tree_map:
            category_id = tree_map[slug]
        else:
            break

    return category_id, H.format_path(path)


def apply_decision(db: Session, tx, legacy_name: str,
                   tree_map: Optional[Dict[str, uuid.UUID]] = None) -> None:
    """Record a human's category choice on both axes at once.

    The review queue speaks the old vocabulary — "Cost of Goods", "Bank Fees" —
    because that is what its dropdown, its saved counterparty decisions and the
    P&L reports are built from. The tree is the axis the drill-down reads. If a
    confirmation updated only one of them they would drift apart within a day,
    and the drill-down would quietly stop reflecting what people actually
    decided.

    The tree side is derived, never guessed: an old name with no honest place in
    the tree (a rail name like "NEFT Transfer") leaves the tree fields alone
    rather than inventing a home for it.
    """
    from app.categorization.deep import anchor_path

    tx.legacy_category = legacy_name

    path = anchor_path(legacy_name)
    if not path:
        return

    _node_id, stored = resolve_path(db, path, tree_map)
    tx.category = path[0]
    tx.category_path = stored
    # `category_id` is left to the caller: it carries the FLAT category, which
    # is what the review queue and the reports resolve a name through, and the
    # caller has just set it from the name the human chose.
    # A person said so. That is the highest confidence this system records.
    tx.category_confidence = 1.0
