"""STAGES 4-7: how alike are two normalised names?

Every function here returns a score in [0, 1] and NONE of them decides
anything. The resolver combines them, because any single measure has a failure
mode that another one covers:

    character distance   sees CHETAN/CHETHAN, blind to CHETHAN B M / B M CHETAN
    token sets           sees the reordering, blind to the spelling
    phonetics            sees EUGIN/EUGINE, and also collides ANAND/AHMED
    concatenation        sees RAMACHANDRAKOTHARI, needs the pieces to exist

The brief was explicit that a single fuzzy threshold is not acceptable, and
this is why: `similarity > 0.80` is a coin toss on transliterated Indian names,
where two spellings of one person can score 0.72 and two different people can
score 0.85.

No third-party dependencies. Levenshtein, Jaro-Winkler and the rest are short
enough to own, and owning them means the behaviour cannot change under us on a
`pip install`.
"""
from __future__ import annotations

from functools import lru_cache
from typing import FrozenSet, List, Optional, Sequence, Set, Tuple

from app.entity_resolution.normalize import Representations, indic_phonetic

__all__ = [
    "indic_phonetic_equal",
    "levenshtein", "edit_ratio", "jaro", "jaro_winkler", "ngram_dice",
    "token_set_ratio", "token_sort_ratio", "token_containment",
    "initials_match", "concatenation_score", "person_name_score",
    "best_token_alignment",
]


# ---------------------------------------------------------------------------
# STAGE 5 — character-level
# ---------------------------------------------------------------------------

@lru_cache(maxsize=200_000)
def levenshtein(a: str, b: str) -> int:
    """Edit distance, two rows of memory rather than a full matrix."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,            # deletion
                current[j - 1] + 1,         # insertion
                previous[j - 1] + (ca != cb),
            ))
        previous = current
    return previous[-1]


def edit_ratio(a: str, b: str) -> float:
    """Edit distance normalised by the longer string. 1.0 is identical."""
    if not a and not b:
        return 1.0
    longest = max(len(a), len(b))
    if not longest:
        return 1.0
    return 1.0 - levenshtein(a, b) / longest


@lru_cache(maxsize=200_000)
def jaro(a: str, b: str) -> float:
    """Jaro similarity. Rewards matching characters near the same position."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    window = max(len(a), len(b)) // 2 - 1
    if window < 0:
        window = 0
    a_flags = [False] * len(a)
    b_flags = [False] * len(b)
    matches = 0
    for i, ca in enumerate(a):
        start = max(0, i - window)
        end = min(i + window + 1, len(b))
        for j in range(start, end):
            if not b_flags[j] and b[j] == ca:
                a_flags[i] = b_flags[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    transpositions = 0
    k = 0
    for i, flagged in enumerate(a_flags):
        if not flagged:
            continue
        while not b_flags[k]:
            k += 1
        if a[i] != b[k]:
            transpositions += 1
        k += 1
    transpositions //= 2
    return (matches / len(a) + matches / len(b)
            + (matches - transpositions) / matches) / 3.0


def jaro_winkler(a: str, b: str, *, prefix_weight: float = 0.1) -> float:
    """Jaro, weighted towards a shared prefix.

    Chosen over plain Jaro because names diverge at the END far more often than
    at the start — truncation, plurals, suffixes — while two different names
    usually differ in the first letters.
    """
    base = jaro(a, b)
    if base < 0.7:
        return base
    prefix = 0
    for ca, cb in zip(a[:4], b[:4]):
        if ca != cb:
            break
        prefix += 1
    return base + prefix * prefix_weight * (1 - base)


def ngram_dice(a: FrozenSet[str], b: FrozenSet[str]) -> float:
    """Dice coefficient over character n-grams. Order-tolerant overlap."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


# ---------------------------------------------------------------------------
# STAGE 4 — token-level
# ---------------------------------------------------------------------------

def token_set_ratio(a: Sequence[str], b: Sequence[str]) -> float:
    """Jaccard over token sets. Immune to order and to repeated words."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def token_sort_ratio(a: Sequence[str], b: Sequence[str]) -> float:
    """Character similarity after sorting the tokens.

    This is what makes `B M CHETAN` and `CHETAN B M` comparable: sorted, both
    become `B CHETAN M`, and the remaining difference is spelling only.
    """
    return jaro_winkler(" ".join(sorted(a)), " ".join(sorted(b)))


def token_containment(a: Sequence[str], b: Sequence[str]) -> float:
    """How much of the SHORTER name is present in the longer one.

    Asymmetric on purpose. `RESILIENT INNOVATIONS` inside `RESILIENT
    INNOVATIONS INDIA` scores 1.0 here — which is a strong signal they are
    related and, deliberately, not a decision that they are the same. Stage 14
    treats an extra distinguishing word as a conflict.
    """
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return len(shorter & longer) / len(shorter)


def initials_match(a: Representations, b: Representations) -> float:
    """Do the initials agree, in any order?

    Weak alone — `RK` collides constantly — so it is scored low and only ever
    contributes alongside something else.
    """
    ia, ib = a.initials, b.initials
    if not ia or not ib:
        return 0.0
    if ia == ib:
        return 1.0
    if sorted(ia) == sorted(ib):
        return 0.9
    shorter, longer = sorted((ia, ib), key=len)
    if shorter and set(shorter) <= set(longer):
        return 0.6
    return 0.0


def best_token_alignment(a: Sequence[str], b: Sequence[str]) -> float:
    """Greedy best-match pairing of tokens, scored by character similarity.

    Handles the case token sets cannot: `MANOJ NATH GOSWAMI` vs `MANOJ N
    GOSWAMI`, where one token is an INITIAL of the other. An exact-set measure
    scores that 0.5; aligning the tokens scores it near 1.
    """
    if not a or not b:
        return 0.0
    remaining = list(b)
    total = 0.0
    for token in a:
        if not remaining:
            break
        best_i, best_v = 0, -1.0
        for i, other in enumerate(remaining):
            if len(token) == 1 or len(other) == 1:
                # An initial matches the word it abbreviates, and nothing else.
                v = 1.0 if token[0] == other[0] else 0.0
            else:
                v = max(jaro_winkler(token, other),
                        1.0 if indic_phonetic(token) == indic_phonetic(other) else 0.0)
            if v > best_v:
                best_i, best_v = i, v
        total += max(best_v, 0.0)
        remaining.pop(best_i)
    return total / max(len(a), len(b))


# ---------------------------------------------------------------------------
# STAGE 7 — concatenated names
# ---------------------------------------------------------------------------

def concatenation_score(a: Representations, b: Representations) -> float:
    """Is one of these the other with the spaces removed?

        RAMACHANDRAKOTHARI   vs  RAMACHANDRA KOTHARI
        MANOJNATHGOSWAMI     vs  MANOJ NATH GOSWAMI

    Answered by asking whether the multi-token side is a valid SEGMENTATION of
    the single-token side, rather than by guessing where to split a long string.
    Splitting blind invents words; checking a proposed split only confirms one.

    A tolerance of one edit per token is allowed so an OCR error or a dropped
    vowel inside the run does not defeat it.
    """
    if a.compact == b.compact and a.compact:
        return 1.0

    long_side, short_side = (a, b) if len(a.core_tokens) < len(b.core_tokens) else (b, a)
    # `long_side` is the one with FEWER tokens — the concatenated candidate.
    if len(long_side.core_tokens) != 1 or len(short_side.core_tokens) < 2:
        # Neither is a single run; fall back to compact-string similarity, which
        # still catches a stray space in the middle of one spelling.
        return edit_ratio(a.compact, b.compact) if a.compact and b.compact else 0.0

    run = long_side.compact
    pieces = short_side.core_tokens
    if not run or abs(len(run) - sum(len(p) for p in pieces)) > len(pieces):
        return 0.0

    # Walk the run, consuming one piece at a time and allowing a small drift.
    pos = 0
    matched = 0
    for piece in pieces:
        window = run[pos:pos + len(piece) + 1]
        if not window:
            break
        if window[:len(piece)] == piece:
            pos += len(piece)
            matched += len(piece)
        elif levenshtein(window[:len(piece)], piece) <= 1:
            pos += len(piece)
            matched += len(piece) - 1
        else:
            return 0.0
    coverage = matched / max(len(run), 1)
    leftover = len(run) - pos
    if leftover > 1:
        coverage *= 0.9 ** leftover
    return min(1.0, coverage)


# ---------------------------------------------------------------------------
# STAGE 9 support — people are matched differently from companies
# ---------------------------------------------------------------------------

def person_name_score(a: Representations, b: Representations) -> float:
    """Similarity tuned for human names.

    Two properties that do not hold for companies drive this:

    1. ORDER IS FREE. `CHETHAN B M` and `B M CHETAN` are one person; `ACME
       TRADERS` and `TRADERS ACME` are not a company anyone writes twice.
    2. THE SURNAME CARRIES MORE THAN THE GIVEN NAME. Two people sharing a first
       name is unremarkable; sharing an unusual surname AND a first initial is
       not.
    """
    if not a.core_tokens or not b.core_tokens:
        return 0.0

    aligned = best_token_alignment(a.core_tokens, b.core_tokens)
    sorted_sim = token_sort_ratio(a.core_tokens, b.core_tokens)

    # THE SURNAME, found by matching rather than by position.
    #
    # Taking the longest token on each side looks reasonable and is wrong:
    # `R GUPTA` yields GUPTA and `ROHAN GUPTA` yields ROHAN (a tie on length,
    # won by the first), so the comparison becomes GUPTA vs ROHAN and one
    # person scores as two. Instead, find the best-matching substantive pair —
    # whichever tokens those turn out to be, in whatever order the bank wrote
    # them.
    subs_a = [t for t in a.core_tokens if len(t) >= 3] or list(a.core_tokens)
    subs_b = [t for t in b.core_tokens if len(t) >= 3] or list(b.core_tokens)
    surname = 0.0
    for ta in subs_a:
        for tb in subs_b:
            v = jaro_winkler(ta, tb)
            if indic_phonetic(ta) == indic_phonetic(tb) and len(indic_phonetic(ta)) >= 3:
                v = 1.0
            surname = max(surname, v)

    phonetic_whole = 1.0 if (a.phonetic and a.phonetic == b.phonetic) else 0.0
    return max(
        phonetic_whole,
        0.45 * aligned + 0.35 * surname + 0.20 * sorted_sim,
    )


def indic_phonetic_equal(a: str, b: str) -> bool:
    """Do two words reduce to the same sound code, meaningfully?

    Guards the length: a two-character code collides with half the dictionary,
    so a match that short is not evidence of anything.
    """
    ca, cb = indic_phonetic(a), indic_phonetic(b)
    return bool(ca) and ca == cb and len(ca) >= 3
