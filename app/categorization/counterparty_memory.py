"""Read and write the per-user counterparty memory.

Kept separate from `counterparty.py` (pure string work, no database) so the
extractor stays testable without a session, and separate from `hybrid.py`
(pure function, no session) so classification remains callable offline.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.categorization.counterparty import (
    Counterparty, ReviewGroup, _fuzzy, extract, group_for,
)
from app.categorization.purpose_rules import ocr_correct
from app.models.counterparty_memory import CounterpartyMemory


@dataclass(frozen=True)
class MemoryHit:
    key: str
    display: str
    category: str
    event_type: Optional[str]
    times_confirmed: int
    # 'counterparty' when the user decided about a trading partner, 'pattern'
    # when they decided about a shape of narration (a bank charge, POS rent).
    kind: str = "counterparty"


def load_memory(db: Session, user_id) -> Dict[str, MemoryHit]:
    """A LOOKUP INDEX over a user's learned mappings — not one entry per row.

    Alongside each stored key it carries the OCR-repaired spelling and the
    consonant skeleton, so one decision reaches the variants the bank prints on
    other statements. Several keys therefore point at the same MemoryHit, and
    counting them counts index entries rather than decisions. Query
    `CounterpartyMemory` if what you want is the mappings themselves.

    Loaded once per ingestion batch rather than queried per row: a 1,800-row
    statement would otherwise issue 1,800 round trips to read a table that in
    practice holds a few hundred rows.
    """
    rows = (
        db.query(CounterpartyMemory)
        .filter(CounterpartyMemory.user_id == user_id)
        .all()
    )
    memory: Dict[str, MemoryHit] = {}
    for r in rows:
        hit = MemoryHit(
            key=r.counterparty_key,
            display=r.display_name,
            category=r.category,
            event_type=r.event_type,
            times_confirmed=r.times_confirmed or 1,
            kind=r.kind or "counterparty",
        )
        memory[r.counterparty_key] = hit

        # Keys stored before the OCR repair moved into `extract` are spelled the
        # damaged way — `5U5HM1THA H SHETTY`. Lookups now arrive repaired, so
        # those decisions would silently stop matching and the user would be
        # asked about a party they already answered for. Indexing both spellings
        # costs one dict entry and no migration.
        repaired = ocr_correct(r.counterparty_key).strip().upper()
        if repaired and repaired != r.counterparty_key:
            memory.setdefault(repaired, hit)

    # Spelling variants, indexed under the consonant skeleton so a decision
    # about `SUSHMITHA H SHETTY` also covers `SUSHMITA H SHETTY` on the next
    # statement. Without it the user answers again for a party they have
    # already answered for, which is the whole complaint about the queue.
    #
    # Kept in a SECOND pass and behind two guards, because this is the one
    # place a fuzzy match books money rather than just grouping a question:
    #
    #   - a skeleton under six characters is not distinctive enough. `ABC`
    #     reduces to `BC`.
    #   - if two decisions share a skeleton and disagree about the category,
    #     neither is used. The user drew a distinction the skeleton cannot see,
    #     and guessing which one they meant is worse than asking.
    by_skeleton: Dict[str, List[MemoryHit]] = {}
    for r in rows:
        skeleton = _fuzzy(r.counterparty_key)
        if skeleton and len(skeleton) >= 6:
            by_skeleton.setdefault(skeleton, []).append(memory[r.counterparty_key])

    for skeleton, hits in by_skeleton.items():
        if len({h.category for h in hits}) != 1:
            continue
        memory.setdefault(skeleton, max(hits, key=lambda h: h.times_confirmed))

    return memory


def lookup(memory: Dict[str, MemoryHit], narration: str) -> Optional[MemoryHit]:
    """What the user already decided about this narration, if anything.

    Exact key first. Then the consonant skeleton, which is how a decision about
    `SUSHMITHA H SHETTY` reaches `SUSHMITA H SHETTY` on the next statement —
    the same person, spelled the way the bank happened to spell it that day.

    That fallback used to be refused outright, on the grounds that deciding
    NARASIMHAIAH CHIKEN and NARASIMHA CHIKEN are one party is the user's call.
    It still is: they made it, in the review queue, where those spellings are
    now shown as one group with the alternates listed. Applying the answer they
    gave is not a new judgement. The guards that matter live in `load_memory` —
    a skeleton must be distinctive, and conflicting decisions disable it.

    Falls back to the narration SHAPE when the row names no counterparty, so a
    decision about "charges of this kind" also carries forward — otherwise the
    695 charge rows on a real statement would be re-asked on every upload.
    """
    group = group_for(narration)
    if not group:
        return None
    hit = memory.get(group.key)
    if hit is not None:
        return hit
    if group.kind == "counterparty" and group.fuzzy_key:
        return memory.get(group.fuzzy_key)
    return None


def remember(
    db: Session,
    user_id,
    narration: str,
    category: str,
    event_type: Optional[str] = None,
    source: str = "manual",
) -> Optional[CounterpartyMemory]:
    """Record (or update) what the user decided about this narration's party.

    Returns None when no counterparty could be extracted — a manual decision on
    a bank charge teaches nothing transferable, and writing a junk key would
    make the memory misfire on unrelated rows later.
    """
    group = group_for(narration)
    if not group:
        return None

    row = (
        db.query(CounterpartyMemory)
        .filter(
            CounterpartyMemory.user_id == user_id,
            CounterpartyMemory.counterparty_key == group.key,
        )
        .first()
    )

    now = datetime.now(timezone.utc)
    if row is None:
        row = CounterpartyMemory(
            id=uuid.uuid4(),
            user_id=user_id,
            counterparty_key=group.key,
            fuzzy_key=group.fuzzy_key,
            display_name=group.display,
            kind=group.kind,
            category=category,
            event_type=event_type,
            times_confirmed=1,
            source=source,
        )
        db.add(row)
    else:
        # A changed decision REPLACES the old one rather than being counted as
        # another confirmation of it — the user correcting themselves must not
        # need as many corrections as they made mistakes.
        if row.category == category and row.event_type == event_type:
            row.times_confirmed = (row.times_confirmed or 1) + 1
        else:
            row.category = category
            row.event_type = event_type
            row.times_confirmed = 1
            row.source = source
        row.fuzzy_key = group.fuzzy_key
        row.display_name = group.display
        row.kind = group.kind
    row.last_applied_at = now
    db.flush()
    return row


def forget(db: Session, user_id, counterparty_key: str) -> bool:
    """Drop one learned mapping. Returns True if a row was removed."""
    deleted = (
        db.query(CounterpartyMemory)
        .filter(
            CounterpartyMemory.user_id == user_id,
            CounterpartyMemory.counterparty_key == counterparty_key,
        )
        .delete(synchronize_session=False)
    )
    return bool(deleted)


def suggest_merges(db: Session, user_id) -> List[Dict[str, object]]:
    """Counterparties that share a fuzzy key but not an exact key.

    Surfaced to the user as "are these the same party?" — never merged
    automatically. Bank narrations transliterate the same name several ways
    (NARASIMHAIAH / NARASIMHA), and this is how those get reconciled without
    the system guessing.
    """
    rows = (
        db.query(CounterpartyMemory)
        .filter(CounterpartyMemory.user_id == user_id)
        .all()
    )
    # Shape groups are excluded. Two charge templates that share a consonant
    # skeleton are not "the same party spelled two ways", and offering to merge
    # them would be nonsense.
    rows = [r for r in rows if (r.kind or "counterparty") == "counterparty"]

    by_fuzzy: Dict[str, List[CounterpartyMemory]] = {}
    for r in rows:
        if r.fuzzy_key:
            by_fuzzy.setdefault(r.fuzzy_key, []).append(r)

    def _member(g: CounterpartyMemory) -> Dict[str, object]:
        return {
            "counterparty_key": g.counterparty_key,
            "display_name": g.display_name,
            "category": g.category,
            "times_confirmed": g.times_confirmed,
        }

    out: List[Dict[str, object]] = []
    seen_pairs = set()

    for fuzzy, group in by_fuzzy.items():
        if len(group) < 2:
            continue
        seen_pairs.update(
            frozenset((a.counterparty_key, b.counterparty_key))
            for a in group for b in group if a is not b
        )
        out.append({
            "reason": "spelling",
            "fuzzy_key": fuzzy,
            "members": [_member(g) for g in group],
            "categories_agree": len({g.category for g in group}) == 1,
        })

    # Prefix relationships. Statement exports cut names at a fixed width, so the
    # same party arrives as "PHONEPE PAYM" and "PHONEPE PAYMENT AGGR" — a
    # difference the fuzzy key cannot see, because the shorter form is simply
    # missing letters rather than spelling them differently.
    #
    # Reported, never applied. "SHETTY" being a prefix of "SHETTY TRADERS" does
    # NOT make them one party, and only the account holder knows which is which.
    ordered = sorted(rows, key=lambda r: len(r.counterparty_key))
    for i, short in enumerate(ordered):
        # Below this length a prefix match is coincidence, not truncation.
        if len(short.counterparty_key) < 8:
            continue
        for long in ordered[i + 1:]:
            if long.counterparty_key == short.counterparty_key:
                continue
            if not long.counterparty_key.startswith(short.counterparty_key):
                continue
            pair = frozenset((short.counterparty_key, long.counterparty_key))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            out.append({
                "reason": "truncation",
                "fuzzy_key": None,
                "members": [_member(short), _member(long)],
                "categories_agree": short.category == long.category,
            })

    return out
