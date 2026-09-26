"""Drill-down: one level at a time, and never a level that leads nowhere.

The failure this guards against is subtle and only shows up in the UI. A
drill-down built on a fixed-depth assumption will happily offer a third level
under `Transfers > Own Account Transfer`, the user clicks, and gets an empty
list — or worse, a level that exists in the taxonomy but holds none of *their*
transactions, so every category looks explorable and half of them are dead ends.

So `has_children` is not read off the taxonomy. It is computed from the rows
that are actually there, per user, per filter. These tests pin that: a node is
explorable exactly when something under it goes deeper.

The tree seeding is checked here too, because it has one property that cannot
be tested from the classifier alone — a legacy `Food & Dining` row must be
UPGRADED into the tree rather than duplicated, or every transaction already
pointing at it silently falls outside the hierarchy.
"""

import datetime
import uuid

import pytest

from app.categorization import hierarchy as H
from app.categorization.deep import classify_deep
from app.models.category import Category
from app.models.transaction import Direction, SourceType, Transaction
from app.services.category_seeder import (
    resolve_path, seed_categories, seed_category_tree,
)
from tests.conftest import TestingSessionLocal, register_bank_account

# Chosen so the resulting tree is deliberately ragged: some branches reach three
# levels, others stop at two.
#
# There is deliberately no depth-1 row here any more. The one that used to
# supply it was `Other / Uncategorized`, a bare root with nothing beneath it,
# deleted from the taxonomy on 2026-08-19. Every remaining root has children and
# the residual floor that replaced it always names a rail AND a direction
# (`Cash > ATM Withdrawal`, `Transfers > External Transfer`), so nothing the
# classifier returns can stop at a single level. Depth 1 survives only for rows
# that predate the hierarchy — see
# `test_a_legacy_row_with_no_path_still_lands_at_depth_one`.
NARRATIONS = [
    ("UPI/DR/1/ZOMATO/YESB",                    45_000, 0),
    ("UPI/DR/2/ZOMATO/YESB",                    32_000, 0),
    ("UPI/DR/3/SWIGGY/YESB",                    51_000, 0),
    ("POS 4321 DMART SUPERMARKET",             210_000, 0),
    ("POS 4321 AMAZON.IN",                     120_000, 0),
    ("ATM WDL 1234 MUMBAI",                    500_000, 0),
    ("TRF TO SELF 50100XXXX",                2_000_000, 0),
    ("NACH DR HDFC HOME LOAN EMI",           3_500_000, 0),
    ("GST PAYMENT GSTN 27AAAA",              1_500_000, 0),
    ("XX99812 004",                              1_000, 0),
    ("NEFT SALARY AUG 2026",                         0, 12_000_000),
]


@pytest.fixture
def auth(client):
    email = f"drill_{uuid.uuid4().hex[:6]}@example.com"
    client.post("/auth/register", json={"email": email, "password": "Password123!"})
    tok = client.post("/auth/login",
                      json={"email": email, "password": "Password123!"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}
    user_id = uuid.UUID(client.get("/auth/me", headers=headers).json()["id"])
    register_bank_account(client, headers,
                          account_number=f"5080{uuid.uuid4().int % 10**8:08d}")
    yield headers, user_id
    db = TestingSessionLocal()
    try:
        db.query(Transaction).filter(Transaction.user_id == user_id).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def ledger(auth):
    """Rows classified by the real classifier, then stored the way ingestion does."""
    _headers, user_id = auth
    db = TestingSessionLocal()
    try:
        tree = seed_category_tree(db)
        for i, (narration, debit, credit) in enumerate(NARRATIONS):
            result = classify_deep(narration,
                                   direction="debit" if debit else "credit")
            cat_id, stored_path = resolve_path(db, result.path, tree)
            db.add(Transaction(
                id=uuid.uuid4(), user_id=user_id,
                direction=Direction.DEBIT if debit else Direction.CREDIT,
                debit_paise=debit or None, credit_paise=credit or None,
                txn_date=datetime.date(2026, 8, 1) + datetime.timedelta(days=i),
                narration_raw=narration, narration_clean=narration,
                source_type=SourceType.STATEMENT, booked_currency="INR",
                category_id=cat_id, category=result.category,
                category_path=stored_path,
                category_confidence=round(result.confidence, 3),
                flow_type=result.flow_type,
                transaction_method=result.transaction_method,
                merchant=result.merchant, counterparty=result.counterparty,
            ))
        db.commit()
    finally:
        db.close()
    return auth


def drill(client, headers, path=None):
    params = {"path": path} if path else {}
    res = client.get("/v1/categories/drilldown", headers=headers, params=params)
    assert res.status_code == 200, res.text
    return res.json()


# ===========================================================================
# Seeding
# ===========================================================================

class TestSeeding:
    def test_the_whole_tree_lands_in_the_table(self):
        db = TestingSessionLocal()
        try:
            tree = seed_category_tree(db)
            assert len(tree) == sum(1 for _ in H.iter_nodes())
            root = db.query(Category).filter(
                Category.slug == H.path_slug((H.FOOD_DINING,))).first()
            assert root is not None and root.level == 1 and root.parent_id is None
            leaf = db.query(Category).filter(
                Category.slug == H.path_slug(
                    (H.FOOD_DINING, "Restaurants", "Fast Food"))).first()
            assert leaf.level == 3 and leaf.path == "Food & Dining > Restaurants > Fast Food"
        finally:
            db.close()

    def test_seeding_twice_changes_nothing(self):
        db = TestingSessionLocal()
        try:
            seed_category_tree(db)
            before = db.query(Category).count()
            seed_category_tree(db)
            assert db.query(Category).count() == before
        finally:
            db.close()

    def test_a_legacy_row_is_upgraded_not_duplicated(self):
        """The load-bearing one.

        A second `Food & Dining` row would leave every transaction already
        pointing at the first one outside the tree — categorised according to
        the database, invisible to the drill-down.
        """
        db = TestingSessionLocal()
        try:
            seed_categories(db)          # writes the flat vocabulary
            legacy = db.query(Category).filter(
                Category.name == "Food & Dining").first()
            assert legacy is not None
            legacy_id = legacy.id

            seed_category_tree(db)

            rows = db.query(Category).filter(Category.name == "Food & Dining").all()
            assert len(rows) == 1
            assert rows[0].id == legacy_id
            assert rows[0].slug == H.path_slug((H.FOOD_DINING,))
        finally:
            db.close()

    def test_the_same_name_may_now_exist_on_two_branches(self):
        db = TestingSessionLocal()
        try:
            seed_category_tree(db)
            names = [c.path for c in
                     db.query(Category).filter(Category.name == "Interest").all()]
            assert "Financial > Interest" in names
            assert "Loans & Credit > Credit Card > Interest" in names
        finally:
            db.close()

    def test_a_narration_derived_level_gets_no_row_of_its_own(self):
        """This table has no user_id. A row named after one user's supplier
        would appear in every other user's tree."""
        db = TestingSessionLocal()
        try:
            tree = seed_category_tree(db)
            path = (H.INCOME, "Salary", "ABC Technologies")
            cat_id, stored = resolve_path(db, path, tree)
            node = db.query(Category).filter(Category.id == cat_id).first()

            assert stored == "Income > Salary > ABC Technologies"
            assert node.path == "Income > Salary"      # stops at the fixed tree
            assert db.query(Category).filter(
                Category.name == "ABC Technologies").count() == 0
        finally:
            db.close()


# ===========================================================================
# Drill-down
# ===========================================================================

class TestDrillDown:
    def test_the_top_level_lists_only_categories_with_transactions(self, client, ledger):
        headers, _ = ledger
        body = drill(client, headers)
        names = {c["name"] for c in body["children"]}

        assert H.FOOD_DINING in names
        assert H.CASH in names
        # 22 categories exist; these rows populate a handful.
        assert len(names) < 12
        assert H.INSURANCE not in names

    def test_children_are_ordered_by_size_not_alphabetically(self, client, ledger):
        headers, _ = ledger
        body = drill(client, headers)
        volumes = [c["total_debit"] + c["total_credit"] for c in body["children"]]
        assert volumes == sorted(volumes, reverse=True)

    def test_a_node_is_explorable_exactly_when_something_goes_deeper(self, client, ledger):
        headers, _ = ledger
        food = drill(client, headers, "Food & Dining")
        by_name = {c["name"]: c for c in food["children"]}

        # Two Zomato rows and one Swiggy sit under Food Delivery.
        assert by_name["Food Delivery"]["has_children"] is True
        delivery = drill(client, headers, "Food & Dining > Food Delivery")
        assert {c["name"] for c in delivery["children"]} == {"Zomato", "Swiggy"}
        assert by_name["Food Delivery"]["transaction_count"] == 3

    def test_a_two_level_answer_offers_no_third_level(self, client, ledger):
        """`Transfers > Own Account Transfer` is complete. Offering a click here
        would land the user on an empty page."""
        headers, _ = ledger
        transfers = drill(client, headers, "Transfers")
        own = next(c for c in transfers["children"]
                   if c["name"] == "Own Account Transfer")
        assert own["has_children"] is False

        node = drill(client, headers, "Transfers > Own Account Transfer")
        assert node["children"] == []
        assert node["is_terminal"] is True
        assert node["transaction_count"] == 1

    def test_no_branch_of_the_drill_down_is_a_bucket_for_not_knowing(self, client, ledger):
        """The screen must never offer `Other / Uncategorized` again. A user who
        opens it learns nothing, cannot reconcile it, and cannot act on it — and
        on the statement that prompted this it was 27% of the money."""
        headers, _ = ledger
        top = drill(client, headers)
        assert all(c["name"] != "Other / Uncategorized" for c in top["children"])

    def test_depth_genuinely_varies_across_the_same_result_set(self, client, ledger):
        """One result set, more than one depth — the drill-down cannot assume a
        fixed number of levels.

        This used to require depth 1 as well. It no longer can: the only depth-1
        answer the classifier ever produced was `Other / Uncategorized`, a bare
        root deleted on 2026-08-19, and every root that remains has children
        while the residual floor is always two levels deep. Requiring depth 1
        from classifier output now asserts a shape the taxonomy deliberately
        cannot have, so the requirement moved to where depth 1 still genuinely
        occurs — `test_a_legacy_row_with_no_path_still_lands_at_depth_one`.

        The intent is unchanged and is asserted directly below: depth varies,
        and it varies because BOTH reachable depths are really populated rather
        than one depth holding everything and a single stray sitting elsewhere.
        """
        headers, _ = ledger
        res = client.get("/v1/categories/summary", headers=headers)
        assert res.status_code == 200, res.text
        distribution = res.json()["depth_distribution"]

        # Depth genuinely varies rather than the tree being flat.
        assert len(distribution) > 1, distribution
        # Both depths the current taxonomy can produce are present...
        assert set(distribution) >= {"2", "3"}, distribution
        # ...and each holds several rows, so neither is a rounding error. A
        # distribution like {"2": 10, "3": 1} would pass a bare set check while
        # the tree was effectively fixed-depth.
        assert distribution["2"] >= 2 and distribution["3"] >= 2, distribution
        # Nothing lands on a bare root: every row here went through the
        # classifier, and the classifier can no longer return a one-level path.
        assert "1" not in distribution, distribution

    def test_a_legacy_row_with_no_path_still_lands_at_depth_one(self, client, auth):
        """Depth 1 has not stopped existing — it stopped coming from the classifier.

        A row ingested before the hierarchy existed carries a flat `category`
        and no `category_path`. `_row_path` places it at that bare category
        instead of dropping it out of the drill-down, which would make the
        totals disagree with every other screen. That is a real depth-1 outcome
        and it is what the depth test above can no longer supply for itself.
        """
        headers, user_id = auth
        db = TestingSessionLocal()
        try:
            db.add(Transaction(
                id=uuid.uuid4(), user_id=user_id, direction=Direction.DEBIT,
                debit_paise=12_345,
                txn_date=datetime.date(2026, 8, 1),
                narration_raw="LEGACY ROW", narration_clean="LEGACY ROW",
                source_type=SourceType.STATEMENT, booked_currency="INR",
                category=H.FOOD_DINING, category_path=None,
            ))
            db.commit()
        finally:
            db.close()

        res = client.get("/v1/categories/summary", headers=headers)
        assert res.status_code == 200, res.text
        assert res.json()["depth_distribution"] == {"1": 1}, res.json()

    def test_the_breadcrumb_carries_every_ancestor(self, client, ledger):
        headers, _ = ledger
        node = drill(client, headers, "Food & Dining > Food Delivery")
        assert [b["name"] for b in node["breadcrumb"]] == ["Food & Dining", "Food Delivery"]
        assert node["level"] == 2

    def test_totals_at_a_node_include_everything_beneath_it(self, client, ledger):
        headers, _ = ledger
        food = drill(client, headers, "Food & Dining")
        child_total = sum(c["total_debit"] for c in food["children"])
        assert abs(food["total_debit"] - child_total) < 0.01

    def test_a_slash_separated_path_works_too(self, client, ledger):
        headers, _ = ledger
        a = drill(client, headers, "Food & Dining > Food Delivery")
        b = drill(client, headers, "Food & Dining/Food Delivery")
        assert a["children"] == b["children"]


# ===========================================================================
# Transactions at a node
# ===========================================================================

class TestTransactionsAtANode:
    def test_listing_a_node_returns_its_descendants(self, client, ledger):
        headers, _ = ledger
        res = client.get("/v1/categories/transactions", headers=headers,
                         params={"path": "Food & Dining"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["total"] == 4
        assert all(t["category_path"].startswith("Food & Dining")
                   for t in body["transactions"])

    def test_excluding_descendants_lists_only_rows_that_stop_here(self, client, ledger):
        headers, _ = ledger
        res = client.get("/v1/categories/transactions", headers=headers,
                         params={"path": "Food & Dining", "include_descendants": False})
        # Every food row in this fixture goes deeper, so nothing stops at level 1.
        assert res.json()["total"] == 0

    def test_the_levels_are_returned_separately_as_well_as_joined(self, client, ledger):
        headers, _ = ledger
        res = client.get("/v1/categories/transactions", headers=headers,
                         params={"path": "Food & Dining > Food Delivery > Zomato"})
        row = res.json()["transactions"][0]
        assert row["category"] == "Food & Dining"
        assert row["subcategory"] == "Food Delivery"
        assert row["specific_category"] == "Zomato"
        assert row["detail"] is None          # null, not padded

    def test_merchant_is_reported_separately_from_category(self, client, ledger):
        """Amazon is who was paid. The category is what for, and for a
        marketplace those are different facts."""
        headers, _ = ledger
        res = client.get("/v1/categories/transactions", headers=headers,
                         params={"path": "Shopping"})
        row = res.json()["transactions"][0]
        assert row["merchant"] == "Amazon"
        assert row["category"] == "Shopping"
        assert row["subcategory"] == "Online Shopping"

    def test_another_users_rows_are_not_visible(self, client, ledger):
        headers, _ = ledger
        other = f"other_{uuid.uuid4().hex[:6]}@example.com"
        client.post("/auth/register", json={"email": other, "password": "Password123!"})
        tok = client.post("/auth/login",
                          json={"email": other, "password": "Password123!"}).json()["access_token"]
        res = client.get("/v1/categories/drilldown",
                         headers={"Authorization": f"Bearer {tok}"})
        assert res.json()["transaction_count"] == 0


# ===========================================================================
# The taxonomy endpoint
# ===========================================================================

class TestTreeEndpoint:
    def test_the_tree_endpoint_returns_the_taxonomy_not_the_user_s_data(self, client, auth):
        headers, _ = auth
        res = client.get("/v1/categories/tree", headers=headers)
        assert res.status_code == 200, res.text
        roots = res.json()
        # 22, not 23: `Other / Uncategorized` was deleted from the taxonomy on
        # 2026-08-19 (23 roots → 22, 213 nodes → 212).
        assert len(roots) == 22
        assert all(r["name"] != "Other / Uncategorized" for r in roots)
        food = next(r for r in roots if r["name"] == "Food & Dining")
        assert {c["name"] for c in food["children"]} >= {"Restaurants", "Food Delivery"}
        transfers = next(r for r in roots if r["name"] == "Transfers")
        own = next(c for c in transfers["children"] if c["name"] == "Own Account Transfer")
        assert own["children"] == []
        assert own["accepts_custom_child"] is False
