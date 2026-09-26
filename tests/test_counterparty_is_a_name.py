"""216 of the 221 questions were clock times.

WHAT THE DIAGNOSTIC FOUND. `scripts/queue_vs_categories.py` on a real 569-row
statement reported 221 counterparties to decide — against a hard ceiling of 50 —
and 216 of them had been seen exactly once. The top of the list explained why:

     36 rows  ₹  430,672.00  Resilientinnova
     32 rows  ₹1,591,416.00  Bharatpe 92293
     24 rows  ₹  865,080.00  Bharatpe
     15 rows  ₹  666,495.00  Resilient Innovations Privat
      1 rows  ₹  184,819.00  07:22:38
      1 rows  ₹  162,629.00  08:56:10
      1 rows  ₹  159,392.00  08:05:03
      ...

Four separate defects, all of them upstream of the queue:

1. TIMESTAMPS BECAME PARTIES. A channel pattern matched, handed `07:22:38` over
   as the payee segment, and `_normalise` turned it into the respectable-looking
   key `07 22 38`. The bare-narration branch of `extract` already required a
   real word before it would key on anything; the channel branches did not.

2. REFERENCE DIGITS SPLIT ONE PARTY IN TWO. `BHARATPE 92293449100000` and
   `BHARATPE` were 32 rows and 24 rows, asked separately.

3. A BANK CODE DID THE SAME. `BHARATPE YESB` is `BHARATPE` with an IFSC prefix
   stuck on the end.

4. A TRUNCATED EXPORT SPLIT A COMPANY. `RESILIENTINNOVA` and `RESILIENT
   INNOVATIONS PRIVAT` are one firm cut at two different widths — 51 rows, two
   questions.

Every test below is paired with one that says the rule does NOT fire, because a
grouping rule that fires too widely is worse than none: it replaces 221
questions with 221 wrong merges nobody was asked to check.

No database.
"""
import pytest

from app.categorization.counterparty import extract, group_for
from app.categorization.deep import classify_deep


# ===========================================================================
# 1. A counterparty is a NAME
# ===========================================================================

class TestAKeyMustContainAName:
    @pytest.mark.parametrize("narration", [
        "EBANK:W1B/07:22:38/522334",
        "MPS/IMPS/5223/08:56:10",
        "NEFT-XXXX-00:09:30",
    ])
    def test_a_clock_time_is_never_a_counterparty(self, narration):
        """The 216-question bug. Whatever else these rows are, nobody was paid
        `07:22:38`."""
        cp = extract(narration)
        assert cp is None or "07" not in cp.key
        grp = group_for(narration)
        assert grp is None or ":" not in grp.display

    @pytest.mark.parametrize("narration", [
        "NEFT-XXXX-92293449100000",
        "IMPS/P2A/5223/8891234567",
    ])
    def test_a_bare_reference_number_is_never_a_counterparty(self, narration):
        cp = extract(narration)
        assert cp is None or any(c.isalpha() for c in cp.key)

    @pytest.mark.parametrize("narration,expected", [
        ("NEFT-HDFCH25-KUMAR FISH-HDFC BANK", "KUMAR FISH"),
        ("IMPS/P2A/8891/JAYAKUMAR", "JAYAKUMAR"),
    ])
    def test_a_real_name_still_comes_through(self, narration, expected):
        """The other half. A guard that also drops real payees has not helped
        anyone."""
        cp = extract(narration)
        assert cp is not None and expected in cp.key


# ===========================================================================
# 2 & 3. One party, one question
# ===========================================================================

class TestOnePartyIsOneQuestion:
    def test_a_trailing_reference_number_does_not_split_a_party(self):
        a = group_for("UPI/CR/522334455/BHARATPE 92293449100000/YESB")
        b = group_for("IMPS-522334455-BHARATPE-YESB")
        assert a.key == b.key == "BHARATPE"

    def test_the_display_drops_the_reference_too(self):
        """Merging the group is not enough if the screen still shows
        `Bharatpe 92293449100000` next to `Bharatpe` — it reads as two parties
        that were wrongly combined."""
        assert group_for(
            "UPI/CR/522334455/BHARATPE 92293449100000/YESB").display == "Bharatpe"

    def test_an_ifsc_code_is_not_part_of_the_name(self):
        assert group_for("NEFT-XX-MEYER ORGANICS PVT LTD-YESB").key == "MEYER ORGANICS"

    def test_a_name_that_merely_ends_in_four_letters_survives(self):
        """`SBIN` is a bank code; `KUMAR FISH` ends in a four-letter word and is
        a fishmonger. Only a whole trailing token from the code list goes."""
        assert "FISH" in group_for("NEFT-HDFCH25-KUMAR FISH").key


# ===========================================================================
# 4. Fixed-width exports cut names in half
# ===========================================================================

class TestTruncatedNames:
    def test_two_cuts_of_one_company_are_one_group(self):
        a = group_for("NEFT-XX-RESILIENTINNOVA")
        b = group_for("NEFT-XX-RESILIENT INNOVATIONS PRIVAT")
        assert a.group_key == b.group_key

    def test_two_different_companies_are_not(self):
        a = group_for("NEFT-XX-KUMAR FISH")
        b = group_for("NEFT-XX-KUMAR FISHERIES EXPORTS")
        assert a.group_key != b.group_key

    def test_two_different_people_are_not(self):
        a = group_for("IMPS/P2A/1/JAYAKUMAR")
        b = group_for("IMPS/P2A/2/RAJKUMAR")
        assert a.group_key != b.group_key


# ===========================================================================
# The acquirer that should never have been a question
# ===========================================================================

class TestAcquirerSettlements:
    @pytest.mark.parametrize("narration", [
        "NEFT-YESB-BHARATPE",
        "UPI/CR/1/BHARATPE 92293449100000/YESB",
        "NEFT-YESB-PINE LABS",
        "NEFT-YESB-RAZORPAY SOFTWARE",
    ])
    def test_money_in_from_an_acquirer_is_the_day_s_takings(self, narration):
        """A settlement is the card and UPI sales being paid out. That is a
        fact about the direction, not a guess about the business — and it is
        the difference between an answer and a question. BharatPe alone was 56
        rows across two spellings."""
        result = classify_deep(narration, direction="credit",
                               account_type="current")
        assert result.path[:2] == ("Income", "Business Revenue")
        assert result.needs_review is False

    def test_money_out_through_the_same_rail_says_nothing(self):
        """DELIBERATELY no matching outbound rule. Paying THROUGH PhonePe tells
        you the rail and nothing about what was bought; reading it as revenue
        because the word appears would be the aggregator bug in reverse."""
        result = classify_deep("UPI/DR/1/PHONEPE/YESB", direction="debit",
                               account_type="current")
        assert result.path[:2] != ("Income", "Business Revenue")
