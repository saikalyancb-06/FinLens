"""Scoring entity resolution against ground truth.

TWO WAYS TO BE WRONG, AND THEY TRADE OFF AGAINST EACH OTHER:

    FALSE MERGE   two different real parties welded into one entity
    FALSE SPLIT   one real party broken into several

A resolver that merges everything has zero false splits. A resolver that merges
nothing has zero false merges. Either number alone is meaningless, so both are
always reported together, and so is the pairwise F1 that balances them.

FALSE MERGE IS THE MORE EXPENSIVE MISTAKE and the counts are deliberately kept
separate rather than summed. A split shows the user two questions where one
would do — annoying, visible, fixable in the review queue. A merge silently
files one party's money under another's name, and nothing on screen says it
happened.

DEFINITIONS, because "how many mistakes" has more than one reasonable answer:

    false_merge_count   summed over produced clusters: (distinct true entities
                        in this cluster) - 1.  Zero when every cluster is pure.
    false_split_count   summed over true entities: (distinct clusters this
                        entity landed in) - 1.  Zero when no entity is torn up.

Both are counts of REPAIRS NEEDED: how many splits and how many merges would
turn the output into the truth. The pairwise numbers underneath measure the
same thing weighted by how many transactions each error affects.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Dict, List, Sequence, Tuple

__all__ = ["GroundTruthScore", "score_against_truth"]


@dataclass
class GroundTruthScore:
    true_entities: int = 0
    predicted_entities: int = 0
    false_merge_count: int = 0
    false_split_count: int = 0
    pairwise_precision: float = 0.0
    pairwise_recall: float = 0.0
    pairwise_f1: float = 0.0
    exact_clusters: int = 0          # produced clusters that ARE a true entity
    merged_examples: List[Tuple[str, List[str]]] = field(default_factory=list)
    split_examples: List[Tuple[str, List[str]]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "true_entities": self.true_entities,
            "predicted_entities": self.predicted_entities,
            "false_merge_count": self.false_merge_count,
            "false_split_count": self.false_split_count,
            "pairwise_precision": round(self.pairwise_precision, 4),
            "pairwise_recall": round(self.pairwise_recall, 4),
            "pairwise_f1": round(self.pairwise_f1, 4),
            "exact_clusters": self.exact_clusters,
        }


def score_against_truth(
    assignments: Sequence[Tuple[str, str]],
    *, examples: int = 10,
) -> GroundTruthScore:
    """Score a clustering. `assignments` is [(predicted_cluster, true_entity)].

    One entry per MENTION, not per unique string — an error on a party seen 40
    times matters more than one on a party seen once, and the pairwise numbers
    below are what capture that.
    """
    by_pred: Dict[str, List[str]] = defaultdict(list)
    by_true: Dict[str, List[str]] = defaultdict(list)
    for pred, truth in assignments:
        by_pred[pred].append(truth)
        by_true[truth].append(pred)

    score = GroundTruthScore(
        true_entities=len(by_true),
        predicted_entities=len(by_pred),
    )

    for pred, truths in by_pred.items():
        distinct = sorted(set(truths))
        if len(distinct) > 1:
            score.false_merge_count += len(distinct) - 1
            if len(score.merged_examples) < examples:
                score.merged_examples.append((pred, distinct))
        elif distinct and len(set(by_true[distinct[0]])) == 1:
            score.exact_clusters += 1

    for truth, preds in by_true.items():
        distinct = sorted(set(preds))
        if len(distinct) > 1:
            score.false_split_count += len(distinct) - 1
            if len(score.split_examples) < examples:
                score.split_examples.append((truth, distinct))

    # Pairwise: over every pair of mentions, did we agree with the truth about
    # whether they are the same party? This is the standard measure and it
    # weights an error by how many transactions it touches.
    tp = fp = fn = 0
    for truths in by_pred.values():
        for a, b in combinations(truths, 2):
            if a == b:
                tp += 1
            else:
                fp += 1
    for preds in by_true.values():
        for a, b in combinations(preds, 2):
            if a != b:
                fn += 1

    score.pairwise_precision = tp / (tp + fp) if (tp + fp) else 1.0
    score.pairwise_recall = tp / (tp + fn) if (tp + fn) else 1.0
    p, r = score.pairwise_precision, score.pairwise_recall
    score.pairwise_f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return score
