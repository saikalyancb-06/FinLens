"""The hierarchy is only worth having if it refuses to make things up.

Every test here is about one of two failure modes, and they pull in opposite
directions:

  TOO SHALLOW — the classifier had the evidence for `Food & Dining > Food
  Delivery > Zomato` and stopped at `Food & Dining`. The drill-down is then a
  list of one level and the feature is pointless.

  TOO DEEP — the classifier had `AMAZON` and answered `Shopping > Electronics >
  Laptop`. This is the dangerous one, because a fabricated level looks exactly
  like a real one in the UI. The user sees a confident answer and has no way to
  know it was invented; and unlike a missing level, a wrong one silently
  poisons every total it is counted in.

So the suite is deliberately lopsided. There are more tests pinning where the
path STOPS than tests pinning where it goes.

These tests need no database, no model and no fixtures — the classifier is a
pure function of a narration and whatever context the caller happens to have.
That is a property worth keeping: it is why an unfamiliar bank can be checked
by pasting a narration into a REPL.
"""

import pytest

from app.categorization import flow as F
from app.categorization import hierarchy as H
from app.categorization import merchants as M
from app.categorization.deep import (
    REVIEW_THRESHOLD, anchor_path, classify_deep, looks_like_organisation,
)


def path_of(narration, **kw):
    return classify_deep(narration, **kw).path


# ===========================================================================
# The tree
# ===========================================================================

class TestTree:
    def test_every_top_level_category_exists(self):
        # 22 since `Other / Uncategorized` was removed. It was the 23rd and it
        # was the only one that did not name something the money did.
        assert len(H.ROOTS) == 22
        for name in H.ROOTS:
            assert H.node_for_path((name,)) is not None

    def test_depth_varies_between_branches(self):
        """The whole premise. If every branch were the same depth this would be
        a flat taxonomy with extra columns."""
        depths = {len(node.path) for node in H.iter_nodes()}
        assert depths == {1, 2, 3}, depths

        assert H.node_for_path((H.TRANSFERS, "Own Account Transfer")).is_leaf
        assert not H.node_for_path((H.FOOD_DINING, "Restaurants")).is_leaf

    def test_there_is_no_bucket_for_not_knowing(self):
        """`Other / Uncategorized` used to be a root here and it was a category
        for the absence of a category — untotallable, unreconcilable, and on a
        real statement it held a quarter of the rows. Every root now names
        something money was actually for or actually did."""
        assert "Other / Uncategorized" not in H.ROOTS
        assert not hasattr(H, "UNCATEGORIZED")

    def test_slugs_are_unique_and_carry_the_branch(self):
        slugs = [n.slug for n in H.iter_nodes()]
        assert len(slugs) == len(set(slugs))
        # Two different `Interest` nodes exist and must not collide.
        assert (H.path_slug((H.FINANCIAL, "Interest"))
                != H.path_slug((H.LOANS_CREDIT, "Credit Card", "Interest")))

    def test_parents_come_before_children(self):
        """A seeder walks this order and sets parent_id as it goes."""
        seen = set()
        for node in H.iter_nodes():
            if node.parent_slug is not None:
                assert node.parent_slug in seen
            seen.add(node.slug)

    def test_a_name_may_be_added_only_where_the_tree_invites_one(self):
        assert H.is_valid_path((H.INCOME, "Salary", "ABC Technologies"))
        # ...and only one level of it. A merchant is a name, not a sub-taxonomy.
        assert not H.is_valid_path((H.INCOME, "Salary", "ABC Technologies", "Bonus"))
        # Under an own-account transfer a name is the other ACCOUNT, and giving
        # it a node produces one category per account number.
        assert not H.is_valid_path((H.TRANSFERS, "Own Account Transfer", "ICICI 1234"))

    def test_trim_returns_the_true_prefix_rather_than_failing(self):
        assert H.trim_to_valid(
            (H.TRANSFERS, "Own Account Transfer", "ICICI 1234")
        ) == (H.TRANSFERS, "Own Account Transfer")
        assert H.trim_to_valid(("Nonsense", "Thing")) == ()


# ===========================================================================
# Where the path stops
# ===========================================================================

class TestDepthDiscipline:
    def test_a_marketplace_stops_at_the_merchant(self):
        """The Amazon rule.

        Amazon sells laptops and lentils. Knowing the shop is not knowing the
        purchase, and `Shopping > Electronics > Laptop` here would be a
        fabrication that no part of the UI could flag.
        """
        assert path_of("POS 4321XXXXXX1234 AMAZON.IN", direction="debit") == (
            H.SHOPPING, "Online Shopping", "Amazon")

    def test_a_marketplace_goes_deeper_when_the_narration_names_the_product(self):
        assert path_of("POS AMAZON.IN MOBILE PHONE PURCHASE", direction="debit") == (
            H.SHOPPING, "Electronics", "Mobile")

    def test_a_company_name_alone_does_not_imply_what_was_bought(self):
        """`NEFT-ABC-PVT-LTD` must not become Raw Materials, or Marketing, or
        anything else the narration did not say."""
        result = classify_deep("NEFT-CITIN25081234-ABC PVT LTD", direction="debit")
        assert result.path[:2] == H.VENDOR_UNKNOWN
        assert result.confidence < REVIEW_THRESHOLD
        assert result.needs_review

    def test_an_own_account_transfer_stops_at_two_levels(self):
        result = classify_deep("TRF TO SELF 50100XXXXXX1234", direction="debit")
        assert result.path == (H.TRANSFERS, "Own Account Transfer")
        assert result.specific_category is None
        assert result.depth == 2

    def test_an_unreadable_narration_is_filed_by_what_it_does_say(self):
        """Nothing here names a purpose, and the answer is not a shrug. The row
        still states that money left the account, and saying so is both true and
        something a report can add up. What is missing — the purpose — is
        reported as a review flag, which is a status, not a category."""
        result = classify_deep("XX9982371 004", direction="debit")
        assert result.path == H.RESIDUAL_TRANSFER
        assert H.is_valid_path(result.path)
        assert result.needs_review

    @pytest.mark.parametrize("narration,direction,expected", [
        ("ATM WDL 998877 SBIN", "debit", ("Cash", "ATM Withdrawal")),
        ("XXXXXX9982 004", "debit", ("Transfers", "External Transfer")),
    ])
    def test_the_residual_states_the_rail_and_nothing_more(
            self, narration, direction, expected):
        """The line this draws: `Cash > ATM Withdrawal` is printed on the
        statement. What the cash was spent on is not, and no branch below
        pretends otherwise."""
        result = classify_deep(narration, direction=direction)
        assert result.path[:2] == expected

    def test_a_weak_answer_is_not_dressed_up_with_a_party_name(self):
        """Naming a party under a category we are 45% sure of makes a vague
        answer look specific. Depth has to be earned by the same evidence that
        earned the category."""
        result = classify_deep("NEFT-XXXX-SOME TRADERS LLP", direction="debit")
        if result.confidence < REVIEW_THRESHOLD:
            assert result.path[:2] == H.VENDOR_UNKNOWN

    def test_the_serialised_form_says_null_rather_than_padding(self):
        d = classify_deep("TRF TO SELF 123", direction="debit").to_dict()
        assert d["category"] == H.TRANSFERS
        assert d["subcategory"] == "Own Account Transfer"
        assert d["specific_category"] is None
        assert d["detail"] is None
        assert d["depth"] == 2


# ===========================================================================
# Where the path goes
# ===========================================================================

class TestResolution:
    @pytest.mark.parametrize("narration,direction,expected", [
        ("UPI/DR/412345678901/ZOMATO/YESB/zomato@ybl",
         "debit", (H.FOOD_DINING, "Food Delivery", "Zomato")),
        ("ATM WDL 1234 MUMBAI", "debit", (H.CASH, "ATM Withdrawal")),
        ("NACH DR HDFC HOME LOAN EMI", "debit", (H.LOANS_CREDIT, "EMI")),
        ("GST PAYMENT GSTN 27AAAA", "debit", (H.TAXES_GOVERNMENT, "GST")),
        ("NETFLIX SUBSCRIPTION", "debit", (H.ENTERTAINMENT, "OTT / Streaming")),
        ("FASTAG TOLL RECHARGE", "debit",
         (H.TRANSPORTATION, "Vehicle Expenses", "Toll")),
        ("IOCL PETROL PUMP", "debit", (H.TRANSPORTATION, "Fuel", "Petrol")),
        ("SIP MUTUAL FUND FOLIO 1234", "debit",
         (H.INVESTMENTS, "Mutual Funds", "SIP")),
    ])
    def test_a_named_purpose_resolves_to_it(self, narration, direction, expected):
        assert path_of(narration, direction=direction) == expected

    def test_the_same_word_means_different_things_in_each_direction(self):
        """SALARY on a credit is income; on a debit it is payroll leaving a
        business account. One keyword, two categories, decided by direction."""
        assert path_of("NEFT SALARY AUG", direction="credit")[0] == H.INCOME
        assert path_of("NEFT SALARY AUG", direction="debit")[0] == H.BUSINESS

    def test_pos_terminal_rent_is_not_premises_rent(self):
        """`P05RENT_MAR25_T1D_...` is the cost of accepting cards. This system
        has already mis-filed a whole statement by reading it as rent."""
        assert path_of("P05RENT_MAR25_T1D_00012345", direction="debit") == (
            H.FINANCIAL, "Payment Gateway Charges")
        assert path_of("HOUSE RENT PAID TO LANDLORD", direction="debit") == (
            H.HOUSING, "Rent")

    def test_a_confirmed_decision_outranks_everything_computed(self):
        result = classify_deep(
            "NEFT-HDFCH2508-MEYER ORGANICS PVT LTD-HDFC BANK",
            direction="debit",
            upstream_category="Sales Income",       # would say Income
            memory_category="Cost of Goods",        # the user said otherwise
            memory_confirmations=3,
        )
        assert result.source == "memory"
        assert result.path[0] == H.BUSINESS
        assert result.confidence > 0.9

    def test_the_existing_classifier_is_used_as_the_anchor(self):
        """"The main categorization is already being done, so use that also."
        An upstream answer that the narration cannot improve on still places
        the row."""
        result = classify_deep("SOMETHING OPAQUE 8891", direction="debit",
                               upstream_category="Bank Fees",
                               upstream_confidence=0.88)
        assert result.path == (H.FINANCIAL, "Bank Charges")
        assert result.source == "upstream"

    def test_a_provisional_upstream_answer_stays_provisional(self):
        """A 0.62 guess must not be laundered into a specific-looking path.

        The narration here has to be genuinely opaque. It used to say
        `PHONEPE SETTLEMENT`, which stopped testing anything once inbound
        acquirer settlements became recognised evidence in their own right —
        the answer was then 0.86 because the narration said so, not because a
        weak upstream number had been inflated.
        """
        result = classify_deep("REF 77120 BATCH 8891", direction="credit",
                               upstream_category="Sales Income",
                               upstream_confidence=0.62,
                               upstream_requires_review=True)
        assert result.confidence <= 0.62

    def test_a_payment_aggregator_is_not_treated_as_the_merchant(self):
        """PhonePe settles for restaurants and pharmacies alike. It names the
        rail and the collector, not the business."""
        result = classify_deep("UPI/PHONEPE MERCHANT SETTLEMENT/8891",
                               direction="credit")
        assert result.counterparty is None
        assert result.merchant is None

    @pytest.mark.parametrize("account_type,expected_sub", [
        ("current", "Business Revenue"),
        ("salary", "Salary"),
    ])
    def test_account_type_breaks_a_tie_it_cannot_break_alone(
            self, account_type, expected_sub):
        """A large NEFT credit is salary for one user and revenue for another.
        Used to choose between two readings the evidence already reached —
        never to introduce a level."""
        result = classify_deep("NEFT INWARD 500000", direction="credit",
                               upstream_category="Other Income",
                               account_type=account_type)
        assert result.path == (H.INCOME, expected_sub)

    def test_a_concept_match_is_not_second_guessed_by_account_type(self):
        result = classify_deep("NEFT SALARY CREDIT AUG", direction="credit",
                               account_type="current")
        assert result.path[:2] == (H.INCOME, "Salary")


# ===========================================================================
# Flow and method — separate fields, separate confidences
# ===========================================================================

class TestFlowAndMethod:
    @pytest.mark.parametrize("narration,expected", [
        ("UPI/DR/412345/ZOMATO/YESB", F.UPI),
        ("NEFT-HDFCH2508-ACME", F.NEFT),
        ("IMPS/P2A/512345/RAHUL", F.IMPS),
        ("RTGS DR 8891", F.RTGS),
        ("ATM WDL 1234", F.ATM),
        ("CHQ PAID 000123", F.CHEQUE),
        ("NACH DR LOAN", F.ACH),
        ("POS 4321 AMAZON", F.CARD),
    ])
    def test_the_rail_is_read_from_the_narration(self, narration, expected):
        assert F.detect_method(narration).method == expected

    def test_a_rail_name_must_be_a_word_not_a_substring(self):
        """`ATM` inside `PATMOS TRADERS` turned a supplier payment into a cash
        withdrawal before the patterns were anchored."""
        assert F.detect_method("PATMOS TRADERS PAYMENT").method != F.ATM

    def test_the_parser_s_value_is_a_hint_not_a_source(self):
        """It is a second-hand reading of the same string, so it cannot be more
        reliable than reading the string here."""
        from_narration = F.detect_method("NEFT-ACME", declared="UPI")
        assert from_narration.method == F.NEFT

        fallback = F.detect_method("OPAQUE 8891", declared="UPI")
        assert fallback.method == F.UPI
        assert fallback.confidence < 0.97

    def test_a_credit_is_not_automatically_income(self):
        """The single distinction that makes flow type worth storing. Counting
        an own-account transfer as revenue overstates the business by its full
        amount."""
        transfer = F.detect_flow("credit", "TRF FROM SELF 5010012345")
        assert transfer.flow_type == F.TRANSFER
        assert not F.is_income_flow(transfer.flow_type)

    def test_a_registered_own_account_settles_it(self):
        result = F.detect_flow("credit", "NEFT FROM ANYTHING",
                               counterparty_is_self=True)
        assert result.flow_type == F.TRANSFER
        assert result.confidence > 0.95

    def test_a_reversal_is_neither_income_nor_expense(self):
        result = F.detect_flow("credit", "REVERSAL OF SERVICE CHARGE 05/24")
        assert result.flow_type == F.REVERSAL
        assert not F.is_income_flow(result.flow_type)

    def test_a_transfer_between_different_parties_is_still_a_real_flow(self):
        """"Transfer" in the everyday sense, inflow or outflow in the accounting
        sense — and reports need the accounting sense."""
        result = F.detect_flow("debit", "IMPS TO RAHUL",
                               category_path=(H.TRANSFERS, "Person to Person"))
        assert result.flow_type == F.OUTFLOW

    def test_certainty_about_the_rail_is_not_certainty_about_the_category(self):
        """Averaging them would hide both. A UPI payment to an unknown party is
        a certain rail and an unknown purpose."""
        result = classify_deep("UPI/DR/412345/UNKNOWN PARTY/YESB",
                               direction="debit")
        assert result.method_confidence > 0.9
        assert result.confidence < REVIEW_THRESHOLD


# ===========================================================================
# Anchors and evidence tables
# ===========================================================================

class TestAnchors:
    def test_every_vocabulary_in_circulation_maps_somewhere(self):
        from app.categorization.dual_taxonomy import PURPOSES
        from app.categorization.taxonomy import CATEGORIES

        unmapped = [name for name in list(PURPOSES) + list(CATEGORIES)
                    if anchor_path(name) is None]
        # `Other` is legitimately unmappable — it is the absence of an answer,
        # and it appears in both vocabularies, hence the set.
        assert set(unmapped) == {"Other"}, unmapped

    def test_a_rail_named_category_is_deliberately_not_mapped(self):
        """`NEFT Transfer` says how the money travelled, not what for. Guessing
        a purpose from it is what put 853 unrelated rows in one bucket."""
        for name in ("NEFT Transfer", "IMPS Transfer", "UPI Transfer", "RTGS Transfer"):
            assert anchor_path(name) is None

    def test_running_the_classifier_over_its_own_output_is_stable(self):
        for root in H.ROOTS:
            assert anchor_path(root) == (root,)

    def test_every_path_in_the_evidence_tables_exists_in_the_tree(self):
        """A typo here becomes a category the API renders, the database has no
        row for, and nobody can drill into."""
        for table in (M.CONCEPT_EVIDENCE, M.MERCHANT_EVIDENCE, M.MARKETPLACE_EVIDENCE):
            for ev in table:
                assert H.is_valid_path(ev.path), ev.path
        for _pattern, path in M.PRODUCT_HINTS:
            assert H.is_valid_path(path), path

    def test_organisation_detection_separates_a_company_from_a_person(self):
        assert looks_like_organisation("MEYER ORGANICS PVT LTD")
        assert looks_like_organisation("ACME TRADERS")
        assert not looks_like_organisation("RAHUL SHARMA")


# ===========================================================================
# Account types
# ===========================================================================

class TestAccountTypes:
    """The taxonomy must not be built around one kind of customer.

    A student's savings account and a manufacturer's current account are
    classified against the same tree; only the branches used differ.
    """

    PERSONAL = [
        ("UPI/DR/1234/SWIGGY/YESB", "debit", H.FOOD_DINING),
        ("NETFLIX SUBSCRIPTION AUG", "debit", H.ENTERTAINMENT),
        ("ATM WDL 500", "debit", H.CASH),
        ("NEFT SALARY AUG", "credit", H.INCOME),
        ("SIP HDFC MUTUAL FUND", "debit", H.INVESTMENTS),
    ]

    BUSINESS = [
        ("GST PAYMENT GSTN", "debit", H.TAXES_GOVERNMENT),
        ("NEFT-HDFCH-MEYER ORGANICS PVT LTD-HDFC", "debit", H.BUSINESS),
        ("AWS CLOUD SERVICES", "debit", H.BUSINESS),
        ("GOOGLE ADS INVOICE", "debit", H.BUSINESS),
        ("SMSCHG 05/24", "debit", H.FINANCIAL),
    ]

    @pytest.mark.parametrize("narration,direction,expected_root", PERSONAL + BUSINESS)
    def test_both_kinds_of_statement_resolve(self, narration, direction, expected_root):
        assert path_of(narration, direction=direction)[0] == expected_root

    def test_no_branch_of_the_tree_is_reachable_only_by_one_account_type(self):
        """Nothing in the classifier keys off account type except the one
        documented tie-break, so this is a guard against that changing."""
        import inspect

        from app.categorization import deep
        source = inspect.getsource(deep)
        assert source.count("account_type") <= 12, (
            "account_type is being consulted in more places than the single "
            "documented tie-break; the taxonomy is drifting toward being "
            "per-customer-type"
        )
