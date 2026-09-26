"""STAGES 8-14 and 16: from a pile of strings to a set of real entities.

THE OBJECTIVE, restated because it is easy to drift from:

    NOT "find strings that look similar"
    BUT "decide which strings refer to the same real-world party, and
         represent them as ONE canonical entity"

Those are different jobs. The first is a similarity function; the second needs
evidence, clustering and the willingness to say no.

HOW IT WORKS

    BLOCKING      cheap keys put plausible pairs in the same bucket, so this is
                  not O(n^2) on a statement with thousands of rows
    PASSES        nine passes from strictest to loosest (stage 10), each one
                  merging clusters before the next runs, so evidence compounds
    SCORING       a weighted combination of every signal (stage 13), never a
                  single threshold
    CONFLICTS     contradictory identifiers veto a merge outright (stage 14)
    CANONICAL     the cleanest complete spelling represents the cluster (12)

WHY PASSES COMPOUND. After `RAMACHANDRA KOTHARI` and `RAMACHANDRAKOTHARI` are
one cluster, a third string only has to match EITHER of them to join. That is
stage 11, and it is what stops a party fragmenting into five near-misses that
each individually fall a hair under the bar.

NOTHING HERE KNOWS A NAME. Feed it a statement from a bank nobody has seen and
it behaves the same way.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import (Any, Dict, FrozenSet, Iterable, List, Optional, Sequence,
                    Set, Tuple)

from app.entity_resolution import similarity as S
from app.entity_resolution.normalize import (Representations, clean_narration,
                                             representations)

__all__ = [
    "EntityMention", "EntityCluster", "Weights", "MatchVerdict",
    "ResolutionReport", "EntityResolver", "resolve",
]


# ---------------------------------------------------------------------------
# STAGE 8 — the context a mention carries
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EntityMention:
    """One appearance of a party, with whatever the statement knew about it.

    Every context field is OPTIONAL, because no two banks provide the same
    ones. A mention with nothing but a raw string still resolves — it just
    resolves on name evidence alone, and the score reflects that.
    """
    raw: str
    row_id: Optional[str] = None
    account_ref: Optional[str] = None     # counterparty account number/fragment
    ifsc: Optional[str] = None
    utr: Optional[str] = None
    merchant_id: Optional[str] = None
    vpa: Optional[str] = None             # UPI handle
    phone: Optional[str] = None
    email: Optional[str] = None
    direction: Optional[str] = None       # 'credit' | 'debit'
    method: Optional[str] = None          # UPI | NEFT | IMPS | ...
    amount: Optional[float] = None
    date: Optional[Any] = None

    def identifiers(self) -> Set[Tuple[str, str]]:
        """Hard identity evidence — things that name a party, not describe one.

        An IFSC or a UTR is not identity (a bank branch, a transfer id), so
        neither appears here. An account number, a VPA, a merchant id, a phone
        or an email belong to exactly one party.
        """
        out: Set[Tuple[str, str]] = set()
        for kind, value in (("account", self.account_ref), ("vpa", self.vpa),
                            ("merchant", self.merchant_id), ("phone", self.phone),
                            ("email", self.email)):
            v = (value or "").strip().upper()
            if v and v not in {"NA", "N/A", "NONE", "-"}:
                out.add((kind, v))
        return out


# ---------------------------------------------------------------------------
# STAGE 13 — the weights, configurable rather than baked in
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Weights:
    """What each signal contributes. Tune on data, not on intuition.

    The brief asked for a combined score rather than `similarity > 0.8`, and
    these are its terms. They do not sum to 1: the score is normalised by the
    weights that actually APPLIED to a given pair, so a pair with no context
    is not punished for the context signals being unavailable.
    """
    exact: float = 3.0
    token_set: float = 1.0
    token_align: float = 1.4
    token_sort: float = 0.8
    containment: float = 0.5
    character: float = 1.2
    ngram: float = 0.8
    phonetic: float = 1.0
    concatenation: float = 1.6
    truncation: float = 1.0
    initials: float = 0.3
    identifier: float = 2.5
    context: float = 0.6

    # Stage 13's three outcomes.
    high: float = 0.86            # merge automatically
    medium: float = 0.70          # merge only with supporting context
    # anything below `medium` stays separate

    # Stage 14.
    conflict_veto: float = 1.0    # a hard conflict subtracts this much
    distinguishing_penalty: float = 0.35


@dataclass
class MatchVerdict:
    """Why two names were or were not judged the same party."""
    score: float
    confidence: str               # 'high' | 'medium' | 'low'
    signals: Dict[str, float] = field(default_factory=dict)
    conflicts: List[str] = field(default_factory=list)
    supported_by_context: bool = False

    @property
    def should_merge(self) -> bool:
        return self.confidence == "high"


# ---------------------------------------------------------------------------
# STAGE 11 — a cluster, not a pair
# ---------------------------------------------------------------------------

@dataclass
class EntityCluster:
    """One real-world party and every string that has ever named it."""
    entity_id: str
    mentions: List[EntityMention] = field(default_factory=list)
    reps: List[Representations] = field(default_factory=list)
    identifiers: Set[Tuple[str, str]] = field(default_factory=set)
    merge_reasons: List[str] = field(default_factory=list)

    @property
    def aliases(self) -> List[str]:
        seen, out = set(), []
        for r in self.reps:
            key = r.cleaned or r.original
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    @property
    def size(self) -> int:
        return len(self.mentions)

    @property
    def is_person(self) -> bool:
        votes = sum(1 for r in self.reps if r.is_person)
        return votes * 2 >= len(self.reps)

    def absorb(self, other: "EntityCluster", reason: str) -> None:
        self.mentions.extend(other.mentions)
        self.reps.extend(other.reps)
        self.identifiers |= other.identifiers
        self.merge_reasons.append(reason)
        self.merge_reasons.extend(other.merge_reasons)

    # ---- STAGE 12 --------------------------------------------------------
    @property
    def canonical(self) -> str:
        """The spelling a person should be shown.

        Ranked by, in order: no leftover transaction metadata, completeness
        (a truncated `PVT LT` loses to a full `PRIVATE LIMITED`), how often the
        bank wrote it, and readability. Explicitly NOT the shortest string —
        the brief calls that out, and the shortest is usually the truncated one.
        """
        if not self.reps:
            return ""
        counts = Counter(r.cleaned for r in self.reps if r.cleaned)

        def rank(rep: Representations) -> Tuple:
            text = rep.cleaned
            if not text:
                return (1, 0, 0, 0, "")
            has_digits = any(c.isdigit() for c in text)
            # A complete legal form beats a truncated one.
            complete = sum(1 for t in rep.tokens
                           if t in {"PRIVATE", "LIMITED", "COMPANY", "CORPORATION",
                                    "INCORPORATED"})
            return (
                0,                       # sorts ahead of the empty case
                -int(not has_digits),    # metadata-free first
                -complete,
                -counts[text],           # then most frequently written
                -len(text),              # then most complete
            )

        best = min(self.reps, key=rank)
        return _titlecase(best.cleaned)


def _titlecase(text: str) -> str:
    """`RESILIENT INNOVATIONS PVT LTD` -> `Resilient Innovations Pvt Ltd`."""
    out = []
    for word in text.split():
        if len(word) <= 3 and word.isupper() and not word.isalpha():
            out.append(word)
        elif len(word) <= 2:
            out.append(word.upper())        # initials stay initials
        else:
            out.append(word.capitalize())
    return " ".join(out)


# ---------------------------------------------------------------------------
# STAGE 16 — the numbers the brief asks to be reported
# ---------------------------------------------------------------------------

@dataclass
class ResolutionReport:
    raw_unique_entities: int = 0
    canonical_entities: int = 0
    entities_merged: int = 0
    merge_percentage: float = 0.0
    high_confidence_merges: int = 0
    medium_confidence_candidates: int = 0
    unresolved_entities: int = 0
    clusters: List[EntityCluster] = field(default_factory=list)
    suggestions: List[Tuple[str, str, MatchVerdict]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "raw_unique_entities": self.raw_unique_entities,
            "canonical_entities": self.canonical_entities,
            "entities_merged": self.entities_merged,
            "merge_percentage": round(self.merge_percentage, 2),
            "high_confidence_merges": self.high_confidence_merges,
            "medium_confidence_candidates": self.medium_confidence_candidates,
            "unresolved_entities": self.unresolved_entities,
        }


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------

# Shortest name that may be matched as a prefix or suffix of a longer one.
#
# Three, measured. Four was tried, on the theory that three-letter prefixes
# collide, and it cost `OLA`/`OLACABS`, `JIO`/`RELIANCEJIO` and `LIC`/`LICPREMIUM`
# — six entities on the ground-truth set — while false merges stayed at zero
# either way. Three earns its keep because the affix rule also demands the short
# side be the WHOLE of the other name, which is a much stronger condition than
# "is a substring".
_MIN_AFFIX = 3

# Stage 10, in order. Each entry is (name, minimum score to merge on this pass).
# Early passes are cheap and certain; later ones are looser and lean harder on
# the clusters the earlier passes already built.
_PASSES: Tuple[Tuple[str, str], ...] = (
    ("exact",         "Identical after normalisation"),
    ("token",         "Same tokens, any order"),
    ("legal",         "Same core once the legal form is removed"),
    ("concatenated",  "One is the other with the spaces removed"),
    ("character",     "Spelling drift within tolerance"),
    ("phonetic",      "Sounds the same"),
    ("context",       "Shares an identifier or transaction context"),
    ("alias",         "Matches an alias the cluster already holds"),
    ("cluster",       "Matches the cluster as a whole"),
)


class EntityResolver:
    """Aggressive-but-safe canonicalisation. Reusable and bank-agnostic."""

    def __init__(self, weights: Optional[Weights] = None,
                 *, known_same: Optional[Iterable[Tuple[str, str]]] = None,
                 known_different: Optional[Iterable[Tuple[str, str]]] = None):
        self.w = weights or Weights()
        # STAGE 15. A person's answer outranks every score in this file, in
        # both directions — including "no", which is the half that is usually
        # forgotten and the reason a rejected suggestion keeps coming back.
        # Normalised through the SAME pipeline as everything else, so a
        # decision saved against one spelling is found from any other. Storing
        # the raw strings would make the memory as fragile as the problem it is
        # there to fix.
        self.known_same = {_decision_key(x, y) for x, y in (known_same or ())}
        self.known_different = {_decision_key(x, y)
                                for x, y in (known_different or ())}

    # -- scoring ---------------------------------------------------------
    def compare(self, a: Representations, b: Representations,
                *, ctx_a: Optional[Sequence[EntityMention]] = None,
                ctx_b: Optional[Sequence[EntityMention]] = None) -> MatchVerdict:
        """STAGE 13. One combined score from every signal that applies."""
        w = self.w
        signals: Dict[str, float] = {}
        weighted = 0.0
        applied = 0.0

        def add(name: str, value: float, weight: float) -> None:
            nonlocal weighted, applied
            signals[name] = round(value, 4)
            weighted += value * weight
            applied += weight

        if a.is_empty or b.is_empty:
            return MatchVerdict(0.0, "low", signals, ["one side has no name"])

        pair = frozenset((a.compact, b.compact))
        if pair in self.known_different:
            return MatchVerdict(0.0, "low", signals, ["a person said these differ"])
        if pair in self.known_same:
            return MatchVerdict(1.0, "high", {"user_confirmed": 1.0}, [],
                                supported_by_context=True)

        # ---- BASE: signals that always apply, as a weighted mean -----------
        #
        # These measure general resemblance and every pair has a value for all
        # of them, so averaging is meaningful.
        add("token_set", S.token_set_ratio(a.core_tokens, b.core_tokens), w.token_set)
        add("token_align", S.best_token_alignment(a.core_tokens, b.core_tokens),
            w.token_align)
        add("token_sort", S.token_sort_ratio(a.core_tokens, b.core_tokens), w.token_sort)
        add("containment", S.token_containment(a.core_tokens, b.core_tokens),
            w.containment)
        add("character", S.jaro_winkler(a.compact, b.compact), w.character)
        add("ngram", S.ngram_dice(a.trigrams, b.trigrams), w.ngram)
        add("phonetic",
            1.0 if (a.phonetic and a.phonetic == b.phonetic) else
            S.jaro_winkler(a.phonetic, b.phonetic) * 0.7, w.phonetic)
        add("initials", S.initials_match(a, b), w.initials)
        base = weighted / applied if applied else 0.0

        # ---- EVIDENCE: signals that are POSITIVE-ONLY ----------------------
        #
        # This is the part a naive weighted average gets wrong, and it is worth
        # being explicit about. "These are literally the same string once
        # normalised" is proof of identity; "these are NOT the same string" is
        # not evidence of anything, because that is true of every pair the
        # engine exists to merge. Averaging such a signal in means a strong
        # proof gets diluted by the eight ordinary measures sitting next to it,
        # and `RAMACHANDRAKOTHARI` scores 0.6 against `RAMACHANDRA KOTHARI`.
        #
        # So these lift the score and never lower it. Each is discounted
        # slightly below 1.0 so that an exact normalised match still ranks
        # above an inferred one.
        evidence: List[Tuple[str, float]] = []
        if a.compact == b.compact:
            evidence.append(("exact", 1.0))
        elif a.compact_core == b.compact_core and len(a.compact_core) >= 4:
            # Same name once the generic trailers are shaved: `HDFCBANK` and
            # `HDFC`, `UBERTECHNOLOGY` and `UBER`. Slightly below a literal
            # match, because shaving is an inference.
            evidence.append(("core", 0.96))
        concat = S.concatenation_score(a, b)
        signals["concatenation"] = round(concat, 4)
        if concat > 0:
            evidence.append(("concatenation", concat * 0.98))
        trunc = max(_truncation_score(a.compact, b.compact),
                    _truncation_score(a.compact_core, b.compact_core) * 0.98)
        signals["truncation"] = round(trunc, 4)
        if trunc > 0:
            evidence.append(("truncation", trunc * 0.95))

        # FULL CONTAINMENT. One name is the other plus a qualifier: `AMAZON` and
        # `AMAZON INDIA`, `UBER` and `UBER TRIP`, `ICICI` and `ICICI BANK`. On a
        # bank statement that is one merchant writing itself two ways far more
        # often than it is two companies.
        #
        # The dangerous case is not this one. It is SUBSTITUTION — `ABC
        # BROTHERS` against `ABC ENTERPRISES`, where each side carries a word
        # the other contradicts — and `_conflicts` handles that separately and
        # still vetoes it. Containment adds a word; substitution disputes one.
        # AFFIX CONTAINMENT on the run-together form. `OLA` inside `OLACABS`,
        # `JIO` at the end of `RELIANCEJIO` — a brand with a product line bolted
        # on the front or the back, written without a space so no token-level
        # rule can see it.
        #
        # Requires the shorter side to be the WHOLE of the other name, not an
        # arbitrary substring, and at least `_MIN_AFFIX` characters.
        sc, lc = sorted((a.compact_core, b.compact_core), key=len)
        if len(sc) >= _MIN_AFFIX and sc != lc and (lc.startswith(sc) or lc.endswith(sc)):
            ratio = len(sc) / len(lc)
            signals["affix"] = round(ratio, 4)
            evidence.append(("affix", 0.86 + 0.10 * ratio))

        # INITIALISM. `SBI` against `STATE BANK INDIA`. The brief lists
        # abbreviations, and this is the only form of them a rule can derive
        # without a lookup table: the short name IS the first letters of the
        # long one, in order.
        init_hit = _initialism_score(a, b)
        if init_hit:
            signals["initialism"] = round(init_hit, 4)
            evidence.append(("initialism", init_hit))

        contain = S.token_containment(a.core_tokens, b.core_tokens)
        if contain >= 0.999 and min(len(a.compact), len(b.compact)) >= 4:
            shorter = min(len(a.core_tokens), len(b.core_tokens))
            longer = max(len(a.core_tokens), len(b.core_tokens))
            # Confidence falls as the extra material grows: one appended word is
            # a qualifier, three is probably a different organisation.
            # One appended qualifier still merges; three appended words is
            # probably a different organisation and lands in medium.
            evidence.append(("containment", max(0.5, 0.97 - 0.06 * (longer - shorter))))

        # -- STAGE 9: entity type decides WHICH strategy, not whether to run --
        if a.is_person and b.is_person:
            person = S.person_name_score(a, b)
            signals["person"] = round(person, 4)
            evidence.append(("person", person * 0.97))

        # -- STAGE 8: context ------------------------------------------------
        ids_a = _identifiers(ctx_a)
        ids_b = _identifiers(ctx_b)
        supported = False
        if ids_a and ids_b and (ids_a & ids_b):
            signals["identifier"] = 1.0
            evidence.append(("identifier", 0.95))
            supported = True
        ctx = _context_similarity(ctx_a or (), ctx_b or ())
        if ctx:
            signals["context"] = round(ctx, 4)
        # DELIBERATELY NOT a promoter. Direction and rail were, briefly, allowed
        # to lift a medium match to high, and on the test set that merged two
        # unrelated people whose names shared one initial: both were paid by
        # NEFT debit, as is most of any statement, so the "context agrees"
        # signal fired on a pair that had nothing else in common.
        #
        # Behaviour is not identity. Only an IDENTIFIER — an account number, a
        # VPA, a merchant id — says two strings are the same party, and only
        # that is allowed to promote.

        score = max([base] + [v for _n, v in evidence])
        signals["base"] = round(base, 4)

        if a.is_person != b.is_person:
            # One reads as a person and one as a company. Weak evidence against,
            # never a veto: the type test is a heuristic and does get it wrong.
            score *= 0.92

        # -- STAGE 14: conflicts ---------------------------------------------
        conflicts = _conflicts(a, b, ids_a, ids_b, self.w)
        for _reason, penalty in conflicts:
            score -= penalty
        score = max(0.0, min(1.0, score))

        if score >= w.high:
            confidence = "high"
        elif score >= w.medium:
            confidence = "medium"
        else:
            confidence = "low"

        # A medium score is promoted only when the context backs it up. That is
        # exactly the rule the brief asked for, in one place.
        if confidence == "medium" and supported:
            confidence = "high"

        return MatchVerdict(score, confidence, signals,
                            [r for r, _p in conflicts], supported)

    # -- the pipeline ----------------------------------------------------
    def resolve(self, mentions: Sequence[EntityMention]) -> ResolutionReport:
        """Cluster every mention into canonical entities."""
        clusters: List[EntityCluster] = []
        by_cleaned: Dict[str, EntityCluster] = {}

        # PASS 1 is free: identical cleaned strings are one entity by definition.
        raw_strings: Set[str] = set()
        for m in mentions:
            raw = (m.raw or "").strip()
            if raw:
                raw_strings.add(raw.upper())
            rep = representations(m.raw)
            if rep.is_empty:
                continue
            existing = by_cleaned.get(rep.compact)
            if existing is None:
                existing = EntityCluster(entity_id=f"ENT_{len(clusters) + 1:04d}")
                clusters.append(existing)
                by_cleaned[rep.compact] = existing
            existing.mentions.append(m)
            existing.reps.append(rep)
            existing.identifiers |= m.identifiers()

        report = ResolutionReport(raw_unique_entities=len(raw_strings))
        if not clusters:
            return report

        # PASSES 2-9. Blocking keeps this near-linear: only clusters sharing a
        # cheap key are ever compared, and the keys are chosen so that every
        # kind of variation the brief lists survives at least one of them.
        merged_high = 0
        suggestions: List[Tuple[str, str, MatchVerdict]] = []
        alive = {c.entity_id: c for c in clusters}

        for _pass_name, _why in _PASSES[1:]:
            buckets: Dict[str, List[EntityCluster]] = defaultdict(list)
            for c in alive.values():
                for key in _blocking_keys(c):
                    buckets[key].append(c)

            for bucket in buckets.values():
                if len(bucket) < 2:
                    continue
                # Largest first: a small fragment should join the established
                # cluster, not the other way round.
                bucket.sort(key=lambda c: -c.size)
                for i, target in enumerate(bucket):
                    if target.entity_id not in alive:
                        continue
                    for other in bucket[i + 1:]:
                        if other.entity_id not in alive or other is target:
                            continue
                        verdict = self._compare_clusters(target, other)
                        if verdict.should_merge:
                            target.absorb(other, _why)
                            del alive[other.entity_id]
                            merged_high += 1
                        elif verdict.confidence == "medium":
                            suggestions.append(
                                (target.canonical, other.canonical, verdict))

        final = list(alive.values())
        report.clusters = sorted(final, key=lambda c: -c.size)
        report.canonical_entities = len(final)
        # Measured against the RAW strings, which is what the user counted.
        # Pass 1 has already collapsed identical-after-cleaning spellings by the
        # time `clusters` exists, so comparing against it would hide the single
        # largest source of reduction.
        raw_n = report.raw_unique_entities or len(clusters)
        report.entities_merged = max(0, raw_n - len(final))
        report.merge_percentage = (
            100.0 * report.entities_merged / raw_n if raw_n else 0.0)
        report.high_confidence_merges = merged_high
        # A suggestion is only worth a person's time if the two sides ended up
        # in DIFFERENT clusters. Pairs recorded mid-run and merged by a later
        # pass would otherwise be presented as open questions that the engine
        # has already answered.
        canonical_now = {c.canonical for c in final}
        cluster_of = {}
        for c in final:
            for r in c.reps:
                cluster_of[r.cleaned] = c.entity_id
        report.suggestions = [
            (x, y, v) for x, y, v in _dedupe_suggestions(suggestions)
            if x in canonical_now and y in canonical_now
        ]
        report.medium_confidence_candidates = len(report.suggestions)
        report.unresolved_entities = sum(1 for c in final if c.size == 1)
        return report

    def _compare_clusters(self, a: EntityCluster, b: EntityCluster) -> MatchVerdict:
        """STAGE 11. Compare against the WHOLE cluster, not one representative.

        A new spelling only has to match any alias the cluster already holds.
        This is what stops a party splintering into five near-misses that each
        individually fall just under the bar.
        """
        best: Optional[MatchVerdict] = None
        for ra in a.reps[:12]:               # bounded: clusters can get large
            for rb in b.reps[:12]:
                v = self.compare(ra, rb, ctx_a=a.mentions, ctx_b=b.mentions)
                if best is None or v.score > best.score:
                    best = v
                if best.should_merge:
                    return best
        return best or MatchVerdict(0.0, "low")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _initialism_score(a: Representations, b: Representations) -> float:
    """Is one name the initials of the other?

    `SBI` for `STATE BANK INDIA`, `HDFC` for a four-word title. Requires three
    or more letters — two-letter initialisms collide with everything — and the
    letters must appear as the token initials IN ORDER, which is what makes this
    a derivation rather than a guess.
    """
    for short, long_ in ((a, b), (b, a)):
        if len(short.core_tokens) != 1 or len(long_.core_tokens) < 2:
            continue
        acronym = short.compact_core
        if len(acronym) < 3 or len(acronym) > len(long_.core_tokens) + 1:
            continue
        built = "".join(t[0] for t in long_.core_tokens)
        if built == acronym:
            return 0.93
        # Tolerate one missing word: statements drop `CORPORATION`, `LIMITED`
        # and the like from the middle of an official name.
        if len(acronym) == len(built) + 1 and built == acronym[:len(built)]:
            return 0.88
    return 0.0


def _decision_key(a: str, b: str) -> FrozenSet[str]:
    """A stable key for a human decision about two names.

    Built from the compacted core, so `ABC Pvt Ltd`, `ABC PRIVATE LIMITED` and
    `NEFT-xxx-ABC PVT LTD` all address the same stored answer.
    """
    return frozenset((representations(a).compact, representations(b).compact))


def _truncation_score(a: str, b: str) -> float:
    """Is one compact string a prefix of the other?

    Fixed-width exports cut names at a column boundary, so `RESILIENTINNOVA` and
    `RESILIENTINNOVATIONS` are the same firm at two widths. Requires a long
    shared prefix — six characters — because short prefixes collide constantly.
    """
    if not a or not b:
        return 0.0
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) < 6:
        return 0.0
    if not longer.startswith(shorter):
        return 0.0
    return min(1.0, len(shorter) / len(longer) + 0.25)


def _identifiers(mentions: Optional[Sequence[EntityMention]]) -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for m in mentions or ():
        out |= m.identifiers()
    return out


def _context_similarity(a: Sequence[EntityMention],
                        b: Sequence[EntityMention]) -> float:
    """STAGE 8. Do these two behave like the same party?

    Direction and rail are the honest signals here. Two strings that only ever
    appear as credits over NEFT are more likely one party than a pair split
    across incoming UPI and outgoing cheques. Amount and date patterns are
    deliberately NOT used: recurring amounts are a property of the arrangement,
    not the party, and treating them as identity merges every ₹5,000 standing
    instruction on the statement.
    """
    def profile(ms: Sequence[EntityMention]) -> Tuple[Counter, Counter]:
        return (Counter((m.direction or "").lower() for m in ms if m.direction),
                Counter((m.method or "").upper() for m in ms if m.method))

    da, ma = profile(a)
    db, mb = profile(b)
    parts: List[float] = []
    for x, y in ((da, db), (ma, mb)):
        if not x or not y:
            continue
        shared = sum((x & y).values())
        total = sum((x | y).values())
        parts.append(shared / total if total else 0.0)
    return sum(parts) / len(parts) if parts else 0.0


def _conflicts(a: Representations, b: Representations,
               ids_a: Set[Tuple[str, str]], ids_b: Set[Tuple[str, str]],
               w: Weights) -> List[Tuple[str, float]]:
    """STAGE 14. Evidence strong enough to stop an otherwise-good match.

    Aggressive canonicalisation is the goal, but a merge is irreversible from
    the user's point of view — they see one party where there were two and have
    no way to know it happened. So contradiction wins over similarity.
    """
    out: List[Tuple[str, float]] = []

    # Identifiers of the same KIND that disagree. Two account numbers is the
    # textbook case; sharing none while both are known is the contradiction.
    kinds_a = {k for k, _v in ids_a}
    kinds_b = {k for k, _v in ids_b}
    for kind in kinds_a & kinds_b:
        va = {v for k, v in ids_a if k == kind}
        vb = {v for k, v in ids_b if k == kind}
        if va and vb and not (va & vb):
            out.append((f"different {kind}", w.conflict_veto))

    # A DISTINGUISHING word — one side carries a token the other lacks that is
    # not a legal form and not a plural. `ABC Technologies` and `ABC
    # Technologies India` may be two registered companies, and the brief names
    # this case specifically.
    only_a = set(a.core_tokens) - set(b.core_tokens)
    only_b = set(b.core_tokens) - set(a.core_tokens)
    extra = only_a | only_b
    substantive = {t for t in extra if len(t) >= 3 and t.isalpha()}
    if substantive and (a.core_tokens and b.core_tokens):
        # Only counts when the rest genuinely matched — otherwise the ordinary
        # similarity signals have already handled it.
        overlap = S.token_containment(a.core_tokens, b.core_tokens)
        # Only when BOTH sides carry a word the other lacks. One side simply
        # having MORE words is containment, handled as positive evidence above:
        # `AMAZON` and `AMAZON INDIA` are one merchant, and treating the extra
        # qualifier as a conflict split every brand on the test set into three.
        if overlap >= 0.99 and only_a and only_b:
            out.append((f"each side has a word the other lacks: "
                        f"{', '.join(sorted(substantive))}",
                        w.distinguishing_penalty))

    # SAME SHAPE, ONE WORD DIFFERENT. `COASTAL SRIRAM` and `COASTAL GAYADI`
    # share a word and a length and are obviously two parties; without this they
    # score in the 0.8s on token-sort alone, because most of the string agrees.
    #
    # Only fires when the differing token is substantive AND genuinely unlike
    # its counterpart — `MANOJ NATH GOSWAMI` vs `MANOJ N GOSWAMI` differs in one
    # token too, and there the tokens are an abbreviation of each other.
    if (len(a.core_tokens) == len(b.core_tokens) >= 2
            and len(only_a) == len(only_b) == 1):
        ta, tb = next(iter(only_a)), next(iter(only_b))
        if (len(ta) >= 3 and len(tb) >= 3
                and ta[0] != tb[0]
                and S.jaro_winkler(ta, tb) < 0.80
                and S.indic_phonetic_equal(ta, tb) is False):
            out.append((f"different word: {ta} vs {tb}", w.conflict_veto))

    # Two different NUMBERS inside otherwise identical names: branch 1 and
    # branch 2, or two franchises of one brand.
    nums_a = {t for t in a.core_tokens if t.isdigit()}
    nums_b = {t for t in b.core_tokens if t.isdigit()}
    if nums_a and nums_b and not (nums_a & nums_b):
        out.append(("different numbers in the name", w.conflict_veto))

    return out


def _blocking_keys(cluster: EntityCluster) -> Set[str]:
    """Cheap keys that put plausible pairs in the same bucket.

    Each key survives a DIFFERENT kind of variation, so a pair only has to
    survive one of them to get compared:

        phonetic      spelling and transliteration drift
        sorted core   token reordering
        prefix        truncation and concatenation
        initials      abbreviated forms
        longest token a shared distinctive word

    Without blocking this is quadratic and unusable on a real statement; with
    it, a bucket is a handful of candidates.
    """
    keys: Set[str] = set()
    for r in cluster.reps[:12]:
        if not r.core_tokens:
            continue
        if r.phonetic:
            keys.add("P:" + r.phonetic[:8])
        keys.add("S:" + "".join(r.sorted_core)[:10])
        if len(r.compact) >= 5:
            keys.add("X:" + r.compact[:5])
        if len(r.compact_core) >= 4:
            keys.add("C:" + r.compact_core)
        # Short prefixes and suffixes, so a brand and its product line meet in
        # the same bucket even though nothing else about them agrees.
        if len(r.compact_core) >= 3:
            keys.add("A:" + r.compact_core[:3])
            keys.add("Z:" + r.compact_core[-3:])
        if len(r.core_tokens) >= 2:
            keys.add("N:" + "".join(t[0] for t in r.core_tokens))
        if r.initials:
            keys.add("I:" + "".join(sorted(r.initials))[:4])
        longest = max(r.core_tokens, key=len)
        if len(longest) >= 4:
            keys.add("T:" + longest[:6])
    for kind, value in list(cluster.identifiers)[:8]:
        keys.add(f"ID:{kind}:{value}")
    return keys


def _dedupe_suggestions(
    suggestions: Sequence[Tuple[str, str, MatchVerdict]],
) -> List[Tuple[str, str, MatchVerdict]]:
    seen: Set[FrozenSet[str]] = set()
    out: List[Tuple[str, str, MatchVerdict]] = []
    for a, b, v in sorted(suggestions, key=lambda s: -s[2].score):
        key = frozenset((a, b))
        if key in seen or a == b:
            continue
        seen.add(key)
        out.append((a, b, v))
    return out


def resolve(raw_strings: Iterable[str],
            weights: Optional[Weights] = None) -> ResolutionReport:
    """Convenience entry point for a bare list of strings, no context."""
    return EntityResolver(weights).resolve(
        [EntityMention(raw=s) for s in raw_strings])


# ---------------------------------------------------------------------------
# The bridge the rest of the application uses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClusterAssignment:
    """Where one narration ended up, in terms the review queue can store."""
    cluster_key: str          # stable, derived from the canonical name
    canonical: str            # what a person should be shown
    aliases: Tuple[str, ...]  # every spelling folded into it
    size: int


def cluster_narrations(
    mentions: Sequence[EntityMention],
    *, weights: Optional[Weights] = None,
    known_same: Optional[Iterable[Tuple[str, str]]] = None,
    known_different: Optional[Iterable[Tuple[str, str]]] = None,
) -> Tuple[Dict[str, ClusterAssignment], ResolutionReport]:
    """Resolve a batch of mentions and return a per-RAW-STRING lookup.

    This is what the review queue calls. It asks one question — "which party is
    this row about" — and gets an answer that already folds in every spelling
    on the statement, so a party is one question rather than nine.

    The `cluster_key` is derived from the canonical name rather than from
    whichever spelling happened to arrive first, so it is stable across uploads:
    a statement that adds a new spelling of a known party does not renumber the
    decision already stored against it.
    """
    resolver = EntityResolver(weights, known_same=known_same,
                              known_different=known_different)
    report = resolver.resolve(mentions)

    lookup: Dict[str, ClusterAssignment] = {}
    for cluster in report.clusters:
        canonical = cluster.canonical
        key = representations(canonical).compact or canonical.upper()
        assignment = ClusterAssignment(
            cluster_key=key,
            canonical=canonical,
            aliases=tuple(cluster.aliases),
            size=cluster.size,
        )
        for mention in cluster.mentions:
            raw = (mention.raw or "").strip()
            if raw:
                lookup[raw] = assignment
    return lookup, report
