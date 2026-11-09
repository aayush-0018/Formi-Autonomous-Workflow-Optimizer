"""The one shared cohort object (C2, stakeholders/05).

A workflow's cohorts are a *partition*: the cartesian product of a short, ordered list of features
(e.g. ["age_band"] or ["age_band", "lang"]). Every cell is a `Cohort` with a deterministic, human-readable
id such as `age_band=60+&lang=hi`. The same objects are written into the simulator config, used to key
schedule tables and experiment allocations, used to stratify analysis, and shown in the supervisor view.
There is exactly one definition of "seniors who speak Tamil", and it is this one.

Cells are disjoint and exhaustive over the observed values, so the simulator's first-match evaluation
order does not matter. Unseen values fall to `default`, whose allocation is the population-wide policy.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

MAX_COHORTS = 20  # simulator limit


@dataclass(frozen=True)
class Cohort:
    id: str
    where: dict = field(hash=False, compare=False)

    def matches(self, feats: dict[str, str]) -> bool:
        return all(feats.get(f) in vals for f, vals in self.where.items())

    def to_config(self) -> dict:
        return {"id": self.id, "where": {f: list(v) for f, v in self.where.items()}}


def cohort_id(assign: dict[str, str], order: list[str]) -> str:
    return "&".join(f"{f}={assign[f]}" for f in order)


def partition(features: list[str], values: dict[str, list[str]]) -> list[Cohort]:
    """Cartesian product of the given features' observed values. Empty feature list -> no cohorts."""
    if not features:
        return []
    grids = [[(f, v) for v in values[f]] for f in features]
    out = []
    for combo in itertools.product(*grids):
        assign = dict(combo)
        out.append(Cohort(cohort_id(assign, features), {f: [v] for f, v in combo}))
    return out


def partition_size(features: list[str], values: dict[str, list[str]]) -> int:
    n = 1
    for f in features:
        n *= len(values[f])
    return n if features else 0


def cohort_of(cohorts: list[Cohort], feats: dict[str, str]) -> str:
    for c in cohorts:
        if c.matches(feats):
            return c.id
    return "default"


def parse_segment(seg: str) -> tuple[str, str] | None:
    """'lang=hi' -> ('lang', 'hi'); 'ALL' -> None."""
    if seg == "ALL":
        return None
    f, v = seg.split("=", 1)
    return f, v


def segment_matches(seg: str, feats: dict[str, str]) -> bool:
    fv = parse_segment(seg)
    return fv is None or feats.get(fv[0]) == fv[1]


def cohort_in_segment(c: Cohort, seg: str) -> bool:
    """True if every lead of cohort c is inside segment seg."""
    fv = parse_segment(seg)
    if fv is None:
        return True
    return c.where.get(fv[0]) == [fv[1]]
