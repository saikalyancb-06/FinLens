"""A reusable, bank-agnostic entity-resolution engine.

Turns the strings a bank statement contains into the parties it is actually
about. Knows no names: everything is derived from the SHAPE of narration text,
so a statement from an unfamiliar bank naming unfamiliar people needs no code
change.

    from app.entity_resolution import EntityMention, EntityResolver

    report = EntityResolver().resolve([EntityMention(raw=n) for n in narrations])
    report.as_dict()          # stage 16 numbers
    report.clusters           # canonical entities, with their aliases
    report.suggestions        # "these may be the same" for a person to confirm
"""
from app.entity_resolution.normalize import (Representations, clean_narration,
                                             representations)
from app.entity_resolution.resolver import (ClusterAssignment, EntityCluster,
                                            EntityMention, EntityResolver,
                                            MatchVerdict, ResolutionReport,
                                            Weights, cluster_narrations,
                                            resolve)

__all__ = [
    "Representations", "clean_narration", "representations",
    "EntityMention", "EntityResolver", "EntityCluster", "MatchVerdict",
    "ResolutionReport", "Weights", "resolve",
    "ClusterAssignment", "cluster_narrations",
]
