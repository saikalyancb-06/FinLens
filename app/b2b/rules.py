"""Caller-supplied classification rules: the schema, its validation, and the
evaluator that applies it.

WHY THIS IS NOT `app/categorization/rule_engine.py`
---------------------------------------------------
That engine owns *our* rules — a curated config of merchants, phrases and tiers
maintained in this repository, emitting *our* taxonomy. It is the right engine
for our own ledger and the wrong one here, because this module answers a
different question: **the caller sends the rules**, and they are not ours to
validate against a vocabulary we chose.

So the two share a matching *philosophy* and no data:

* Ranked, never first-match. Every rule is evaluated and the highest priority
  wins. First-match makes a ruleset order-dependent in a way its author cannot
  see, and it means inserting a rule at the top silently changes the meaning of
  every rule below it.
* Explainable. Every classified row names the rule that decided it and the
  terms that fired, because a caller iterating on a ruleset needs to know *why*
  a row landed where it did, not just where.
* A term matched inside a concatenated token counts. Banks strip separators
  (`UPI-NAMMAYATRI`, `TATAPOWERBILLPAYMENT`), and a rule author writing
  `NAMMAYATRI` means that row.

WHAT A RULE MAY AND MAY NOT DO
------------------------------
A rule may assign a category and stamp descriptive fields (`category_path`,
`counterparty`, `merchant`, `flow_type`, `event_type`, `transaction_method`,
`tags`).

A rule may **never** alter money, dates or direction. Those are facts read off
the statement, and a classification service that let a rule rewrite an amount
would be able to return a number that appears on no document. The whitelist in
`SETTABLE_FIELDS` is the enforcement, and it is deliberately narrow.

CATEGORY NAMES ARE OPAQUE
-------------------------
Categories are echoed back exactly as supplied and are never checked against
`app/categorization/taxonomy.py`. The calling product has its own vocabulary;
forcing ours on it would make the service useless to it. The only constraint is
a length cap.

ON CALLER-SUPPLIED REGEX
------------------------
`regex` is compiled with Python's `re`, which has no execution timeout, so a
pathological pattern can burn CPU (ReDoS). This is bounded, not solved:
pattern length, rule count and the span of text a regex is run against are all
capped below. That is proportionate for a first-party integration and it is
stated plainly rather than implied to be safe — see `docs/CLASSIFY_API.md`.
"""
from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from app.b2b import errors
from app.b2b.errors import ApiError
from app.categorization.normalizer import normalize_narration, tokenize_narration

# --------------------------------------------------------------------- limits
#
# Every one of these exists to bound the work a single request can ask for. They
# are generous for a real ruleset and small enough that a hostile one cannot
# pin a worker.

MAX_RULES = 1000
MAX_RULES_JSON_BYTES = 512 * 1024         # 512 KB of JSON
#
# Deliberately UNDER Starlette's own per-part multipart cap, which is exactly
# 1 MB (`starlette.formparsers.MultiPartParser.max_part_size`). At 1 MB this
# check was unreachable: Starlette refused the field first, with a bare 400 and
# no indication that the ruleset was the oversized part. Sitting below it means
# the caller gets RULES_TOO_LARGE and the actual limit instead. A 1000-rule
# ruleset is a small fraction of this.
MAX_TERMS_PER_CLAUSE = 200
MAX_TERM_LENGTH = 200
MAX_REGEX_LENGTH = 500
MAX_CATEGORY_LENGTH = 200
MAX_RULE_ID_LENGTH = 120
MAX_TAGS_PER_RULE = 20

#: Regexes run against at most this many characters of a narration. A bank
#: narration is tens of characters; this is headroom, and it caps the input size
#: a backtracking pattern can be fed.
REGEX_SCAN_CHARS = 2000

#: A term shorter than this is not looked for inside a concatenated token: a
#: three-letter term would hit inside unrelated words constantly.
MIN_EMBEDDED_TERM_LENGTH = 5

#: Default priority when a rule does not state one.
DEFAULT_PRIORITY = 50

#: Fields a rule's `set` block may write. Nothing about money, dates or
#: direction appears here, and nothing may be added to it without a reason that
#: survives the argument in this module's docstring.
SETTABLE_FIELDS: Tuple[str, ...] = (
    "category_path",
    "counterparty",
    "merchant",
    "flow_type",
    "event_type",
    "transaction_method",
    "tags",
)

#: What `fallback` may be set to.
FALLBACK_NONE = "none"
FALLBACK_BUILTIN = "builtin"
FALLBACK_MODES = (FALLBACK_NONE, FALLBACK_BUILTIN)

#: How a row's category was decided. Reported per row so a caller can always
#: tell its own ruleset's output apart from ours.
METHOD_RULE = "rule"            # one of the caller's rules matched
METHOD_BUILTIN = "builtin"      # our own classifier, because fallback=builtin
METHOD_DEFAULT = "default"      # no rule matched; default_category applied
METHOD_NONE = "none"            # no rule matched and no default was supplied

_ALLOWED_RULE_KEYS = {"id", "category", "priority", "match", "set", "stop", "catch_all"}
_ALLOWED_MATCH_KEYS = {
    "any_of", "all_of", "none_of", "regex", "regex_flags",
    "direction", "min_amount", "max_amount", "min_date", "max_date",
}
_ALLOWED_TOP_KEYS = {"version", "rules", "default_category", "fallback"}

_DIRECTIONS = {"debit", "credit"}


# --------------------------------------------------------------- value parsing

def _fail(message: str, **detail: Any) -> "ApiError":
    """An INVALID_RULES error. Always names where in the ruleset the fault is."""
    return ApiError(errors.INVALID_RULES, message,
                    detail={k: v for k, v in detail.items() if v is not None})


def _to_paise(value: Any, *, where: str, field_name: str) -> int:
    """Major units to integer minor units, for a threshold.

    Deliberately NOT `app.b2b.canonical.to_minor`: that function maps zero to
    None, which is right for an amount column (an empty debit is not a debit of
    nothing) and wrong for a threshold, where `min_amount: 0` is a meaningful
    bound that must survive as 0.
    """
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise _fail(f"'{field_name}' must be a number.",
                    rule=where, field=field_name, received=repr(value))
    if d < 0:
        raise _fail(f"'{field_name}' must not be negative; amounts are compared "
                    f"as magnitudes and direction is matched separately with "
                    f"'direction'.",
                    rule=where, field=field_name, received=str(value))
    return int((d * 100).to_integral_value(rounding="ROUND_HALF_UP"))


def _to_date(value: Any, *, where: str, field_name: str) -> datetime.date:
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        raise _fail(f"'{field_name}' must be an ISO date (YYYY-MM-DD).",
                    rule=where, field=field_name, received=repr(value))


def _to_terms(value: Any, *, where: str, field_name: str) -> Tuple[str, ...]:
    """A clause's term list, normalised to uppercase and de-duplicated.

    A bare string is accepted as a one-element list: `"any_of": "SWIGGY"` is
    what people write, and rejecting it would be pedantry rather than safety.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise _fail(f"'{field_name}' must be a string or a list of strings.",
                    rule=where, field=field_name)
    if len(value) > MAX_TERMS_PER_CLAUSE:
        raise _fail(f"'{field_name}' holds {len(value)} terms; the limit is "
                    f"{MAX_TERMS_PER_CLAUSE}.",
                    rule=where, field=field_name)

    out: List[str] = []
    seen: Set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise _fail(f"every entry in '{field_name}' must be a string.",
                        rule=where, field=field_name, received=repr(raw))
        term = raw.strip().upper()
        if not term:
            # An empty term matches everything, which is never what the author
            # meant and would silently widen the rule to a catch-all.
            raise _fail(f"'{field_name}' contains an empty term.",
                        rule=where, field=field_name)
        if len(term) > MAX_TERM_LENGTH:
            raise _fail(f"a term in '{field_name}' exceeds {MAX_TERM_LENGTH} "
                        f"characters.", rule=where, field=field_name)
        if term not in seen:
            seen.add(term)
            out.append(term)
    return tuple(out)


def _compile_regex(pattern: Any, flags_raw: Any, *, where: str) -> "re.Pattern[str]":
    if not isinstance(pattern, str):
        raise _fail("'regex' must be a string.", rule=where, field="regex")
    if len(pattern) > MAX_REGEX_LENGTH:
        raise _fail(f"'regex' exceeds {MAX_REGEX_LENGTH} characters.",
                    rule=where, field="regex")

    flags = re.IGNORECASE          # case-insensitive by default, like every
                                   # other matcher here
    if flags_raw is not None:
        if not isinstance(flags_raw, str):
            raise _fail("'regex_flags' must be a string, e.g. 'i' or 'is'.",
                        rule=where, field="regex_flags")
        mapping = {"i": re.IGNORECASE, "s": re.DOTALL, "m": re.MULTILINE,
                   "x": re.VERBOSE}
        flags = 0
        for ch in flags_raw.strip().lower():
            if ch not in mapping:
                raise _fail(f"unknown regex flag '{ch}'; supported flags are "
                            f"i, s, m, x.", rule=where, field="regex_flags")
            flags |= mapping[ch]

    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        # The caller wrote the pattern, so the compiler's complaint about it is
        # theirs to see — this is one of the few places where echoing a library
        # message outward is the helpful thing rather than a leak.
        raise _fail(f"'regex' is not a valid regular expression: {exc}",
                    rule=where, field="regex")


# ------------------------------------------------------------------ the schema

@dataclass(frozen=True)
class MatchSpec:
    """The conditions on one rule. Every condition present must hold (AND)."""

    any_of: Tuple[str, ...] = ()
    all_of: Tuple[str, ...] = ()
    none_of: Tuple[str, ...] = ()
    regex: Optional["re.Pattern[str]"] = None
    direction: Optional[str] = None
    min_amount_paise: Optional[int] = None
    max_amount_paise: Optional[int] = None
    min_date: Optional[datetime.date] = None
    max_date: Optional[datetime.date] = None

    @property
    def is_empty(self) -> bool:
        return not any((self.any_of, self.all_of, self.none_of, self.regex,
                        self.direction,
                        self.min_amount_paise is not None,
                        self.max_amount_paise is not None,
                        self.min_date, self.max_date))


@dataclass(frozen=True)
class Rule:
    """One caller rule, validated and ready to evaluate."""

    index: int                       # position as supplied; the tie-break
    rule_id: str
    category: str
    priority: int
    match: MatchSpec
    assign: Dict[str, Any] = field(default_factory=dict)
    catch_all: bool = False
    stop: bool = False


@dataclass(frozen=True)
class RuleSet:
    """A whole validated ruleset."""

    rules: Tuple[Rule, ...]
    default_category: Optional[str] = None
    fallback: str = FALLBACK_NONE
    version: Optional[str] = None

    @property
    def rule_ids(self) -> Tuple[str, ...]:
        return tuple(r.rule_id for r in self.rules)


# ------------------------------------------------------------------ validation

def _parse_match(raw: Any, *, where: str) -> MatchSpec:
    if raw is None:
        return MatchSpec()
    if not isinstance(raw, dict):
        raise _fail("'match' must be an object.", rule=where, field="match")

    unknown = set(raw) - _ALLOWED_MATCH_KEYS
    if unknown:
        # Strict, on purpose. A typo'd condition key under a lenient parser
        # produces a rule that looks specific and matches far more than its
        # author intended — the worst possible failure for a classifier, since
        # the output looks plausible.
        raise _fail(
            f"unknown key(s) in 'match': {', '.join(sorted(unknown))}. "
            f"Supported: {', '.join(sorted(_ALLOWED_MATCH_KEYS))}.",
            rule=where, unknown_keys=sorted(unknown))

    direction = raw.get("direction")
    if direction is not None:
        if not isinstance(direction, str) or direction.strip().lower() not in _DIRECTIONS:
            raise _fail("'direction' must be 'debit' or 'credit'.",
                        rule=where, field="direction", received=repr(direction))
        direction = direction.strip().lower()

    min_p = (_to_paise(raw["min_amount"], where=where, field_name="min_amount")
             if raw.get("min_amount") is not None else None)
    max_p = (_to_paise(raw["max_amount"], where=where, field_name="max_amount")
             if raw.get("max_amount") is not None else None)
    if min_p is not None and max_p is not None and min_p > max_p:
        raise _fail("'min_amount' is greater than 'max_amount', so this rule "
                    "can never match.", rule=where)

    min_d = (_to_date(raw["min_date"], where=where, field_name="min_date")
             if raw.get("min_date") is not None else None)
    max_d = (_to_date(raw["max_date"], where=where, field_name="max_date")
             if raw.get("max_date") is not None else None)
    if min_d and max_d and min_d > max_d:
        raise _fail("'min_date' is after 'max_date', so this rule can never "
                    "match.", rule=where)

    compiled = (_compile_regex(raw.get("regex"), raw.get("regex_flags"), where=where)
                if raw.get("regex") is not None else None)
    if raw.get("regex_flags") is not None and raw.get("regex") is None:
        raise _fail("'regex_flags' was supplied without 'regex'.",
                    rule=where, field="regex_flags")

    return MatchSpec(
        any_of=_to_terms(raw.get("any_of"), where=where, field_name="any_of"),
        all_of=_to_terms(raw.get("all_of"), where=where, field_name="all_of"),
        none_of=_to_terms(raw.get("none_of"), where=where, field_name="none_of"),
        regex=compiled,
        direction=direction,
        min_amount_paise=min_p,
        max_amount_paise=max_p,
        min_date=min_d,
        max_date=max_d,
    )


def _parse_assign(raw: Any, *, where: str) -> Dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _fail("'set' must be an object.", rule=where, field="set")

    unknown = set(raw) - set(SETTABLE_FIELDS)
    if unknown:
        raise _fail(
            f"'set' may not write: {', '.join(sorted(unknown))}. Settable "
            f"fields are {', '.join(SETTABLE_FIELDS)}. Amounts, dates, balances "
            f"and direction are read from the statement and cannot be "
            f"overridden by a rule.",
            rule=where, unknown_keys=sorted(unknown))

    out: Dict[str, Any] = {}
    for key, value in raw.items():
        if key == "tags":
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, (list, tuple)):
                raise _fail("'tags' must be a string or a list of strings.",
                            rule=where, field="tags")
            if len(value) > MAX_TAGS_PER_RULE:
                raise _fail(f"'tags' holds more than {MAX_TAGS_PER_RULE} entries.",
                            rule=where, field="tags")
            tags: List[str] = []
            for tag in value:
                if not isinstance(tag, str) or not tag.strip():
                    raise _fail("every tag must be a non-empty string.",
                                rule=where, field="tags")
                tags.append(tag.strip()[:MAX_TERM_LENGTH])
            out["tags"] = tags
            continue
        if value is None:
            continue
        if not isinstance(value, (str, int, float)):
            raise _fail(f"'set.{key}' must be a string.", rule=where, field=key)
        out[key] = str(value)[:MAX_CATEGORY_LENGTH]
    return out


def _parse_rule(raw: Any, index: int) -> Rule:
    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise _fail("every entry in 'rules' must be an object.", rule=where)

    unknown = set(raw) - _ALLOWED_RULE_KEYS
    if unknown:
        raise _fail(
            f"unknown key(s) on rule: {', '.join(sorted(unknown))}. "
            f"Supported: {', '.join(sorted(_ALLOWED_RULE_KEYS))}.",
            rule=where, unknown_keys=sorted(unknown))

    category = raw.get("category")
    if not isinstance(category, str) or not category.strip():
        raise _fail("'category' is required and must be a non-empty string.",
                    rule=where, field="category")
    category = category.strip()[:MAX_CATEGORY_LENGTH]

    rule_id = raw.get("id")
    if rule_id is None:
        # Generated rather than required: a caller hand-writing a small ruleset
        # should not have to invent ids, but every response line needs one to
        # point at.
        rule_id = f"rule_{index}"
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise _fail("'id' must be a non-empty string when supplied.",
                    rule=where, field="id")
    rule_id = rule_id.strip()[:MAX_RULE_ID_LENGTH]

    priority = raw.get("priority", DEFAULT_PRIORITY)
    if isinstance(priority, bool) or not isinstance(priority, (int, float)):
        raise _fail("'priority' must be a number.", rule=where, field="priority")
    priority = int(priority)

    catch_all = bool(raw.get("catch_all", False))
    stop = bool(raw.get("stop", False))
    match = _parse_match(raw.get("match"), where=where)

    if match.is_empty and not catch_all:
        # The single most dangerous shape a ruleset can take: a rule with no
        # conditions quietly claims every transaction. Forgetting the `match`
        # block is a far likelier explanation than wanting that, so it has to
        # be said out loud with `catch_all`.
        raise _fail(
            "this rule has no conditions and would match every transaction. "
            "Add a 'match' block, or set \"catch_all\": true if that is "
            "genuinely intended (pair it with a low 'priority').",
            rule=where, rule_id=rule_id)
    if match.none_of and not (match.any_of or match.all_of or match.regex
                              or match.direction
                              or match.min_amount_paise is not None
                              or match.max_amount_paise is not None
                              or match.min_date or match.max_date):
        raise _fail(
            "this rule only has 'none_of', so it matches every transaction that "
            "does not contain those terms. Add a positive condition, or set "
            "\"catch_all\": true to confirm that is intended.",
            rule=where, rule_id=rule_id)

    return Rule(index=index, rule_id=rule_id, category=category,
                priority=priority, match=match,
                assign=_parse_assign(raw.get("set"), where=where),
                catch_all=catch_all, stop=stop)


def parse_ruleset(raw: Any) -> RuleSet:
    """Validate a decoded ruleset. Raises `ApiError(INVALID_RULES)` on any fault.

    Accepts either the documented object form or a bare list of rules, because
    `[{...}, {...}]` is the obvious thing to send and refusing it would buy
    nothing.
    """
    if isinstance(raw, list):
        raw = {"rules": raw}
    if not isinstance(raw, dict):
        raise _fail("the ruleset must be a JSON object, or a JSON array of rules.")

    unknown = set(raw) - _ALLOWED_TOP_KEYS
    if unknown:
        raise _fail(
            f"unknown top-level key(s): {', '.join(sorted(unknown))}. "
            f"Supported: {', '.join(sorted(_ALLOWED_TOP_KEYS))}.",
            unknown_keys=sorted(unknown))

    rules_raw = raw.get("rules")
    if rules_raw is None:
        raise _fail("'rules' is required. Send an empty array with "
                    "\"fallback\": \"builtin\" to classify using the built-in "
                    "engine alone.")
    if not isinstance(rules_raw, list):
        raise _fail("'rules' must be an array.")
    if len(rules_raw) > MAX_RULES:
        raise _fail(f"{len(rules_raw)} rules were supplied; the limit is "
                    f"{MAX_RULES}.", rule_count=len(rules_raw))

    fallback = raw.get("fallback") or FALLBACK_NONE
    if not isinstance(fallback, str) or fallback.strip().lower() not in FALLBACK_MODES:
        raise _fail(f"'fallback' must be one of: {', '.join(FALLBACK_MODES)}.",
                    received=repr(raw.get("fallback")))
    fallback = fallback.strip().lower()

    default_category = raw.get("default_category")
    if default_category is not None:
        if not isinstance(default_category, str) or not default_category.strip():
            raise _fail("'default_category' must be a non-empty string when supplied.")
        default_category = default_category.strip()[:MAX_CATEGORY_LENGTH]

    version = raw.get("version")
    if version is not None and not isinstance(version, (str, int, float)):
        raise _fail("'version' must be a string.")

    rules = tuple(_parse_rule(entry, i) for i, entry in enumerate(rules_raw))

    seen: Dict[str, int] = {}
    for rule in rules:
        if rule.rule_id in seen:
            # Ids are how the response reports which rule fired and how
            # `rule_usage` reports which never did. Duplicates would merge two
            # rules' counts into one line and make both unreadable.
            raise _fail(
                f"duplicate rule id '{rule.rule_id}' at rules[{rule.index}]; "
                f"it is already used by rules[{seen[rule.rule_id]}]. Rule ids "
                f"must be unique.",
                rule=f"rules[{rule.index}]", rule_id=rule.rule_id)
        seen[rule.rule_id] = rule.index

    if not rules and fallback == FALLBACK_NONE:
        raise _fail("no rules were supplied and 'fallback' is 'none', so every "
                    "transaction would come back unclassified. Supply rules, or "
                    "set \"fallback\": \"builtin\".")

    return RuleSet(rules=rules, default_category=default_category,
                   fallback=fallback,
                   version=str(version) if version is not None else None)


def load_ruleset(raw_json: Optional[str]) -> RuleSet:
    """Decode and validate the `rules` form field."""
    if raw_json is None or not str(raw_json).strip():
        raise ApiError(
            errors.MISSING_RULES,
            "A ruleset must be supplied in the 'rules' form field as JSON. "
            "GET /v1/classify/schema documents the format.")
    text = str(raw_json)
    if len(text.encode("utf-8", errors="ignore")) > MAX_RULES_JSON_BYTES:
        raise ApiError(
            errors.RULES_TOO_LARGE,
            f"the ruleset exceeds the {MAX_RULES_JSON_BYTES // 1024} KB limit",
            detail={"max_bytes": MAX_RULES_JSON_BYTES})
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise _fail(f"'rules' is not valid JSON: {exc}")
    return parse_ruleset(decoded)


# ------------------------------------------------------------------- matching

@dataclass
class _Subject:
    """One transaction reduced to the forms the matchers need, computed once.

    Built per transaction rather than per (transaction, rule): normalising and
    tokenising a narration once for a 1000-rule ruleset instead of a thousand
    times is the difference between a fast request and a slow one.
    """

    raw_upper: str
    normalized: str
    tokens: Tuple[str, ...]
    token_set: Set[str]
    regex_text: str
    amount_paise: int
    direction: Optional[str]
    txn_date: Optional[datetime.date]


def _subject_for(txn: Any) -> _Subject:
    raw = (getattr(txn, "narration_raw", None)
           or getattr(txn, "narration_clean", None) or "")
    raw_upper = str(raw).upper()
    tokens = tuple(tokenize_narration(raw))
    return _Subject(
        raw_upper=raw_upper,
        normalized=normalize_narration(raw),
        tokens=tokens,
        token_set=set(tokens),
        regex_text=str(raw)[:REGEX_SCAN_CHARS],
        amount_paise=int(getattr(txn, "amount_paise", 0) or 0),
        direction=getattr(txn, "direction", None),
        txn_date=getattr(txn, "txn_date", None),
    )


def _term_matches(term: str, subject: _Subject) -> bool:
    """Does `term` appear in this narration?

    Three ways, in order of cost:

    1. As a whole word in the normalised narration. Padding both sides with a
       space is what stops `VI` matching inside `VIDEO`, and it still lets a
       multi-word term match as a unit.
    2. As a whole word in the raw narration. Normalisation strips reference
       numbers and long digit runs, so a term containing digits or punctuation
       would otherwise be unfindable even though it is plainly in the text.
    3. Concatenated inside a single token, for terms of at least
       `MIN_EMBEDDED_TERM_LENGTH` characters. `NAMMAYATRI` has to match
       `UPI-NAMMAYATRI`, because that is how the narration arrives.
    """
    if f" {term} " in f" {subject.normalized} ":
        return True
    if f" {term} " in f" {subject.raw_upper} ":
        return True
    compact = term.replace(" ", "")
    if len(compact) >= MIN_EMBEDDED_TERM_LENGTH:
        for tok in subject.tokens:
            if len(tok) >= len(compact) and compact in tok:
                return True
        # The raw form catches separator-joined text that tokenisation split
        # differently, e.g. a term spanning a '/' the tokeniser treats as a
        # boundary.
        if compact in subject.raw_upper.replace(" ", ""):
            return True
    return False


def _matched_terms(terms: Sequence[str], subject: _Subject) -> List[str]:
    return [t for t in terms if _term_matches(t, subject)]


def evaluate_rule(rule: Rule, subject: _Subject) -> Optional[List[str]]:
    """Does this rule fire? Returns the matched terms, or None if it does not.

    An empty list is a real answer — a rule matching purely on direction, amount
    or date fires with no terms — so the None/[] distinction matters and callers
    must test `is None`.
    """
    spec = rule.match

    if spec.direction is not None and subject.direction != spec.direction:
        return None

    if spec.min_amount_paise is not None and subject.amount_paise < spec.min_amount_paise:
        return None
    if spec.max_amount_paise is not None and subject.amount_paise > spec.max_amount_paise:
        return None

    if spec.min_date is not None:
        if subject.txn_date is None or subject.txn_date < spec.min_date:
            return None
    if spec.max_date is not None:
        if subject.txn_date is None or subject.txn_date > spec.max_date:
            return None

    # Vetoes before positive evidence: a `none_of` hit ends the rule regardless
    # of how much else matched.
    if spec.none_of and _matched_terms(spec.none_of, subject):
        return None

    terms: List[str] = []

    if spec.all_of:
        hits = _matched_terms(spec.all_of, subject)
        if len(hits) != len(spec.all_of):
            return None
        terms.extend(hits)

    if spec.any_of:
        hits = _matched_terms(spec.any_of, subject)
        if not hits:
            return None
        terms.extend(hits)

    if spec.regex is not None:
        found = spec.regex.search(subject.regex_text)
        if not found:
            return None
        terms.append(f"regex:{found.group(0)[:80]}")

    return terms


# ------------------------------------------------------------------- decisions

@dataclass
class Decision:
    """What a ruleset concluded about one transaction."""

    category: Optional[str]
    method: str
    rule_id: Optional[str] = None
    priority: Optional[int] = None
    #: Only ever set for a `builtin` decision, where a real probability exists.
    #: A caller rule is a deterministic assertion, not an estimate, so it leaves
    #: this None rather than inventing a number like 1.0.
    confidence: Optional[float] = None
    matched_terms: List[str] = field(default_factory=list)
    assign: Dict[str, Any] = field(default_factory=dict)
    ambiguous: bool = False
    runner_up_rule_id: Optional[str] = None
    explanation: str = ""

    def to_api(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "category": self.category,
            "method": self.method,
            "rule_id": self.rule_id,
            "explanation": self.explanation,
        }
        if self.priority is not None:
            out["priority"] = self.priority
        if self.confidence is not None:
            # 3dp to match `CanonicalTxn.to_api`, which publishes the same
            # quantity as the row's `category_confidence`. At 4dp here the two
            # fields rendered the same number differently in one response.
            out["confidence"] = round(float(self.confidence), 3)
        if self.matched_terms:
            out["matched_terms"] = self.matched_terms
        if self.ambiguous:
            out["ambiguous"] = True
            out["runner_up_rule_id"] = self.runner_up_rule_id
        return out


def classify_one(ruleset: RuleSet, txn: Any,
                 subject: Optional[_Subject] = None) -> Decision:
    """Apply a ruleset to one transaction.

    Every rule is evaluated and the winner is the highest `priority`; ties break
    on the earlier position in the supplied array, so the result is reproducible
    for a given ruleset and never depends on dict ordering. A tie between two
    *different* categories is reported as `ambiguous` rather than silently
    resolved, because that is a fault in the ruleset its author needs to see.

    `stop: true` on a rule short-circuits evaluation the moment it matches. That
    is the one place first-match semantics are available, and it is opt-in per
    rule.
    """
    subject = subject or _subject_for(txn)

    best: Optional[Tuple[Rule, List[str]]] = None
    runner_up: Optional[Rule] = None

    for rule in ruleset.rules:
        hits = evaluate_rule(rule, subject)
        if hits is None:
            continue
        if rule.stop:
            return Decision(
                category=rule.category, method=METHOD_RULE, rule_id=rule.rule_id,
                priority=rule.priority, matched_terms=hits, assign=dict(rule.assign),
                explanation=_explain(rule, hits, stopped=True))
        if best is None:
            best = (rule, hits)
            continue
        current = best[0]
        if (rule.priority, -rule.index) > (current.priority, -current.index):
            runner_up = current
            best = (rule, hits)
        elif runner_up is None or rule.priority > runner_up.priority:
            runner_up = rule

    if best is None:
        if ruleset.default_category:
            return Decision(
                category=ruleset.default_category, method=METHOD_DEFAULT,
                explanation="No rule matched; 'default_category' applied.")
        return Decision(
            category=None, method=METHOD_NONE,
            explanation="No rule matched and no 'default_category' was supplied.")

    rule, hits = best
    ambiguous = (runner_up is not None
                 and runner_up.priority == rule.priority
                 and runner_up.category != rule.category)
    return Decision(
        category=rule.category, method=METHOD_RULE, rule_id=rule.rule_id,
        priority=rule.priority, matched_terms=hits, assign=dict(rule.assign),
        ambiguous=ambiguous,
        runner_up_rule_id=runner_up.rule_id if ambiguous else None,
        explanation=_explain(rule, hits, ambiguous_with=runner_up if ambiguous else None))


def _explain(rule: Rule, hits: Sequence[str], *, stopped: bool = False,
             ambiguous_with: Optional[Rule] = None) -> str:
    if rule.catch_all and not hits:
        text = f"Catch-all rule '{rule.rule_id}' applied → {rule.category}."
    elif hits:
        text = (f"Rule '{rule.rule_id}' matched on {', '.join(hits)} "
                f"→ {rule.category}.")
    else:
        text = (f"Rule '{rule.rule_id}' matched on its amount, date or direction "
                f"conditions → {rule.category}.")
    if stopped:
        text += " Evaluation stopped here ('stop': true)."
    if ambiguous_with is not None:
        text += (f" Ambiguous: rule '{ambiguous_with.rule_id}' matched at the "
                 f"same priority {ambiguous_with.priority} with a different "
                 f"category '{ambiguous_with.category}'; the earlier rule won.")
    return text


__all__ = [
    "MAX_RULES", "MAX_RULES_JSON_BYTES", "MAX_REGEX_LENGTH", "SETTABLE_FIELDS",
    "FALLBACK_NONE", "FALLBACK_BUILTIN", "FALLBACK_MODES",
    "METHOD_RULE", "METHOD_BUILTIN", "METHOD_DEFAULT", "METHOD_NONE",
    "DEFAULT_PRIORITY",
    "MatchSpec", "Rule", "RuleSet", "Decision",
    "parse_ruleset", "load_ruleset", "classify_one", "evaluate_rule",
]
