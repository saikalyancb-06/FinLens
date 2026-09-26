"""Reading the trade out of a counterparty's name.

WHY THIS EXISTS. On a real 1,823-row restaurant statement the review queue asked
about **73 counterparties**. A person answering 73 questions to file one month is
a worse deal than doing it by hand, and this system exists to remove that work,
not relocate it. Most of the 73 were trades: fish, vegetables, chicken, a
garage, a packaging supplier. Their names said what they sell.

THE LINE THIS DRAWS, and it is a narrow one. Everywhere else this codebase
refuses to infer a purpose from a counterparty — `ABC PVT LTD` must never become
"Raw Materials", and there are tests above pinning that. This module is not an
exception to that rule, it is the other half of it:

    a name that NAMES A TRADE is evidence          KUMAR FISH, MARUTHI MOTORS
    a name that is only a name is not              ABC PVT LTD, JAYAKUMAR

So the suite below is symmetrical. Every test that says "this trade resolves"
has a partner saying "this non-trade still goes to a human", because a rule that
fires too widely is worse than no rule: it replaces 73 questions with 73 wrong
answers nobody was asked to check.

No database, no model, no fixtures.
"""

import pytest

from app.categorization import hierarchy as H
from app.categorization import trades as T
from app.categorization.deep import classify_deep
from app.categorization.dual_taxonomy import (
    COST_OF_GOODS, PROFESSIONAL_FEES, SALES_INCOME,
)
from app.categorization.hybrid import (
    classify_transaction, memory_should_override,
)


# ===========================================================================
# The keyword table
# ===========================================================================

class TestTradeRecognition:
    @pytest.mark.parametrize("name,expected_word", [
        ("KUMAR FISH", "FISH"),
        ("SRI LAKSHMI CHICKEN CENTRE", "CHICKEN"),
        ("BALAJI CHIKEN SUPPLY", "CHIKEN"),          # the misspelling users type
        ("ANNAPURNA VEGETABLES", "VEGETABLES"),
        ("FRESH VEG MANDI", "VEG"),
        ("SRI DAIRY PRODUCTS", "DAIRY"),
        ("MARUTHI MOTORS", "MARUTHI"),
        ("SHREE TYRES", "TYRES"),
        ("APOLLO MEDICALS", "MEDICALS"),
        ("KRISHNA CEMENTS", "CEMENTS"),
        ("SUNRISE PACKAGING", "PACKAGING"),
        ("VENKAT TRANSPORTS", "TRANSPORTS"),
        ("SRI SAI PRINTERS", "PRINTERS"),
        ("GLOBAL INFOTECH", "INFOTECH"),
        ("LITTLE FLOWER SCHOOL", "SCHOOL"),
    ])
    def test_a_name_that_states_a_trade_is_read(self, name, expected_word):
        hit = T.match(name, direction="debit")
        assert hit is not None, f"{name} was not recognised"
        assert hit.matched_word == expected_word

    @pytest.mark.parametrize("name", [
        "ABC PVT LTD",
        "SRI VENKATESWARA ENTERPRISES",
        "ACME TRADERS",
        "JAYAKUMAR",
        "RAHUL SHARMA",
        "M/S GOPAL AND SONS",
        "XYZ INDUSTRIES",
    ])
    def test_a_name_that_is_only_a_name_is_not_read(self, name):
        """The other half of the rule. These have to keep reaching a human —
        a rule that fires here replaces 73 questions with 73 wrong answers."""
        assert T.match(name, direction="debit") is None

    def test_corporate_boilerplate_is_recognised_as_saying_nothing(self):
        assert T.names_only_a_company("ABC PVT LTD")
        assert T.names_only_a_company("SRI VENKATESWARA ENTERPRISES")
        assert not T.names_only_a_company("KUMAR FISH")

    def test_the_matched_word_is_reported_so_a_wrong_answer_is_visible(self):
        hit = T.match("NEFT-HDFCH2508-KUMAR FISH-HDFC BANK", direction="debit")
        assert "FISH" in hit.explanation
        assert "supplier" in hit.explanation

    def test_a_trade_word_must_be_a_whole_word(self):
        """`VEG` inside `VEGA SOLUTIONS` is not a vegetable supplier, and
        `MEAT` inside a longer word is not a butcher."""
        hit = T.match("VEGAS ENTERTAINMENT PVT LTD", direction="debit")
        assert hit is None or hit.matched_word != "VEG"


# ===========================================================================
# Direction decides which side of the trade you are on
# ===========================================================================

class TestDirection:
    def test_money_out_to_a_supplier_is_a_purchase(self):
        hit = T.match("KUMAR FISH", direction="debit")
        assert hit.flat_purpose == COST_OF_GOODS

    def test_money_in_from_the_same_party_is_a_sale(self):
        """Same keyword, opposite books. The direction column settles it
        without anyone guessing."""
        hit = T.match("KUMAR FISH", direction="credit")
        assert hit.flat_purpose == SALES_INCOME
        assert hit.path == (H.INCOME, "Business Revenue")


# ===========================================================================
# Account type decides whose books these are
# ===========================================================================

class TestAccountType:
    def test_a_business_account_buys_inventory(self):
        hit = T.match("KUMAR FISH", direction="debit", account_type="current")
        assert hit.path == (H.BUSINESS, "Inventory")

    def test_a_personal_account_buys_groceries(self):
        """A restaurant's fish supplier and a household's fishmonger are the
        same shop and different lines in the books."""
        hit = T.match("KUMAR FISH", direction="debit", account_type="savings")
        assert hit.path == (H.FOOD_DINING, "Groceries")

    def test_a_trade_that_means_the_same_either_way_does_not_move(self):
        for acct in ("current", "savings", None):
            hit = T.match("APOLLO MEDICALS", direction="debit", account_type=acct)
            assert hit.path[0] == H.HEALTHCARE


# ===========================================================================
# What the rest of the system does with it
# ===========================================================================

class TestIntegration:
    def test_a_trade_name_is_categorised_without_asking_the_user(self):
        """The point of the exercise. A provisional answer still puts the row
        in front of a person, and confirming 73 trade names one at a time is
        the work this is meant to remove."""
        result = classify_transaction("NEFT-HDFCH2508-KUMAR FISH-HDFC BANK",
                                      amount=8900.0, direction="DEBIT",
                                      account_type="current")
        assert result.category == COST_OF_GOODS
        assert result.requires_review is False
        assert result.classification_rule == "trade_name:FISH"
        assert "FISH" in result.explanation

    def test_a_name_that_says_nothing_still_goes_to_review(self):
        result = classify_transaction("NEFT-CITIN25081234-ABC PVT LTD",
                                      amount=8900.0, direction="DEBIT",
                                      account_type="current")
        assert result.requires_review is True

    def test_a_rule_matching_the_narration_itself_still_wins(self):
        """`GST PAYMENT TO SOMETHING FOODS` is a tax payment. What the
        narration says about the transaction beats what the payee's name says
        about the payee."""
        result = classify_transaction("GST PAYMENT GSTN 27AAAA FOODS",
                                      amount=15000.0, direction="DEBIT",
                                      account_type="current")
        assert result.classification_rule != "trade_name:FOODS"

    def test_the_hierarchy_places_the_supplier_by_name(self):
        result = classify_deep("NEFT-HDFCH2508-KUMAR FISH-HDFC BANK",
                               direction="debit", account_type="current")
        assert result.path[:2] == (H.BUSINESS, "Inventory")
        assert result.source == "trade_name"
        assert result.needs_review is False

    def test_an_unreadable_company_still_lands_in_the_honest_bucket(self):
        result = classify_deep("NEFT-CITIN25081234-ABC PVT LTD", direction="debit")
        assert result.path[:2] == H.VENDOR_UNKNOWN
        assert result.needs_review is True

    def test_a_recognised_merchant_outranks_a_trade_word(self):
        """`ZOMATO` is a specific company we know; a generic food word is not.
        The stronger evidence has to win or the tree gets shallower."""
        result = classify_deep("UPI/DR/1/ZOMATO ONLINE FOODS/YESB", direction="debit")
        assert result.path == (H.FOOD_DINING, "Food Delivery", "Zomato")


# ===========================================================================
# The number that matters
# ===========================================================================

def test_a_realistic_supplier_list_mostly_stops_needing_a_human():
    """The whole justification, as a number.

    These are the shapes that filled the review queue on the real statement.
    If most of them still needed a decision, this module would not be worth its
    own file.
    """
    suppliers = [
        "NEFT-HDFCH25-KUMAR FISH-HDFC BANK",
        "NEFT-HDFCH25-SRI LAKSHMI VEGETABLES",
        "IMPS/P2A/512345/BALAJI CHIKEN CENTRE",
        "NEFT-ICICI25-ANNAPURNA DAIRY",
        "NEFT-SBIN25-VENKAT TRANSPORTS",
        "NEFT-AXIS25-SUNRISE PACKAGING",
        "IMPS/P2A/9912/MARUTHI MOTORS",
        "NEFT-HDFCH25-APOLLO MEDICALS",
        "NEFT-HDFCH25-SRI SAI PRINTERS",
        "NEFT-KOTAK25-KRISHNA CEMENTS",
        "NEFT-HDFCH25-GLOBAL INFOTECH SOLUTIONS",
        "NEFT-HDFCH25-RELIABLE SECURITY SERVICES",
        # ...and the ones that genuinely cannot be read.
        "NEFT-CITIN2508-ABC PVT LTD",
        "IMPS/P2A/8891/JAYAKUMAR",
        "NEFT-HDFCH25-SRI VENKATESWARA ENTERPRISES",
    ]
    results = [classify_transaction(n, amount=5000.0, direction="DEBIT",
                                    account_type="current")
               for n in suppliers]
    still_asking = [n for n, r in zip(suppliers, results) if r.requires_review]

    assert len(still_asking) == 3, still_asking
    # And the three that remain are the three that should: nothing in those
    # names says what the money was for.
    assert all(any(k in n for k in ("ABC PVT LTD", "JAYAKUMAR", "VENKATESWARA"))
               for n in still_asking)


def test_every_trade_path_exists_in_the_tree():
    """A typo here becomes a category the API renders and nobody can drill into."""
    for trade in T.TRADES:
        assert H.is_valid_path(trade.path), trade.path
        assert H.is_valid_path(trade.business_path), trade.business_path


# ===========================================================================
# A person's decision beats a keyword
# ===========================================================================

class TestMemoryOutranksTheTradeRule:
    """The regression the trade rule introduced, and the guard against it.

    The counterparty memory used to be consulted only when the classifier
    abstained — which was exactly when a saved decision was needed. Trade names
    answer confidently, so a user who had taught the system that "KUMAR FISH" is
    Professional Fees would have had that silently overruled on the very next
    upload. A keyword must never outrank a person who already answered.

    The condition lives in one function so both call sites share it and it can
    be tested without a database.
    """

    def test_an_abstention_is_overridable(self):
        assert memory_should_override(None, requires_review=True)

    def test_a_trade_guess_is_overridable(self):
        assert memory_should_override("trade_name:FISH", requires_review=False)

    def test_a_fact_about_the_transaction_is_not(self):
        """"GST PAYMENT" is a property of the row, not of who was paid.
        A counterparty decision must not reach it."""
        assert not memory_should_override("narration_pattern:GST payment",
                                          requires_review=False)
        assert not memory_should_override("exact_merchant:ZOMATO",
                                          requires_review=False)

    def test_a_trade_answer_carries_the_prefix_the_rule_keys_off(self):
        """Load-bearing: both call sites recognise a trade answer by this."""
        result = classify_transaction("NEFT-HDFCH25-KUMAR FISH", amount=100.0,
                                      direction="DEBIT", account_type="current")
        assert result.classification_rule.startswith("trade_name:")
        assert memory_should_override(result.classification_rule,
                                      result.requires_review)
