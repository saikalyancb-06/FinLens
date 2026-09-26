"""Counterparty extraction and normalisation.

Indian bank statements encode the *other side* of a transfer inside the
narration, wrapped in channel-specific scaffolding:

    NEFT-HDFCH25081234567-MEYER ORGANICS PVT LTD-HDFC BANK LTD.
    RTGS-HDFCR52026081112-EMMVEE ENERGY PRIVATE LIMITED
    EBANK:WIB/1501906475/JAYAPRAKASH SHETTY
    IMPS/P2A/518912345678/CASA2STAYSPRIVA/PayoutforC2S
    UPI/CR/412345678901/SHIVKUMAR VEG/YESB/paytoveg

No regex can tell you what business KUMAR FISH is in — that is knowledge only
the account holder has. What this module does is reduce all the ways the same
counterparty can appear down to ONE stable key, so the user categorises
"KUMAR FISH" once and every past and future transfer to them follows.

Two levels of key are produced:

  key       exact normalised name, used for confident auto-apply
  fuzzy_key a consonant-skeleton form that survives the spelling drift banks
            introduce (NARASIMHAIAH CHIKEN vs NARASIMHA CHIKEN), used only to
            *suggest*, never to auto-apply
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

# The OCR repair lives with the narration patterns that need it most.
# `purpose_rules` is pure and imports nothing from here, so this is safe.
from app.categorization.purpose_rules import ocr_correct

# Channel scaffolding. Ordered: the more specific pattern must win.
_CHANNEL_PATTERNS = [
    # SBI writes the rail INSIDE a transfer line and terminates with "--":
    #   BY TRANSFER-NEFT*SBIN0001234*KUMAR FISH--
    #   TO TRANSFER-INB SUSHMITHA H SHETTY--
    # Matched before the generic NEFT rule, which would otherwise never see it.
    ("neft", re.compile(
        r"^(?:TO|BY)\s+TRANSFER[-/]\s*(?:NEFT|RTGS|IMPS|INB|UPI)?[-/*\s]*"
        r"(?:[A-Z]{4}[0-9]{4,}[*\s/-]+)?(?P<rest>[A-Z][^*]*?)\s*-*$")),
    # NEFT-<ref>-NAME[-BANK], and the "NEFT DR-" / "NEFT CR-" form HDFC uses.
    # The direction token is part of the rail, not of the counterparty.
    ("neft", re.compile(
        r"^(?:NEFT|RTGS|INFT|IFT)(?:\s+(?:DR|CR))?[-/\s][A-Z0-9]{2,}[-/](?P<rest>.+)$")),
    # EBANK:WIB/<account or ref>/NAME
    ("ebank", re.compile(r"^EBANK:[A-Z]{2,}/[0-9]+/(?P<rest>.+)$")),
    # IMPS/P2A/<ref>/NAME/<remark>   IMPS/<ref>/NAME
    ("imps", re.compile(r"^IMPS[-/](?:[A-Z0-9]{2,4}[-/])?[0-9]{4,}[-/](?P<rest>.+)$")),
    # UPI/CR/<ref>/NAME/<vpa>/<remark>
    ("upi", re.compile(r"^(?:MB:)?UPI[-/](?:CR|DR|P2A|P2M)?[-/]?[0-9]{4,}[-/](?P<rest>.+)$")),
    # HDFC puts the payee FIRST: UPI-SWIGGY-SWIGGY@YBL-YESB0000262-<ref>-PAYMENT
    ("upi", re.compile(r"^UPI[-/](?P<rest>[A-Z][A-Z0-9 .&\'-]*?)[-/][A-Z0-9.@_-]+@")),
    # Kotak/others: MB:UPI/<ref>/NAME/BANK
    ("upi", re.compile(r"^MB:UPI[-/][0-9]{4,}[-/](?P<rest>.+)$")),
    # MMT/IMPS/<ref>/<remark>/NAME
    ("mmt", re.compile(r"^MMT[-/]IMPS[-/][0-9]+[-/](?P<rest>.+)$")),
    # ACH/NACH mandates: ACH-DR-<sponsor>-NAME
    ("ach", re.compile(r"^(?:ACH|NACH)[-/](?:DR|CR)[-/][A-Z0-9]+[-/](?P<rest>.+)$")),
    # EBANK:<ref>///NAME  — the intermediate fields are blank on some exports
    ("ebank", re.compile(r"^EBANK:[0-9]+/+(?P<rest>[A-Z].*)$")),
    # Card batch settlement. The acquirer's name is the LAST segment, after a
    # variable number of reference fields:
    #   BT26022051849666/ 6051910375/79707/BOBCARD LIMITE
    # Anchoring on the first segment after the slash misses it entirely — that
    # is 314 rows on a single real statement, all of them one counterparty.
    ("card", re.compile(r"^BT[0-9]{6,}/(?:[\s0-9]+/)*(?P<rest>[A-Z][A-Z0-9 .&'-]*)$")),
    # BT<ref>/MERCHANT — the simple single-segment form.
    ("card", re.compile(r"^BT[0-9]{6,}/(?P<rest>[A-Z].+)$")),
    # Card present / ATM: POS 4321XXXXXXXX9876 RELIANCE RETAIL
    ("card", re.compile(r"^POS\s+[0-9X]{8,}\s+(?P<rest>[A-Z].+)$")),
    # Biller payments: BIL/ONL/<ref>/TATA POWER/ELECTRICITY
    ("biller", re.compile(r"^BIL[-/](?:ONL|BPAY|INF)?[-/]?[0-9]+[-/](?P<rest>.+)$")),
    # ICICI card settlement: VAT/EAZYDINER PVT/<ref>
    ("card", re.compile(r"^VAT[-/](?P<rest>[A-Z][A-Z0-9 .&\'-]*)[-/][0-9]+$")),
]

# Bank names that trail a counterparty. The statement column is often truncated
# mid-word, so each alternative is matched as a PREFIX of the trailing segment.
_BANK_WORDS = [
    "STATE BANK", "CANARA BANK", "AXIS BANK", "HDFC BANK", "ICICI BANK",
    "KARNATAKA BANK", "UCO BANK", "UNION BANK", "KOTAK MAHINDRA", "KOTAK BANK",
    "IDFC FIRST", "IDFC BANK", "YES BANK", "BANK OF BARODA", "BANK OF INDIA",
    "BANK OF MAHARASHTRA", "PUNJAB NATIONAL", "PUNJAB AND SIND", "INDIAN BANK",
    "INDIAN OVERSEAS", "IDBI BANK", "BANDHAN BANK", "FEDERAL BANK", "RBL BANK",
    "INDUSIND BANK", "CENTRAL BANK", "CITY UNION", "SOUTH INDIAN BANK",
    "TAMILNAD MERCANTILE", "DBS BANK", "HSBC", "CITIBANK", "STANDARD CHARTERED",
    "AU SMALL FINANCE", "EQUITAS", "JANA SMALL", "UJJIVAN", "ESAF",
    "KARUR VYSYA", "DHANLAXMI", "CSB BANK", "NAINITAL BANK", "JAMMU AND KASHMIR",
    "PAYTM PAYMENTS", "AIRTEL PAYMENTS", "FINO PAYMENTS", "INDIA POST PAYMENTS",
]
# Longest first so "BANK OF MAHARASHTRA" is not eaten by "BANK OF".
_BANK_WORDS.sort(key=len, reverse=True)

# The four-letter IFSC prefix, which several banks print instead of the name:
# "...-BHARATPE-YESB". Matched as a WHOLE trailing token only — `SBIN` is a
# bank, but a name ending in a four-letter word must not be truncated because
# it happens to look like one. Kept separate from `_BANK_WORDS` because those
# are prefix-matched and these must not be.
_IFSC_CODES = {
    "YESB", "HDFC", "ICIC", "SBIN", "UTIB", "KKBK", "PUNB", "CNRB", "BARB",
    "IOBA", "IDIB", "MAHB", "INDB", "FDRL", "RATN", "BKID", "UBIN", "IBKL",
    "CIUB", "KVBL", "TMBL", "DCBL", "AUBL", "ESFB", "UJVN", "IDFB", "KARB",
    "SIBL", "CSBK", "JAKA", "PSIB", "UCBA", "CBIN", "ANDB", "ORBC", "VIJB",
    "SCBL", "CITI", "HSBC", "DBSS", "DEUT", "BOFA", "ABNA", "AIRP", "PYTM",
}
_IFSC_TRAILING = re.compile(
    r"\s+(?:" + "|".join(sorted(_IFSC_CODES)) + r")\s*$"
)

# Corporate suffixes carry no identity — MEYER ORGANICS PVT LTD and
# MEYER ORGANICS PRIVATE LIMITED are the same supplier.
_CORP_SUFFIX = re.compile(
    r"\b(?:PVT|PRIVATE|PUBLIC|LTD|LIMITED|LIMITE|LIMIT|LLP|LLC|INC|CORP|"
    r"COMPANY|CO|ENTERPRISES|ENTERPRISE|INDIA|IND)\b"
)

# Trailing noise banks append to a name: "NEW", "-STATE B", account fragments.
_TRAILING_NOISE = re.compile(r"\b(?:NEW|OLD|A/?C|ACCOUNT|ACC|CURRENT|SAVINGS)\b\s*$")

# The CHANNEL the payment was made through, appended after the payee by several
# banks ("...-MEYER ORGANICS PVT LTD-NETBANK"). It describes how the money moved,
# not who received it — and leaving it in splits one supplier across every
# channel the user happens to pay them through.
_TRAILING_CHANNEL = re.compile(
    r"\s+(?:NETBANK|NET\s*BANKING|MOBILE|MOB|INB|IB|ATM|BRANCH|BR|ONLINE|"
    r"NEFT|RTGS|IMPS|UPI|PAYMENT|PAYMENTS|TRANSFER|TRF|COLLECT|COLL)$"
)

# Narrations that are NOT counterparty transfers — charges, interest, cash,
# card settlements. These are the rule engine's job, not the memory's.
_NOT_A_COUNTERPARTY = re.compile(
    r"^(?:BY\s+CASH|TO\s+CASH|CASH\s*DEP(?:OSIT)?|ATM|CHQ|CHEQUE|CLG|CLEARING|"
    r"INT\.?\s*COLL|INTEREST|PENAL|SERVICE\s*CHARGE|GST|SGST|CGST|IGST|TDS|"
    r"BT\d*|SETTLEMENT|CHARGES?|MIN\s*BAL|SMS\s*CHG|AMB\s*CHG)\b"
)

# Minimum meaningful key. "SHEKHAR" (7) is real; "AB" is not.
_MIN_KEY_LEN = 3

# How many consonants of the skeleton the review queue compares. See
# `ReviewGroup.group_key` — this is what survives a truncated export.
_GROUP_SKELETON_LEN = 7


@dataclass(frozen=True)
class Counterparty:
    """A normalised counterparty extracted from one narration."""

    key: str            # exact match key, e.g. "MEYER ORGANICS"
    display: str        # human-facing name, e.g. "Meyer Organics Pvt Ltd"
    fuzzy_key: str      # consonant skeleton for suggestion-only matching
    channel: str        # neft | ebank | imps | upi | mmt | ach | plain

    @property
    def is_usable(self) -> bool:
        """Long enough, and actually a name.

        THE 216-QUESTION BUG. A real statement produced counterparties called
        `07:22:38`, `08:56:10`, `00:09:30` — clock times, one per row, 216 of
        them. The channel patterns matched, handed the timestamp over as the
        payee segment, and `_normalise` turned `07:22:38` into the perfectly
        valid-looking key `07 22 38`. Every one became its own question about
        its own single row.

        The bare-narration branch of `extract` already demanded a real word
        before it would key on anything; the channel branches did not, which is
        the whole gap. A counterparty is a NAME. If there is no run of three
        letters in it, there is no name in it, and no amount of scaffolding
        around it makes a reference number into a party.
        """
        if len(self.key) < _MIN_KEY_LEN:
            return False
        return bool(re.search(r"[A-Z]{3,}", self.key))


def _strip_trailing_bank(text: str) -> str:
    """Remove a trailing bank name, tolerating mid-word truncation.

    Statement exports frequently cut the narration at a fixed width, leaving
    "-CANARA BA" or "-STATE B". Matching the bank list as a prefix of the
    trailing segment handles both the complete and the truncated form.
    """
    for sep in ("-", "/"):
        idx = text.rfind(sep)
        while idx > 0:
            tail = text[idx + 1:].strip()
            tail_cmp = re.sub(r"[^A-Z ]", " ", tail)
            tail_cmp = re.sub(r"\s+", " ", tail_cmp).strip()
            for bank in _BANK_WORDS:
                # complete ("CANARA BANK") or truncated ("CANARA BA", "CANARA")
                if tail_cmp.startswith(bank) or (
                    len(tail_cmp) >= 3 and bank.startswith(tail_cmp)
                ):
                    return text[:idx]
            idx = text.rfind(sep, 0, idx)
    return text


def _normalise(name: str) -> str:
    s = name.upper()
    s = re.sub(r"[^A-Z0-9& ]", " ", s)
    s = _CORP_SUFFIX.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Fixed-width exports truncate suffixes mid-word: "PRIVATE LI", "LIMITE".
    # A trailing fragment that is a prefix of a known suffix is that suffix.
    s = re.sub(
        r"\s+(?:" + "|".join(
            w[:i] for w in ("LIMITED", "PRIVATE", "ENTERPRISES")
            for i in range(2, len(w))
        ) + r")$",
        "", s,
    )
    # A long digit run is a terminal id, an account number or a UTR, never part
    # of a name. `BHARATPE 92293449100000` and `BHARATPE` are one party and were
    # two questions; on the real statement that pair alone was 56 rows.
    s = re.sub(r"\b\d{5,}\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # `BHARATPE YESB` and `BHARATPE` are one party. Repeated because some
    # exports print both the code and a trailing channel word.
    for _ in range(2):
        trimmed = _IFSC_TRAILING.sub("", s).strip()
        if trimmed == s or not trimmed:
            break
        s = trimmed
    s = _TRAILING_NOISE.sub("", s).strip()
    # Repeated: "…-MEYER ORGANICS PVT LTD-NETBANK PAYMENT" has two of them.
    for _ in range(3):
        stripped = _TRAILING_CHANNEL.sub("", s).strip()
        if stripped == s:
            break
        # Never strip away the whole name — "PAYMENT" alone as a counterparty is
        # useless, but so is an empty key.
        if not stripped:
            break
        s = stripped
    s = re.sub(r"\s+", " ", s)
    return s


def _fuzzy(key: str) -> str:
    """Consonant skeleton: collapses spelling drift in transliterated names.

    NARASIMHAIAH -> NRSMH, NARASIMHA -> NRSMH. Vowels are where Indian-name
    transliteration varies most, and doubled consonants are normalised too.

    An H that follows another consonant goes as well, because that is the other
    place transliteration wobbles: SHEKHAR and SHEKAR are one person, and so are
    SHETTY and SETTY, BHAT and BAT, JAYAPRAKASH and JAYAPRAKAS. Only a leading H
    survives, where it is carrying a sound of its own rather than modifying the
    consonant before it.
    """
    s = re.sub(r"[^A-Z]", "", key)
    s = re.sub(r"[AEIOU]", "", s)
    out = []
    for ch in s:
        if ch == "H" and out:
            continue
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)[:12]


def extract(narration: str) -> Optional[Counterparty]:
    """Pull the counterparty out of a bank narration.

    Returns None when the narration is not a counterparty transfer (a bank
    charge, cash movement, card settlement) or when nothing usable survives
    normalisation — in both cases the caller should fall through to the rule
    engine rather than inventing a key.
    """
    if not narration:
        return None

    # Repair OCR damage BEFORE building the key, or the same party splits into
    # several. On one real statement `SUSHMITHA H SHETTY` arrived as both
    # `5U5HM1THA H SHETTY` and `SUSHMITHA H SHETTY`, and one supplier appeared
    # as nine separate groups — `NARA MHA AH CHIKEN`, `NAR MHA CHIKEN`,
    # `NARASIMHA CHIKEN` and so on. Every fragment is a separate question for
    # the user, about a party they already answered for.
    #
    # It also feeds the trade rules: `MARUTH1 PRO STORE` is not recognisable as
    # Maruthi until the 1 becomes an I.
    raw = re.sub(r"\s+", " ", ocr_correct(str(narration))).strip().upper()
    if not raw:
        return None

    # "RTN:" marks a returned/reversed transfer; the counterparty is unchanged.
    raw = re.sub(r"^RTN:\s*", "", raw)

    if _NOT_A_COUNTERPARTY.match(raw) and not re.match(r"^BT[0-9]{6,}/", raw):
        return None

    channel = "plain"
    rest = raw
    for name, pattern in _CHANNEL_PATTERNS:
        m = pattern.match(raw)
        if m:
            channel = name
            rest = m.group("rest")
            break
    else:
        # No channel scaffolding recognised. Only treat the whole narration as
        # a counterparty when it looks like a bare name — otherwise we would
        # key on free-text remarks and pollute the memory.
        if not re.fullmatch(r"[A-Z][A-Z0-9 .&'-]{2,}", raw):
            return None
        # A bare narration must read like a name: at least one purely
        # alphabetic word of 3+ letters. "BT12" or "AC/1234" is a reference,
        # not a counterparty, and must not become a memory key.
        if not re.search(r"\b[A-Z]{3,}\b", raw):
            return None

    rest = _strip_trailing_bank(rest)

    # A 1-2 character trailing segment is always a truncated bank name
    # ("...NEW-U" for UCO BANK), never a counterparty. Drop it so the key does
    # not depend on where the export happened to cut the line.
    rest = re.sub(r"[-/][A-Z]{1,2}\s*$", "", rest)

    # IMPS/UPI put the remark after the name: NAME/PayoutforC2S. Keep the
    # first segment, which is the party.
    if channel in ("imps", "upi", "mmt", "biller"):
        rest = rest.split("/")[0]

    # The display drops the same reference noise the key does. Showing
    # `Bharatpe 92293449100000` next to `Bharatpe` reads as two parties even
    # after they have been merged into one group.
    display = re.sub(r"\b\d{5,}\b", " ", rest.replace("-", " "))
    display = re.sub(r"\s+", " ", display).strip().title()
    key = _normalise(rest)

    cp = Counterparty(
        key=key, display=display or key, fuzzy_key=_fuzzy(key), channel=channel
    )
    return cp if cp.is_usable else None


def extract_key(narration: str) -> Optional[str]:
    """Convenience wrapper returning just the exact key, or None."""
    cp = extract(narration)
    return cp.key if cp else None


# ---------------------------------------------------------------------------
# Narration-shape grouping
#
# Not every transaction has a counterparty. A bank charge, POS terminal rent,
# interest collected, a cash deposit — the money did not go TO anyone the user
# trades with, so `extract` correctly returns None for all of them.
#
# But those rows still need categorising, and on a real statement there are a
# lot of them: 695 of 1,823. Sending them to one-by-one review means asking the
# same question 273 times for 273 identical "Charges for PORD Customer Payment"
# rows that differ only by a reference number.
#
# So when there is no counterparty, group by the SHAPE of the narration instead.
# The reference numbers, dates and month codes that make each row unique are
# exactly the parts that carry no meaning; strip them and what remains is the
# kind of charge, which is the thing the user actually decides about.
# ---------------------------------------------------------------------------

_MONTHS = (
    "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC|"
    "JANUARY|FEBRUARY|MARCH|APRIL|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|"
    "NOVEMBER|DECEMBER"
)

# Ordered. Dates must be collapsed before bare digit runs, or "01-03-2026"
# becomes "#-#-#" and stops matching "01/03/26".
_SHAPE_SUBSTITUTIONS = [
    # 01-03-2026, 01/03/26, 2026-03-01
    (re.compile(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b"), " "),
    # FEB26, SEPT25, JAN2026 — month-year codes in POS rent and charge lines
    (re.compile(r"\b(?:" + _MONTHS + r")\s*\d{2,4}\b"), " "),
    # A bare month name on its own
    (re.compile(r"\b(?:" + _MONTHS + r")\b"), " "),
    # Any run of digits: reference numbers, terminal IDs, account fragments
    (re.compile(r"\d+"), " "),
    # Masked fields
    (re.compile(r"X{3,}"), " "),
]

# Tokens that survive digit-stripping but identify nothing: leftovers of
# reference formats and connective words.
_SHAPE_NOISE = {
    "TO", "FOR", "AT", "OF", "ON", "THE", "AND", "FROM", "BY", "WITH",
    "REF", "NO", "TXN", "TRN", "ID", "A", "AC", "CHG", "CHGS",
    # Field LABELS, not values. "POSRENT_JUN25_TID_65100728" and
    # "POSRENT_JUL25_65100728" are the same monthly charge; keeping TID splits
    # one group in two purely on whether the bank printed the label that month.
    "TID", "MID", "VPA", "UTR", "RRN", "SEQ", "BRN", "BRNCH",
}

_SHAPE_MAX_TOKENS = 6
_SHAPE_MIN_TOKENS = 1


def shape_key(narration: str) -> Optional[str]:
    """A stable key for narrations that name no counterparty.

    Returns None when nothing meaningful survives — a narration that is only a
    reference number ("A00021802260044311") has no shape to group on, and
    inventing one would put unrelated rows in the same bucket.
    """
    if not narration:
        return None

    # Repaired first, for the same reason as `extract`: a shape group keyed
    # on damaged text splits one charge into several, and the user is asked
    # about each fragment separately.
    s = re.sub(r"\s+", " ", ocr_correct(str(narration))).strip().upper()
    # Underscore is a WORD character to the regex engine, so "_JUN25_" has no
    # word boundary in front of it and the month-code rule below silently fails.
    # POSRENT_JUN25_TID then splits into a separate group from POSRENT_JUL25_TID
    # — one charge appearing as twelve. Normalise separators first.
    s = re.sub(r"[_]+", " ", s)
    for pattern, replacement in _SHAPE_SUBSTITUTIONS:
        s = pattern.sub(replacement, s)

    # Punctuation is formatting, not meaning: "Int.Coll:" and "INT COLL" are the
    # same charge written two ways.
    s = re.sub(r"[^A-Z ]", " ", s)
    tokens = [t for t in s.split() if t and t not in _SHAPE_NOISE and len(t) > 1]

    if len(tokens) < _SHAPE_MIN_TOKENS:
        return None
    return " ".join(tokens[:_SHAPE_MAX_TOKENS])


def shape_label(narration: str) -> Optional[str]:
    """Human-facing name for a shape group, e.g. 'Posrent'."""
    key = shape_key(narration)
    if not key:
        return None
    return key.title()


@dataclass(frozen=True)
class ReviewGroup:
    """How one transaction should be grouped for review.

    `kind` is 'counterparty' when the row names someone the user trades with,
    and 'pattern' when it does not and is grouped by narration shape instead.
    The distinction is shown in the UI, because "categorise everything paid to
    KUMAR FISH" and "categorise everything that looks like a POS rent charge"
    are different kinds of decision and deserve different scrutiny.
    """

    kind: str
    key: str
    display: str
    fuzzy_key: Optional[str] = None
    channel: str = "plain"

    @property
    def group_key(self) -> str:
        """The key the REVIEW QUEUE collapses on — spelling variants included.

        `key` stays exact, because it is what a decision is stored against.
        This is looser on purpose: one person arrives as `SUSHMITHA H SHETTY`,
        `SUSHMITA H SHETTY` and `SUSHMITHA SHETTY`, and asking about them three
        times is three chances for someone to give up on the queue.

        Nothing is booked from this. The user still sees every narration in the
        group and makes one decision, so a wrong merge is visible before it
        costs anything — which is why grouping may be looser than the
        auto-apply that happens on later uploads.

        Short skeletons fall back to the exact key. `ABC` reduces to `BC`, and
        two letters will collide with parties that have nothing to do with each
        other.

        Truncated to a PREFIX, because fixed-width statement exports cut names
        mid-word and the two halves then look like two companies:
        `RESILIENTINNOVA` (skeleton `RSLNTNV`) and `RESILIENT INNOVATIONS
        PRIVAT` (skeleton `RSLNTNVTNS`) were 36 rows and 15 rows, asked
        separately, about the same firm. Comparing only the first
        `_GROUP_SKELETON_LEN` consonants folds them together. Seven is enough
        that unrelated parties do not collide and short enough to survive the
        cut — and any over-merge is visible in `also_known_as` before anyone
        acts on it.
        """
        if self.kind == "counterparty" and self.fuzzy_key and len(self.fuzzy_key) >= 5:
            return self.fuzzy_key[:_GROUP_SKELETON_LEN]
        return self.key


def group_for(narration: str) -> Optional[ReviewGroup]:
    """Group one narration for review — by counterparty first, shape second.

    Returns None only when neither is available, which on a real statement is a
    handful of rows that are nothing but a reference number.
    """
    cp = extract(narration)
    if cp:
        return ReviewGroup(
            kind="counterparty", key=cp.key, display=cp.display,
            fuzzy_key=cp.fuzzy_key, channel=cp.channel,
        )

    key = shape_key(narration)
    if key:
        return ReviewGroup(kind="pattern", key=key, display=key.title())
    return None


# ---------------------------------------------------------------------------
# Grouping health
#
# WHY THIS EXISTS. Coverage is a misleading number on its own. Deleting every
# bank-specific channel pattern from this module leaves coverage of a real
# 1,823-row statement at 99.9%, because the shape fallback catches whatever the
# counterparty patterns miss. Coverage is therefore NOT evidence that this works
# on an unfamiliar bank — it is carried by format-agnostic machinery and would
# read 99.9% even if every pattern here were wrong.
#
# What degrades on an unfamiliar format is the QUALITY of the grouping, and it
# degrades silently:
#
#   NEFT-HDFCH25081234567-MEYER ORGANICS PVT LTD-HDFC BANK LTD.
#
# If the NEFT pattern fires, this groups as the party MEYER ORGANICS, and every
# payment to them lands together whatever rail was used. If it does not fire,
# the shape fallback still groups the row — as "NEFT HDFCH MEYER ORGANICS HDFC
# BANK" — so coverage is unaffected, but the same supplier now splits across
# every bank and rail the user pays them through, and the user is asked about
# them several times instead of once.
#
# So these checks look for the fingerprints of a MISSED counterparty pattern
# rather than for missing coverage: a shape key that still contains a payment
# rail, a bank reference code, or a name-like body. A statement that trips them
# is one this module does not properly understand, and the user is told so
# rather than being handed quietly bad groups.
# ---------------------------------------------------------------------------

_RAIL_TOKENS = {
    "NEFT", "RTGS", "IMPS", "UPI", "INFT", "IFT", "ACH", "NACH", "EBANK",
    "MMT", "TRANSFER", "INB", "WIB", "P2A", "P2M", "BIL", "POS", "ATW",
}

# A bank reference code: a long alphanumeric token, or a 4-letter IFSC-style
# bank prefix. These are unique per transaction, so a shape key containing one
# is a key that will never match a second row.
_BANK_REF = re.compile(r"^[A-Z]{3,5}[A-Z0-9]*$")


@dataclass(frozen=True)
class GroupingHealth:
    """How well this module understood one batch of narrations."""

    total: int
    grouped: int
    by_counterparty: int
    by_shape: int
    ungrouped: int
    # Shape groups whose key still smells of an unparsed counterparty line.
    suspect_keys: List[str] = field(default_factory=list)
    suspect_rows: int = 0

    @property
    def coverage(self) -> float:
        return self.grouped / self.total if self.total else 0.0

    @property
    def counterparty_share(self) -> float:
        """Of grouped rows, how many were understood as a real party."""
        return self.by_counterparty / self.grouped if self.grouped else 0.0

    @property
    def suspect_share(self) -> float:
        return self.suspect_rows / self.total if self.total else 0.0

    @property
    def is_healthy(self) -> bool:
        """False when this looks like a format the module does not understand.

        Deliberately NOT based on coverage, which stays high regardless.
        """
        return self.coverage >= 0.95 and self.suspect_share <= 0.10

    def as_dict(self) -> Dict[str, object]:
        return {
            "total": self.total,
            "grouped": self.grouped,
            "coverage": round(self.coverage, 4),
            "by_counterparty": self.by_counterparty,
            "by_shape": self.by_shape,
            "ungrouped": self.ungrouped,
            "counterparty_share": round(self.counterparty_share, 4),
            "suspect_rows": self.suspect_rows,
            "suspect_share": round(self.suspect_share, 4),
            "suspect_keys": self.suspect_keys[:20],
            "is_healthy": self.is_healthy,
            "warning": None if self.is_healthy else (
                "Some transfers are being grouped by narration text rather than "
                "by who was paid, which means this statement's format is only "
                "partly understood. Grouping still works, but the same party may "
                "appear more than once."
            ),
        }


# Vocabulary of things banks CHARGE FOR. Small, and stable across banks and
# countries in a way that rail names are not — a bank invents its own transfer
# prefix but everyone calls a fee a fee.
#
# The point of this list is what it EXCLUDES: a shape key containing any of
# these is a charge template, which is exactly what shape grouping is for and
# must not be reported as a problem.
_CHARGE_VOCABULARY = {
    "CHARGE", "CHARGES", "CHG", "CHGS", "FEE", "FEES", "COMMISSION", "COMM",
    "INT", "INTEREST", "PENAL", "PENALTY", "FINE", "TAX", "GST", "CGST",
    "SGST", "IGST", "TDS", "TCS", "CBDT", "TIN", "DUTY", "CESS", "LEVY",
    "RENT", "RENTAL", "MAINTENANCE", "AMB", "MIN", "BAL", "BALANCE", "SMS",
    "ALERT", "ANNUAL", "MONTHLY", "QUARTERLY", "YEARLY", "SERVICE", "PROCESSING",
    "RETURN", "RETURNED", "BOUNCE", "FOLIO", "LEDGER", "STATEMENT", "CHEQUE",
    "CHQ", "CASH", "HANDLING", "WITHDRAWAL", "DEPOSIT", "INSURANCE", "PREMIUM",
    "SUBSCRIPTION", "RENEWAL", "REVERSAL", "REFUND", "ROUNDING", "ADJUSTMENT",
    "INSPECTION", "STAMP", "COURIER", "POSTAGE", "LOCKER", "SAFE", "CUSTODY",
}


def _is_charge_like(tokens: List[str]) -> bool:
    """Does this key describe a charge rather than a payee?

    Substring match, not equality: banks concatenate ("POSRENT", "SMSCHG") and a
    key that is one squashed word still describes a fee.
    """
    for token in tokens:
        if token in _CHARGE_VOCABULARY:
            return True
        for word in _CHARGE_VOCABULARY:
            if len(word) >= 4 and word in token:
                return True
    return False


def _looks_like_missed_counterparty(key: str) -> bool:
    """Does this shape key look like a transfer line that should have parsed?

    Deliberately NOT dependent on recognising the rail. An earlier version
    required a token from a hardcoded rail list, which made the check as
    format-specific as the thing it was supposed to audit: a bank using its own
    prefix ("TRF~000123~ACME INDUSTRIES LLP") passed as healthy while every one
    of its counterparties was being grouped by narration text.

    The list-free signal is the SHAPE of what is left. A charge template is
    short and made of fee vocabulary; a missed payee line is longer and made of
    words that are somebody's name.
    """
    tokens = key.split()
    if not tokens:
        return False

    # A charge is a charge, whatever rail-looking prefix precedes it.
    if _is_charge_like(tokens):
        return False

    body = [t for t in tokens if t not in _RAIL_TOKENS]
    if not body:
        return False

    # A known rail plus a name is the clearest case.
    if any(t in _RAIL_TOKENS for t in tokens) and body:
        return True

    # No recognised rail: fall back to shape. Two or more non-fee words is a
    # name ("ACME INDUSTRIES LLP"), not a fee description.
    if len(body) >= 2:
        return True

    # A single long word that is not fee vocabulary is more likely a company.
    return len(body) == 1 and len(body[0]) >= 8 and not _BANK_REF.match(body[0])


def grouping_health(narrations: Iterable[str]) -> GroupingHealth:
    """Assess how well a batch of narrations was understood.

    Call this on ingestion so an unfamiliar bank surfaces as a warning at the
    moment it arrives, rather than as a user wondering why one supplier appears
    in their review queue four times.
    """
    rows = [n for n in narrations if n and str(n).strip()]
    total = len(rows)
    by_counterparty = by_shape = ungrouped = 0
    suspect_rows = 0
    suspect_keys: Dict[str, int] = {}

    for narration in rows:
        group = group_for(narration)
        if group is None:
            ungrouped += 1
            continue
        if group.kind == "counterparty":
            by_counterparty += 1
            continue

        by_shape += 1
        if _looks_like_missed_counterparty(group.key):
            suspect_rows += 1
            suspect_keys[group.key] = suspect_keys.get(group.key, 0) + 1

    ordered = sorted(suspect_keys.items(), key=lambda kv: -kv[1])
    return GroupingHealth(
        total=total,
        grouped=by_counterparty + by_shape,
        by_counterparty=by_counterparty,
        by_shape=by_shape,
        ungrouped=ungrouped,
        suspect_keys=[k for k, _ in ordered],
        suspect_rows=suspect_rows,
    )
