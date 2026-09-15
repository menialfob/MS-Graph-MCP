"""Reduce the full Graph surface to a reviewable, in-scope catalog.

Two distinct reductions happen here, and conflating them causes trouble later:

1. Structural pruning -- removing generated noise ($count/$ref siblings, OData
   cast variants, deep navigation expansions). This is objectively safe: those
   operations are artifacts of how the description is generated, not distinct
   actions a person would ask for.

2. Scope filtering -- applying a profile's include/exclude rules. This is a
   product judgement about which surfaces this deployment should offer, and it
   is NOT a security control (see docs/SCOPE.md).

The output is the operation catalog that gets indexed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from graph_mcp.policy.globs import match_any
from pipeline.join import normalize_path
from pipeline.parse_openapi import Operation


@dataclass
class Profile:
    name: str
    description: str
    max_path_params: int
    drop_suffixes: list[str]
    drop_segments_containing: list[str]
    include: list[str]
    exclude: list[str]
    read_only: list[str]

    @classmethod
    def load(cls, path: Path) -> "Profile":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        prune = raw.get("prune", {})
        return cls(
            name=raw["name"],
            description=raw.get("description", "").strip(),
            max_path_params=prune.get("max_path_params", 3),
            drop_suffixes=prune.get("drop_suffixes", []),
            drop_segments_containing=prune.get("drop_segments_containing", []),
            include=raw.get("include", []),
            exclude=raw.get("exclude", []),
            read_only=raw.get("read_only", []),
        )


@dataclass
class CurationStats:
    total: int = 0
    dropped_structural: int = 0
    dropped_not_included: int = 0
    dropped_excluded: int = 0
    dropped_read_only: int = 0
    kept: int = 0

    def report(self) -> str:
        lines = [
            f"  input operations        {self.total:>7,}",
            f"  - structural noise      {self.dropped_structural:>7,}",
            f"  - outside include list  {self.dropped_not_included:>7,}",
            f"  - explicitly excluded   {self.dropped_excluded:>7,}",
            f"  - write on read-only    {self.dropped_read_only:>7,}",
            f"  = catalog               {self.kept:>7,}",
        ]
        return "\n".join(lines)


def is_structural_noise(op: Operation, profile: Profile) -> bool:
    path = normalize_path(op.path)
    if any(path.endswith(s.lower()) for s in profile.drop_suffixes):
        return True
    # normalize_path already strips the microsoft.graph. prefix from cast
    # segments, so test the raw path for those.
    if any(frag in op.path for frag in profile.drop_segments_containing):
        return True
    if len(op.path_params) > profile.max_path_params:
        return True
    return False


def curate(
    ops: list[Operation], profile: Profile
) -> tuple[list[Operation], CurationStats]:
    stats = CurationStats(total=len(ops))
    kept: list[Operation] = []

    for op in ops:
        if is_structural_noise(op, profile):
            stats.dropped_structural += 1
            continue
        path = normalize_path(op.path)
        if match_any(profile.exclude, path):
            stats.dropped_excluded += 1
            continue
        if not match_any(profile.include, path):
            stats.dropped_not_included += 1
            continue
        if op.method.upper() != "GET" and match_any(profile.read_only, path):
            stats.dropped_read_only += 1
            continue
        kept.append(op)

    stats.kept = len(kept)
    return kept, stats
