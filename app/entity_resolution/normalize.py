"""STAGES 1-3: turn a raw bank narration into comparable representations.

THE PROBLEM THIS SOLVES. A bank statement does not contain entities, it contains
strings, and the same real-world party arrives as a different string almost
every time:

    UPI UPI RAMACHANDRAKOTHARI
    NEFT-YESB43340764058-RAMACHANDRA KOTHARI-YES BANK
    IMPS/P2A/512334455/Ramachandra  Kothari
    RAMACHANDRA KOTHARI

Four strings, one person. Every difference above is FORMAT — a rail name, a
reference number, a bank name, whitespace, case. None of it is identity.

NOTHING IN THIS FILE KNOWS ANY NAME. It is built entirely from the SHAPE of
narration text: a run of digits is a reference wherever it appears, a token that
is also a payment rail is metadata whatever bank wrote it. That is what makes it
work on a statement from a bank nobody here has seen.

THE THREE STAGES

    1. CLEANING       strip transaction-system noise from the raw narration
    2. REPRESENTATION build every normalised form the matcher will need
    3. LEGAL FORM     `PVT LTD` and `PRIVATE LIMITED` are the same suffix

Stage 3 draws a line that matters: a LEGAL FORM carries no identity and is
removed, a TRADE DESCRIPTOR does and is kept. `ABC PVT LTD` and `ABC PRIVATE
LIMITED` are one company. `ABC BROTHERS` and `ABC ENTERPRISES` are two, and
collapsing both to `ABC` would merge them — so `BROTHERS` and `ENTERPRISES`
stay.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

__all__ = [
    "clean_narration", "representations", "Representations",
    "strip_legal_form", "legal_forms_in", "singularise", "repair_ocr",
    "canonical_legal_form",
    "indic_phonetic", "char_ngrams", "initials", "looks_like_person",
    "RAIL_TOKENS", "LEGAL_FORMS", "TRADE_DESCRIPTORS", "BOILERPLATE",
    "QUALIFIERS", "strip_trailing_known",
]


# ---------------------------------------------------------------------------
# Vocabularies. Categories of WORD, not lists of names.
#
# Every set below describes a kind of token that banks emit, and none of it is
# specific to any customer, statement or institution. Adding a name to any of
# these would be a bug: the engine has to work on parties it has never seen.
# ---------------------------------------------------------------------------

# Payment rails and channel markers. These say how money moved.
RAIL_TOKENS: FrozenSet[str] = frozenset({
    "UPI", "NEFT", "IMPS", "RTGS", "ECS", "ACH", "NACH", "POS", "ATM", "MMT",
    "TPT", "INB", "IB", "EBANK", "NETBANK", "NETBANKING", "MOBILE", "MOB",
    "CHQ", "CHEQUE", "CLG", "CLEARING", "DD", "PO", "BT", "VPA", "QR", "BHIM",
    "CARD", "DEBIT", "CREDIT", "DR", "CR", "WDL", "DEP", "TFR", "TRF", "TRANSFER",
    "P2A", "P2M", "P2P", "C2C", "OTW", "INW", "OW", "IW", "RTN", "REV",
})

# Words the bank writes about the transaction rather than the party.
BOILERPLATE: FrozenSet[str] = frozenset({
    "TO", "FROM", "BY", "FOR", "AT", "ON", "OF", "THE", "AND", "VIA", "WITH",
    "PAYMENT", "PAYMENTS", "PAY", "PAID", "RECEIVED", "RECEIPT", "COLLECT",
    "COLLECTION", "SETTLEMENT", "SETTLE", "REF", "REFNO", "NO", "TXN", "TRN",
    "TRANS", "TRANSACTION", "ID", "UTR", "RRN", "SEQ", "TID", "MID", "BRN",
    "BRANCH", "ACCOUNT", "ACC", "AC", "ACNO", "SELF", "OWN", "MISC", "OTHERS",
    "OTHER", "CHARGES", "CHARGE", "CHG", "CHGS", "FEE", "FEES", "AMT", "AMOUNT",
    "INR", "RS", "DATED", "DATE", "TIME", "SUCCESS", "SUCCESSFUL", "FAILED",
})

# LEGAL FORM — the incorporation wrapper. Carries no identity, so it goes.
# Keys are canonical; every spelling and truncation maps onto one of them.
LEGAL_FORMS: Dict[str, str] = {
    "PVT": "PRIVATE", "PVTLTD": "PRIVATE LIMITED", "PRIVATE": "PRIVATE",
    "PRIVAT": "PRIVATE", "PRIVA": "PRIVATE", "PRVT": "PRIVATE",
    "LTD": "LIMITED", "LIMITED": "LIMITED", "LIMITE": "LIMITED",
    "LIMIT": "LIMITED", "LTD.": "LIMITED", "LMTD": "LIMITED",
    "PUBLIC": "PUBLIC", "LLP": "LLP", "LLC": "LLC",
    "INC": "INC", "INCORPORATED": "INC", "CORP": "CORP",
    "CORPORATION": "CORP", "CO": "CO", "COMPANY": "CO",
    "OPC": "OPC", "HUF": "HUF", "TRUST": "TRUST", "SOCIETY": "SOCIETY",
    "FOUNDATION": "FOUNDATION", "NGO": "NGO",
}

# TRADE DESCRIPTOR — looks like boilerplate, is not. `ABC BROTHERS` and
# `ABC ENTERPRISES` are different firms, and stripping these would merge them.
# Listed so the legal-form stripper can be told explicitly to leave them alone.
TRADE_DESCRIPTORS: FrozenSet[str] = frozenset({
    "ENTERPRISE", "ENTERPRISES", "BROTHERS", "BROS", "SONS", "TRADERS",
    "TRADING", "INDUSTRIES", "INDUSTRY", "AGENCIES", "AGENCY", "STORES",
    "STORE", "MART", "SUPERMARKET", "TRADES", "ASSOCIATES", "PARTNERS",
    "GROUP", "HOLDINGS", "VENTURES", "SOLUTIONS", "SERVICES", "SYSTEMS",
    "TECHNOLOGIES", "TECHNOLOGY", "LABS", "WORKS", "MILLS", "TEXTILES",
    "MOTORS", "AUTOMOBILES", "PHARMA", "PHARMACY", "MEDICALS", "HOSPITAL",
    "CLINIC", "SCHOOL", "COLLEGE", "ACADEMY", "INSTITUTE", "HOTEL", "RESTAURANT",
    "CATERERS", "BAKERY", "DAIRY", "FOODS", "PRODUCTS", "PACKAGING", "PRINTERS",
    "STUDIO", "DESIGNS", "CONSTRUCTIONS", "BUILDERS", "DEVELOPERS", "PROPERTIES",
    "TRANSPORTS", "TRANSPORT", "LOGISTICS", "CARRIERS", "TOURS", "TRAVELS",
})

# GENERIC QUALIFIERS. Words a brand appends to itself that name a market, a
# product line or a channel rather than a different organisation:
# `AMAZON INDIA`, `UBER TRIP`, `AIRTEL PREPAID`, `LIC PREMIUM`.
#
# Category of word, not a list of brands — nothing here is a name, and the same
# set works on a statement full of parties nobody has seen.
QUALIFIERS: FrozenSet[str] = frozenset({
    "INDIA", "INDIAN", "BHARAT", "INTERNATIONAL", "GLOBAL", "WORLDWIDE",
    "ONLINE", "DIGITAL", "MOBILE", "WEB", "APP", "ECOM", "ECOMMERCE",
    "PREPAID", "POSTPAID", "RECHARGE", "PREMIUM", "SUBSCRIPTION", "RENEWAL",
    "TRIP", "RIDE", "ORDER", "BOOKING", "CHECKOUT", "WALLET", "PAY",
    "RETAIL", "MARKETPLACE", "SELLER", "MERCHANT", "BUSINESS", "CORPORATE",
    "PRIVATE", "PUBLIC", "NATIONAL", "REGIONAL", "SOUTH", "NORTH", "EAST",
    "WEST", "CENTRAL",
})

# Bank-name words. A trailing bank is metadata about the rail, not the payee.
BANK_WORDS: FrozenSet[str] = frozenset({
    "BANK", "BANKING", "SBI", "HDFC", "ICICI", "AXIS", "KOTAK", "YES", "IDFC",
    "IDBI", "PNB", "BOB", "BOI", "CANARA", "UNION", "INDIAN", "UCO", "RBL",
    "INDUSIND", "BANDHAN", "FEDERAL", "KARNATAKA", "KARUR", "VYSYA", "CITI",
    "CITIBANK", "HSBC", "DBS", "SCB", "STANDARD", "CHARTERED", "MAHARASHTRA",
    "BARODA", "SIND", "OVERSEAS", "CENTRAL", "NAINITAL", "DHANLAXMI", "CSB",
    "EQUITAS", "UJJIVAN", "ESAF", "JANA", "FINCARE", "AU", "SURYODAY", "UTKARSH",
})

# ---------------------------------------------------------------------------
# Shape patterns. These recognise a KIND of token, not a value.
# ---------------------------------------------------------------------------

_TIMESTAMP = re.compile(r"^\d{1,2}[:.]\d{2}(?:[:.]\d{2})?$")
_DATE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$")
_ALL_DIGITS = re.compile(r"^\d+$")
_IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
# A reference blob: letters and digits mixed, long enough that no name is
# plausible. `YESB43340764058`, `BARBR52025070100901976`, `HDFCH25081234567`.
_REF_BLOB = re.compile(r"^(?=.*\d)[A-Z0-9]{6,}$")
_MASKED = re.compile(r"^X+\d*$|^\d*X+\d*$")
_VPA = re.compile(r"^[A-Z0-9._-]+@[A-Z0-9]+$")
_SEPARATORS = re.compile(r"[/\-|:,;*#~]+")
_NON_NAME = re.compile(r"[^A-Z0-9&' ]+")

# ---------------------------------------------------------------------------
# OCR repair, applied BEFORE anything classifies a token.
#
# Scanned and image-sourced statements substitute digits for letters, and the
# damage is not cosmetic here: `B0BCARD` contains a digit, so the reference-blob
# rule below would throw the whole token away as a transaction id and the payee
# would vanish. Repair has to happen first or the noise filter eats the name.
#
# Only applied inside a token that is mostly letters, so a genuine reference
# number is never "repaired" into a word.
# ---------------------------------------------------------------------------
_OCR_SUBSTITUTIONS = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B", "6": "G"})
_OCR_CANDIDATE = re.compile(r"^(?=.*[A-Z])[A-Z0-9]+$")


def repair_ocr(token: str) -> str:
    """Undo digit-for-letter substitution in a token that is mostly letters."""
    if not _OCR_CANDIDATE.match(token):
        return token
    letters = sum(c.isalpha() for c in token)
    digits = len(token) - letters
    # Needs a real word underneath: at least three letters, and letters must
    # outnumber digits. `YESB43340764058` fails both and stays a reference.
    if letters < 3 or digits > letters:
        return token
    return token.translate(_OCR_SUBSTITUTIONS)


def _fold(text: str) -> str:
    """Unicode -> plain uppercase ASCII, so accents never split a party."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return ascii_only.upper()


# ---------------------------------------------------------------------------
# STAGE 1 — raw narration cleaning
# ---------------------------------------------------------------------------

def _token_is_noise(token: str) -> bool:
    """Is this token transaction machinery rather than part of a name?

    Every test is about the token's SHAPE or its membership of a category of
    banking word. None of them is about a particular party.
    """
    if not token:
        return True
    if token in RAIL_TOKENS or token in BOILERPLATE:
        return True
    if _ALL_DIGITS.match(token):
        # A long run of digits is a reference, an account or a UTR. A SHORT one
        # can be part of the name — `SUNRISE STORE 2`, `HOTEL 108`, `UNIT 7` —
        # and dropping it merges two branches of one brand into one party.
        return len(token) > 3
    if _TIMESTAMP.match(token) or _DATE.match(token):
        return True
    if _IFSC.match(token) or _MASKED.match(token) or _VPA.match(token):
        return True
    if _REF_BLOB.match(token) and not re.match(r"^[A-Z]+\d{0,2}$", token):
        # Mixed letters and digits, six or more characters. `ABC12` survives
        # (a short trade name with a number); `YESB43340764058` does not.
        return True
    return False


def _segment_score(tokens: Sequence[str]) -> float:
    """How much does this segment look like a party's name?

    Used to pick between the pieces of `NEFT-YESB4334-ACME TRADERS-YES BANK`
    without knowing that ACME is a company. Real names are made of alphabetic
    words; metadata is made of codes and banking vocabulary.
    """
    if not tokens:
        return 0.0
    alpha = [t for t in tokens if t.isalpha() and len(t) >= 2]
    if not alpha:
        return 0.0
    score = 0.0
    for t in alpha:
        if t in BANK_WORDS:
            score += 0.15          # a bank name is weak evidence of a payee
        elif t in TRADE_DESCRIPTORS or t in LEGAL_FORMS:
            score += 0.6           # part of a business name, but not its core
        else:
            score += 1.0
    # Longer alphabetic words are more name-like than two-letter fragments.
    score += 0.1 * sum(1 for t in alpha if len(t) >= 4)
    return score


def clean_narration(narration: str, *, keep_segments: int = 2) -> str:
    """STAGE 1. Strip transaction-system noise and return the party text.

        UPI UPI RAMACHANDRAKOTHARI                    -> RAMACHANDRAKOTHARI
        NEFT-YESB43340764058-RESILIENT INNOV PVT LTD  -> RESILIENT INNOV PVT LTD
        IMPS/123456/ABC TRADERS                       -> ABC TRADERS

    Works by splitting on every delimiter a bank might use, scoring each
    segment for name-likeness, and keeping the best. `keep_segments` allows a
    name that the export split across two fields to be rejoined.
    """
    folded = _fold(narration)
    if not folded.strip():
        return ""

    # `RTN:` and similar leading markers describe the transaction's fate.
    folded = re.sub(r"^\s*(?:RTN|REV|REVERSAL)\s*:\s*", "", folded)

    # Timestamps go BEFORE the string is split, because `:` is also a field
    # separator: split first and `07:22:38` becomes three innocent-looking
    # two-digit tokens that the short-number rule below is happy to keep.
    folded = re.sub(r"\b\d{1,2}[:.]\d{2}(?:[:.]\d{2})?\b", " ", folded)

    segments = [s for s in _SEPARATORS.split(folded) if s.strip()]
    if not segments:
        segments = [folded]

    scored: List[Tuple[float, int, List[str]]] = []
    for idx, seg in enumerate(segments):
        cleaned = _NON_NAME.sub(" ", seg)
        tokens = [repair_ocr(t) for t in cleaned.split() if t]
        kept = [t for t in tokens if not _token_is_noise(t)]
        # A short number is only part of a name when there is a name beside it:
        # `STORE 2` keeps its 2, a lone `24` in a reference field does not.
        if not any(t.isalpha() for t in kept):
            kept = [t for t in kept if not t.isdigit()]
        if kept:
            scored.append((_segment_score(kept), idx, kept))

    if not scored:
        return ""

    scored.sort(key=lambda x: (-x[0], x[1]))
    best_score = scored[0][0]
    # Keep the best segment, plus any other segment that is nearly as
    # name-like — a name split across two fields scores similarly in both.
    chosen = [s for s in scored[:keep_segments] if s[0] >= best_score * 0.75]
    chosen.sort(key=lambda x: x[1])                      # restore reading order

    words: List[str] = []
    for _score, _idx, tokens in chosen:
        for t in tokens:
            # A word repeated back-to-back is a bank artefact: `UPI UPI NAME`,
            # `NAME NAME`. Collapse it rather than letting it change the shape.
            if not words or words[-1] != t:
                words.append(t)

    # A trailing bank name is metadata. Removed only from the END, and only
    # while something else survives — `YES BANK` as the whole payee is a payee.
    while len(words) > 1 and words[-1] in BANK_WORDS:
        words.pop()

    # A single trailing letter is where a fixed-width export cut a word —
    # `...BOBCARD L`, `...NEW U`. It is never a name on its own.
    #
    # But a RUN of them is initials, and `CHETHAN B M` must keep its `B M` or it
    # can never be matched with `B M CHETAN`. So the length of the run decides:
    # one is damage, two or more is a name.
    run = 0
    while run < len(words) and len(words[-1 - run]) == 1 and words[-1 - run].isalpha():
        run += 1
    if run == 1 and len(words) > 1:
        words.pop()

    return " ".join(words).strip()


# ---------------------------------------------------------------------------
# STAGE 3 — legal form  (defined before Stage 2, which uses it)
# ---------------------------------------------------------------------------

_PLURAL = re.compile(r"(?<=[A-Z]{3})S$")


def singularise(token: str) -> str:
    """`ENTERPRISES` -> `ENTERPRISE`, so a plural does not split a firm.

    Applied only to words of four or more letters, because `SONS` and `BROS`
    are how those firms are actually named and `SON`/`BRO` is not an improvement.
    """
    if len(token) < 5:
        return token
    if token.endswith("IES"):
        return token[:-3] + "Y"
    if token.endswith("SS"):
        return token
    # Strip only the final S. Taking `ES` off `ENTERPRISES` gives `ENTERPRIS`,
    # which then fails to match the descriptor list and the whole
    # company-vs-person test with it.
    return _PLURAL.sub("", token) or token


# Fixed-width exports cut the suffix mid-word: `PRIVATE LI`, `LIMITE`, `PVT LT`.
# Every prefix of a known form, from two characters up, resolves to that form.
_LEGAL_PREFIXES: Dict[str, str] = {}
for _word, _canon in LEGAL_FORMS.items():
    for _i in range(2, len(_word) + 1):
        _LEGAL_PREFIXES.setdefault(_word[:_i], _canon)
# A prefix that is itself a different whole form wins as that form.
_LEGAL_PREFIXES.update(LEGAL_FORMS)


def canonical_legal_form(token: str) -> Optional[str]:
    """The incorporation wrapper this token is, including truncated spellings."""
    return _LEGAL_PREFIXES.get(token)


def legal_forms_in(tokens: Sequence[str]) -> Set[str]:
    """Which incorporation wrappers this name carries, canonicalised."""
    return {f for f in (canonical_legal_form(t) for t in tokens) if f}


def strip_legal_form(tokens: Sequence[str]) -> List[str]:
    """STAGE 3. Remove the incorporation wrapper, keep the trade descriptor.

    `ABC PVT LTD` and `ABC PRIVATE LIMITED` both reduce to `ABC`.
    `ABC BROTHERS` stays `ABC BROTHERS` and `ABC ENTERPRISES` stays
    `ABC ENTERPRISE` — they are different firms and must not both become `ABC`.

    Never returns empty: a name that is nothing but a legal form is returned
    unchanged, because an empty core matches everything.
    """
    core = [t for t in tokens if canonical_legal_form(t) is None]
    core = [singularise(t) for t in core]
    return core or list(tokens)


# ---------------------------------------------------------------------------
# STAGE 6 support — phonetic representation
# ---------------------------------------------------------------------------

# Digraphs where Indian transliteration varies most. Applied longest-first.
_PHONETIC_DIGRAPHS: Tuple[Tuple[str, str], ...] = (
    ("SCH", "S"), ("TCH", "C"), ("DGE", "J"),
    ("PH", "F"), ("GH", "G"), ("KH", "K"), ("BH", "B"), ("DH", "D"),
    ("TH", "T"), ("CH", "C"), ("SH", "S"), ("ZH", "S"), ("JH", "J"),
    ("CK", "K"), ("QU", "K"), ("WR", "R"), ("KN", "N"), ("GN", "N"),
)
_PHONETIC_SINGLES = str.maketrans({
    "Q": "K", "X": "K", "Z": "S", "C": "K", "W": "V", "Y": "I", "J": "J",
})


def indic_phonetic(word: str) -> str:
    """A sound-alike code tuned for Indian names and transliteration.

    `CHETAN`/`CHETHAN`, `EUGIN`/`EUGINE`, `SHETTY`/`SETTY`, `BHAT`/`BAT` are the
    same name spelled by different clerks. Metaphone is built for English; this
    handles the aspirated consonants (BH, DH, GH, KH, PH, TH) and the vowel
    drift that actually varies in transliteration.

    Deliberately lossy. It is ONE signal in the score, never the decision — a
    code this aggressive will collide, and the caller is expected to require
    corroboration.
    """
    s = re.sub(r"[^A-Z]", "", _fold(word))
    if not s:
        return ""
    for a, b in _PHONETIC_DIGRAPHS:
        s = s.replace(a, b)
    lead = s[0]
    s = s.translate(_PHONETIC_SINGLES)
    # Vowels carry almost no information across transliterations; keep only a
    # leading one so `AMIT` and `MITA` do not collapse together.
    body = re.sub(r"[AEIOU]", "", s[1:])
    s = (lead if lead in "AEIOU" else s[0]) + body
    out: List[str] = []
    for ch in s:
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


def char_ngrams(text: str, n: int = 3) -> FrozenSet[str]:
    """Character n-grams of the compacted string, for order-tolerant overlap."""
    s = re.sub(r"[^A-Z0-9]", "", _fold(text))
    if len(s) < n:
        return frozenset({s}) if s else frozenset()
    return frozenset(s[i:i + n] for i in range(len(s) - n + 1))


def initials(tokens: Sequence[str]) -> str:
    """`RAMACHANDRA KOTHARI` -> `RK`. Supports `B M CHETAN` vs `CHETHAN B M`."""
    return "".join(t[0] for t in tokens if t)


# ---------------------------------------------------------------------------
# Person vs company
# ---------------------------------------------------------------------------

# Words that may be shaved off the END of a concatenated name when deciding
# whether two names are the same party. NOT used for the substitution conflict:
# `ABC BROTHERS` against `ABC ENTERPRISES` must still be two firms, and that is
# a DISPUTE between two words rather than one name carrying an extra one.
# TRADE DESCRIPTORS ARE INCLUDED, and it is a judgement call worth recording.
#
# Excluding them is safer in the abstract: shaving `BROTHERS` off `ABCBROTHERS`
# gives `ABC`, which then matches `ABC PVT LTD`. But it was measured, and on the
# 500-transaction ground-truth set excluding them cost six real entities —
# `UBERTECHNOLOGY` stopped reaching `UBER`, `OLACABS` stopped reaching `OLA` —
# while gaining nothing, because false merges were already zero either way.
#
# The dangerous case is not a shaved descriptor. It is SUBSTITUTION —
# `ABC BROTHERS` against `ABC ENTERPRISES` — and `_conflicts` vetoes that
# whatever this set contains. Re-measure with `scripts/entity_resolution_report.py`
# before changing it.
_TRAILING_STRIPPABLE = (
    set(LEGAL_FORMS) | set(QUALIFIERS) | set(BANK_WORDS) | set(TRADE_DESCRIPTORS)
    | {singularise(w) for w in TRADE_DESCRIPTORS}
)
_STRIPPABLE_SORTED = sorted(_TRAILING_STRIPPABLE, key=len, reverse=True)
_MIN_CORE_CHARS = 4


def strip_trailing_known(compact: str) -> str:
    """Shave generic trailing words off a run-together name.

    `HDFCBANK` -> `HDFC`, `UBERTECHNOLOGY` -> `UBER`,
    `EMMVEEENERGYPRIVATELIMITED` -> `EMMVEEENERGY`.

    Banks write the same party with and without these, and when the name
    arrives with no spaces the token-level stripper cannot see them at all —
    which is how one company became two entities on the test set.

    Stops at `_MIN_CORE_CHARS` so a name that IS a generic word survives: a
    payee called `INDIA` must not reduce to nothing and then match everything.
    """
    out = compact
    changed = True
    while changed:
        changed = False
        for word in _STRIPPABLE_SORTED:
            if len(word) < 3 or not out.endswith(word):
                continue
            candidate = out[: -len(word)]
            if len(candidate) >= _MIN_CORE_CHARS:
                out = candidate
                changed = True
                break
    return out


def looks_like_person(tokens: Sequence[str],
                      legal_forms: Optional[Set[str]] = None) -> bool:
    """Entity-type guess from shape alone, for STAGE 9.

    A company name usually carries a legal form or a trade descriptor. A person
    is two or three plain words, often with a single-letter initial among them.
    Wrong sometimes — which is why the resolver treats the type as a weight on
    the score rather than a gate on the comparison.
    """
    if not tokens:
        return False
    if legal_forms:
        # Passed in because the caller hands us the CORE tokens, from which the
        # incorporation wrapper has already been removed. Without this a
        # `... PVT LTD` company reads as a two-word person.
        return False
    if any(canonical_legal_form(t) for t in tokens):
        return False
    descriptors = {singularise(d) for d in TRADE_DESCRIPTORS}
    if any(singularise(t) in descriptors for t in tokens):
        return False
    if len(tokens) > 4:
        return False
    return all(t.isalpha() for t in tokens)


# ---------------------------------------------------------------------------
# STAGE 2 — every representation, computed once
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Representations:
    """All normalised forms of one raw string. Computed once, compared many.

    The ORIGINAL is deliberately kept. Canonical-name selection (stage 12)
    needs to show a person something they recognise, and every other field here
    has thrown information away to make matching possible.
    """
    original: str
    cleaned: str                       # stage 1 output
    tokens: Tuple[str, ...]
    core_tokens: Tuple[str, ...]       # stage 3: legal form removed
    sorted_core: Tuple[str, ...]
    compact: str                       # core, no spaces at all
    compact_core: str                  # compact with generic trailers shaved
    lowercase: str
    initials: str
    phonetic_tokens: Tuple[str, ...]
    phonetic: str
    trigrams: FrozenSet[str] = field(default=frozenset())
    legal_forms: FrozenSet[str] = field(default=frozenset())
    is_person: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.core_tokens


def representations(raw: str, *, cleaned: Optional[str] = None) -> Representations:
    """STAGE 2. Build every form the matcher needs, from one raw string."""
    text = str(raw or "")
    clean = cleaned if cleaned is not None else clean_narration(text)
    tokens = tuple(t for t in clean.split() if t)
    core = tuple(strip_legal_form(tokens))
    compact = "".join(core)
    phon = tuple(indic_phonetic(t) for t in core if t)
    return Representations(
        original=text.strip(),
        cleaned=clean,
        tokens=tokens,
        core_tokens=core,
        sorted_core=tuple(sorted(core)),
        compact=compact,
        compact_core=strip_trailing_known(compact),
        lowercase=clean.lower(),
        initials=initials(core),
        phonetic_tokens=phon,
        phonetic="".join(phon),
        trigrams=char_ngrams(compact),
        legal_forms=frozenset(legal_forms_in(tokens)),
        is_person=looks_like_person(core, legal_forms_in(tokens)),
    )
