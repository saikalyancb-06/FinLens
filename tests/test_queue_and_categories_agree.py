"""The two screens must not disagree about how much work is left.

THE BUG THIS PINS. The Categories page reported **221 counterparties to
decide**. The Review Queue, same user, same second, reported **0** and rendered
"No counterparties are waiting on a decision." Neither screen was broken on its
own terms — they were reading different columns:

    Review Queue  ->  predictions.requires_review, transactions.category_id
    Categories    ->  transactions.category_confidence

A transaction is classified twice in this system, once flat and once into the
tree, and nothing forced the two results to agree. So a row the flat engine
answered confidently vanished from the queue while the tree still had no purpose
for it, and the user was told to go do work on a screen with nothing on it.

The predicate now lives in one module. These tests are the thing that keeps it
there: they fail if either screen grows its own opinion again.

No database. Fake row objects only — the point is the RULE, and a rule that
needs a fixture to state is a rule nobody will check.
"""
import pytest

from app.categorization.decisions import (
    REVIEW_THRESHOLD, decision_weight, needs_decision, purpose_is_established,
)


class Row:
    """The columns the predicate reads, and nothing else."""
    def __init__(self, **kw):
        self.category = kw.get("category")
        self.category_path = kw.get("category_path")
        self.category_confidence = kw.get("category_confidence")
        self.category_id = kw.get("category_id", 1)
        self.legacy_category = kw.get("legacy_category")


class Pred:
    def __init__(self, requires_review=False, predicted_category="Cost of Goods"):
        self.requires_review = requires_review
        self.predicted_category = predicted_category


# ===========================================================================
# What "settled" means
# ===========================================================================

class TestPurposeIsEstablished:
    def test_a_confident_tree_answer_is_settled(self):
        assert purpose_is_established(Row(category="Food & Dining",
                                          category_confidence=0.88))

    def test_a_residual_placement_is_not(self):
        """`Transfers > External Transfer` is TRUE of the row and is not a
        purpose. The confidence is what carries that distinction, which is why
        the residual placements sit below the threshold on purpose."""
        assert not purpose_is_established(Row(category="Transfers",
                                              category_confidence=0.30))

    def test_the_boundary_is_the_review_threshold(self):
        assert purpose_is_established(Row(category="X",
                                          category_confidence=REVIEW_THRESHOLD))
        assert not purpose_is_established(
            Row(category="X", category_confidence=REVIEW_THRESHOLD - 0.01))

    def test_a_legacy_row_with_no_confidence_is_judged_the_old_way(self):
        """Rows ingested before the tree existed have no confidence recorded.
        Treating a missing number as zero would drag every one of them into the
        queue on the first deploy."""
        assert purpose_is_established(Row(legacy_category="Cost of Goods",
                                          category_confidence=None))
        assert not purpose_is_established(Row(category_confidence=None))


# ===========================================================================
# The union, which is the whole fix
# ===========================================================================

class TestNeedsDecision:
    def test_a_settled_row_asks_nobody(self):
        assert not needs_decision(Row(category="Food & Dining",
                                      category_confidence=0.88),
                                  Pred())

    def test_the_tree_being_unsure_is_enough_on_its_own(self):
        """THE REGRESSION. The flat engine is content — no review flag, a real
        category_id — and the tree still cannot say what the money was for. This
        is the row that showed on Categories and not in the queue."""
        row = Row(category="Transfers", category_confidence=0.30, category_id=7)
        assert needs_decision(row, Pred(requires_review=False))

    def test_the_flat_engine_being_unsure_is_also_enough(self):
        """And the mirror image, which is the failure pointing the other way:
        an intersection rule would have silenced this one instead."""
        row = Row(category="Food & Dining", category_confidence=0.95)
        assert needs_decision(row, Pred(requires_review=True))

    def test_the_flat_sentinel_still_counts(self):
        row = Row(category="Food & Dining", category_confidence=0.95)
        assert needs_decision(row, Pred(predicted_category="Uncategorized"))

    def test_a_missing_category_id_counts(self):
        row = Row(category="Food & Dining", category_confidence=0.95,
                  category_id=None)
        assert needs_decision(row, None)

    def test_a_missing_prediction_does_not_silence_the_tree(self):
        """Not every caller joins the prediction, and an outer join produces
        NULL rather than false. The tree half has to stand alone."""
        assert needs_decision(Row(category="Transfers",
                                  category_confidence=0.30), None)


# ===========================================================================
# 221 questions is not an answer
# ===========================================================================

class TestRanking:
    def test_clearing_more_rows_outranks_moving_more_money(self):
        """A party seen forty times is worth more than one large payment: the
        decision is remembered, so it also settles every future statement."""
        many = decision_weight(40, 5_00_000)
        one_big = decision_weight(1, 9_00_00_000)
        assert many > one_big

    def test_money_breaks_a_tie_between_equal_counts(self):
        assert decision_weight(3, 500_000) > decision_weight(3, 100_000)

    def test_a_party_seen_once_ranks_last(self):
        """The long tail is what took the queue to 221. These are left at the
        placement the classifier could defend rather than turned into a
        question that clears exactly one row."""
        ranked = sorted([decision_weight(1, 10_000),
                         decision_weight(2, 100),
                         decision_weight(9, 1)], reverse=True)
        assert ranked[-1] == decision_weight(1, 10_000)


# ===========================================================================
# Nobody gets to grow their own opinion again
# ===========================================================================

def test_both_screens_import_the_shared_predicate():
    """A grep, as a test. If either screen goes back to spelling the condition
    out against its own columns, the two numbers drift apart again and the user
    is the one who finds out."""
    import inspect

    from app.api import categories as cat_api
    from app.api import review_queue as rq_api

    assert "needs_decision_clause" in inspect.getsource(rq_api._review_query)
    assert "needs_decision" in inspect.getsource(cat_api.summary)
