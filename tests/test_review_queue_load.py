"""How many questions the review queue asks, and why it asked too many.

Measured on a real 1,823-row statement: **73 counterparties** waiting on a
human. Reading trade names out of counterparties took it to 50. This file is
about the other two causes, both visible in that run's output and neither of
them about categories at all.

CAUSE 1 — FACTS WERE BEING TREATED AS GUESSES.

    273 rows  CHARGES FOR PORD CUSTOMER PAYMENT
     35 rows  P05RENT_MAR25_T1D_34016239
     12 rows  74460500000024:1NT.C011:01-04-2025 TO 30-04
     12 rows  BY CASH
      4 rows  LEDGER FOLIO CHARGES - CC/OD

Every one of those was recognised, categorised correctly, and then queued for
a human to confirm. "LEDGER FOLIO CHARGES - CC/OD is a bank charge" is not a
judgement anyone can improve on. The narration patterns now carry a `certain`
flag: a fact about the transaction is written and not queued; an inference
about a counterparty ("PHONEPE settled money in, so this is probably revenue")
still is.

CAUSE 2 — OCR DAMAGE WAS SPLITTING ONE PARTY INTO SEVERAL.

    EBANK:W1B/1461198140/5U5HM1THA H SHETTY
    EBANK:W1B/1456922475/SUSHMITHA  H SHETTY

Two groups, one person, two questions. And on the supplier side one poultry
supplier appeared NINE times — `NARA MHA AH CHIKEN`, `NAR MHA CHIKEN`,
`NARASIMHA CHIKEN` and so on — because the key was built from the damaged text.

The repair existed; it just ran too late and too timidly. It now runs before
the key is built, and its threshold dropped from three letters to two, because
every row on that statement was scaffolded `EBANK:W1B/<ref>/<name>` and `W1B`
is two letters and a digit. Unrepaired, it failed the channel pattern, so
every one of those rows was grouped by narration shape rather than by payee.
"""

import pytest

from app.categorization.counterparty import group_for
from app.categorization.purpose_rules import derive_result, ocr_correct


# ===========================================================================
# A fact is not a question
# ===========================================================================

class TestBankFactsAreCertain:
    @pytest.mark.parametrize("narration,note", [
        ("CHARGES FOR PORD CUSTOMER PAYMENT :002714096", "bank charge"),
        ("CHG CASH HANDLING FOR:11-03-2025", "bank charge"),
        ("LEDGER FOLIO CHARGES - CC/OD", "bank charge"),
        ("P05RENT_MAR25_T1D_34016239", "POS terminal rental"),
        ("74460500000024:1NT.C011:01-04-2025 TO 30-04-2025", "interest collected by bank"),
        ("74460500000024:PENA1 CHARGE C011:01-01-2026", "penal interest"),
        ("EBANK:1451815833//25041900037929/CBDT T1N20", "direct tax challan"),
        ("EBANK:W1B/1451818217/5H0BHA G GST", "GST payment"),
        ("BY CASH", "cash deposit"),
        ("TO CASH SELF", "cash withdrawal"),
        ("EBANK:5ELF/1450086102/0A5TAL 108 TO COASTAL", "self transfer"),
    ])
    def test_the_bank_describing_its_own_action_is_not_queued(self, narration, note):
        """These were 350+ rows of confirmations that could not change anything."""
        got = derive_result(narration)
        assert got is not None, f"not recognised at all: {narration}"
        assert got.note == note
        assert got.certain is True

    @pytest.mark.parametrize("narration", [
        "UPI/CR/12345/PH0NEPE/YESB",
        "NEFT-IN-RAZORPAY SOFTWARE",
        "UPI/CR/12345/SWIGGY/YESB",
    ])
    def test_an_inference_about_a_counterparty_is_still_queued(self, narration):
        """"PhonePe settled money in" is a fact; "so this row is revenue" is a
        judgement. The second one keeps its confirmation step.

        These names are ALSO consumer rails — you can pay Swiggy for dinner and
        Paytm for a phone bill — and `DERIVATION_RULES` carries no direction, so
        promoting them would label a Swiggy dinner as Sales Income with
        confidence. They stay provisional until the rule knows which way the
        money went.
        """
        got = derive_result(narration)
        assert got is not None
        assert got.certain is False

    @pytest.mark.parametrize("narration", [
        "BT25040124001012/ 5091357116/79707/B0BCARD L",
        "UPI/1/04:20:46/UPI/bharatpe.payout@yes",
        "NEFT-CITIN25-PLUXEE IND PVT LTD-ESCROW A-",
    ])
    def test_a_b2b_only_acquirer_does_not_need_confirming(self, narration):
        """MEASURED, not assumed. A merchant never PAYS BOBCARD, BharatPe or
        Pluxee — those names reach a statement only when money is being settled
        TO you, so unlike Swiggy there is no second reading to rule out.

        `scripts/eval_rule_engine.py` on the 2,862 labelled rows in `data.csv`
        found these rules right 605 times out of 612 — and all 612 were being
        sent to a human to confirm. Asking ~600 questions to hear "yes" 99% of
        the time is the largest single source of review work on a real
        statement. Promoting them took coverage 28.5% -> 56.7% and accuracy
        91.8% -> 95.4%.
        """
        got = derive_result(narration)
        assert got is not None
        assert got.certain is True


# ===========================================================================
# OCR damage, and the two places it had to be repaired
# ===========================================================================

class TestOcrRepair:
    @pytest.mark.parametrize("damaged,repaired", [
        ("5U5HM1THA", "SUSHMITHA"),
        ("JAYAPRAKA5H", "JAYAPRAKASH"),
        ("5HEKHAR", "SHEKHAR"),
        ("AZ1ZULLA", "AZIZULLA"),
        ("MARUTH1", "MARUTHI"),
        ("C0A5TAL", "COASTAL"),
        ("W1B", "WIB"),          # two letters: the case the old threshold missed
        ("1NT", "INT"),
        ("B0BCARD", "BOBCARD"),
    ])
    def test_a_damaged_word_is_repaired(self, damaged, repaired):
        assert ocr_correct(damaged) == repaired

    @pytest.mark.parametrize("identifier", [
        "AXNPN09200047279",
        "5091357116",
        "74460500000024",
        "BT25040124001012",
        "25041900037929",
    ])
    def test_an_identifier_is_left_alone(self, identifier):
        """Lowering the threshold to two letters must not start rewriting
        reference numbers — the digit ratio is what keeps them out."""
        assert ocr_correct(identifier) == identifier

    def test_the_same_person_written_two_ways_is_one_group(self):
        """Two groups, one person, two questions about someone already
        answered for."""
        a = group_for("EBANK:W1B/1461198140/5U5HM1THA H SHETTY")
        b = group_for("EBANK:W1B/1456922475/SUSHMITHA H SHETTY")
        assert a.key == b.key == "SUSHMITHA H SHETTY"

    def test_repair_happens_before_the_key_is_built(self):
        """The repair existed before this change; it ran after the key was
        made, which is the same as not running."""
        assert group_for("EBANK:W1B/1450084354/MARUTH1 PRO STORE").key \
            == "MARUTHI PRO STORE"

    def test_a_repaired_name_reaches_the_channel_pattern(self):
        """`EBANK:W1B/...` scaffolding: unrepaired, `W1B` fails the pattern's
        letter class and the row falls back to shape grouping — which is how
        every counterparty on that statement ended up grouped by narration
        text instead of by payee."""
        assert group_for("EBANK:W1B/1463826748/5HEKHAR").kind == "counterparty"

    def test_a_repaired_name_can_then_be_read_as_a_trade(self):
        from app.categorization import trades

        key = group_for("EBANK:W1B/1450084354/MARUTH1 PRO STORE").key
        assert trades.match(key, direction="debit") is not None


# ===========================================================================
# The number, end to end
# ===========================================================================

def test_the_shapes_that_filled_the_queue_no_longer_ask():
    """One row per group that was outstanding on the real statement.

    Counted as groups, not rows: a person answers once per group, and that is
    the number that decides whether they finish or give up.
    """
    from app.categorization.hybrid import classify_transaction

    outstanding = [
        # bank's own vocabulary — facts
        "CHARGES FOR PORD CUSTOMER PAYMENT :002714096",
        "P05RENT_MAR25_T1D_34016239",
        "EBANK:1451815833//25041900037929/CBDT T1N20",
        "74460500000024:1NT.C011:01-04-2025 TO 30-04-2025",
        "BY CASH",
        "CHG CASH HANDLING FOR:11-03-2025",
        "LEDGER FOLIO CHARGES - CC/OD",
        "TO CASH SELF",
        "74460500000024:PENA1 CHARGE C011:01-01-2026",
        "CHARGES FOR :1MP5/P2A/524516549790/XXXXXXXX",
        "EBANK:5ELF/1450086102/0A5TAL 108 TO COASTAL",
        "EBANK:5ELF/1452840005/C0ASTA1 TO COASTAL",
        "EBANK:W1B/1451818217/5H0BHA G GST",
        # trade names
        "EBANK:W1B/1450084354/MARUTH1 PRO STORE",
        # genuinely a person — must still ask
        "EBANK:W1B/1461198140/5U5HM1THA H SHETTY",
        "EBANK:W1B/1463826748/5HEKHAR",
    ]
    still_asking = [
        n for n in outstanding
        if classify_transaction(n, amount=1000.0, direction="DEBIT",
                                account_type="current").requires_review
    ]
    assert len(still_asking) == 2, still_asking
    assert all("SHETTY" in n or "HEKHAR" in n for n in still_asking)


# ===========================================================================
# One person, several spellings
# ===========================================================================

class TestSpellingVariants:
    """The last thing filling the queue after the OCR repair.

        Sushmitha H Shetty   12 rows
        Sushmita H Shetty     1 row      no 'h'
        Sushmitha Shetty      1 row      no middle initial
        Shekar Shetty         1 row
        Shekhar Shetty        1 row

    Five questions, two people. Transliteration wobbles in exactly two places —
    vowels, and an H after a consonant — and the consonant skeleton was already
    computed for all of them. It was just never used to group.
    """

    @pytest.mark.parametrize("variants", [
        ("SUSHMITHA H SHETTY", "SUSHMITA H SHETTY", "SUSHMITHA SHETTY"),
        ("SHEKAR SHETTY", "SHEKHAR SHETTY"),
        ("JAYAPRAKASH SHETTY", "JAYAPARAKSH SHETTY"),
        ("NARASIMHA CHIKEN", "NARASIMHAIAH CHIKEN"),
    ])
    def test_one_person_written_several_ways_is_one_question(self, variants):
        keys = {group_for(f"EBANK:W1B/1234567/{v}").group_key for v in variants}
        assert len(keys) == 1, keys

    def test_different_parties_stay_apart(self):
        a = group_for("EBANK:W1B/1/KUMAR FISH").group_key
        b = group_for("EBANK:W1B/2/MEYER ORGANICS PVT LTD").group_key
        c = group_for("EBANK:W1B/3/JAYAKUMAR").group_key
        assert len({a, b, c}) == 3

    def test_a_short_skeleton_falls_back_to_the_exact_key(self):
        """`ABC` reduces to two letters, which will collide with parties that
        have nothing to do with each other."""
        g = group_for("EBANK:W1B/1/ABC PVT LTD")
        assert g.group_key == g.key

    def test_the_exact_key_is_still_what_a_decision_is_stored_against(self):
        """Grouping may be looser than storage: the queue collapses a question,
        it does not rewrite what the user answered about."""
        g = group_for("EBANK:W1B/1/SUSHMITA H SHETTY")
        assert g.key == "SUSHMITA H SHETTY"
        assert g.group_key != g.key


class TestMemoryReachesTheVariants:
    """Grouping alone is not enough.

    Collapsing the question is worth one upload. If the saved decision only
    matches the exact spelling, the next statement spells it differently and
    asks again — which is the user's actual complaint, one upload later.

    Fake rows rather than a fixture: the behaviour under test is the indexing
    in `load_memory`, not the table it read from.
    """

    @staticmethod
    def _memory(*rows):
        from types import SimpleNamespace

        from app.categorization import counterparty_memory as M

        class _Query:
            def __init__(self, r): self.r = r
            def filter(self, *a, **k): return self
            def all(self): return self.r

        class _DB:
            def __init__(self, r): self.r = r
            def query(self, *a, **k): return _Query(self.r)

        made = [SimpleNamespace(counterparty_key=k, display_name=d, category=c,
                                event_type=None, times_confirmed=n,
                                kind="counterparty")
                for k, d, c, n in rows]
        return M.load_memory(_DB(made), "user"), M

    @pytest.mark.parametrize("narration", [
        "EBANK:W1B/1/SUSHMITHA H SHETTY",
        "EBANK:W1B/2/SUSHMITA H SHETTY",
        "EBANK:W1B/3/SUSHMITHA SHETTY",
        "EBANK:W1B/4/5U5HM1THA H SHETTY",      # and through OCR damage
    ])
    def test_one_decision_covers_every_spelling(self, narration):
        memory, M = self._memory(
            ("SUSHMITHA H SHETTY", "Sushmitha H Shetty", "Salary & Wages", 3))
        hit = M.lookup(memory, narration)
        assert hit is not None and hit.category == "Salary & Wages"

    def test_an_unrelated_party_is_not_swept_in(self):
        memory, M = self._memory(
            ("SUSHMITHA H SHETTY", "Sushmitha H Shetty", "Salary & Wages", 3))
        assert M.lookup(memory, "EBANK:W1B/9/RAMESH KUMAR") is None

    def test_conflicting_decisions_disable_the_skeleton(self):
        """`SURESH KUMAR` and `SIRISH KUMAR` reduce to the same skeleton. If
        the user filed them differently they are two parties, and guessing
        which one a third spelling meant is worse than asking."""
        memory, M = self._memory(
            ("SURESH KUMAR", "Suresh Kumar", "Salary & Wages", 1),
            ("SIRISH KUMAR", "Sirish Kumar", "Cost of Goods", 1),
        )
        # Each exact spelling still resolves to what the user said.
        assert M.lookup(memory, "EBANK:W1B/1/SURESH KUMAR").category == "Salary & Wages"
        assert M.lookup(memory, "EBANK:W1B/2/SIRISH KUMAR").category == "Cost of Goods"
        # A third spelling gets no answer rather than a coin toss.
        assert M.lookup(memory, "EBANK:W1B/3/SURESHH KUMAR") is None
