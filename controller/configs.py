"""Workflow-config helpers implementing the semantics documented in docs/SIMULATOR.md §3–4.

Re-implemented from the contract (not imported from `sim`): weights are normalised, a cohort that omits a
dimension falls back to `default`, and auto-apply's `max_shift` is the largest total-variation distance of any
cohort's welcome/voice/caller_id distribution.
"""
from __future__ import annotations

import copy

AGENT_DIMS = ("welcome_message", "voice", "caller_id")
CHANGE_DIMS = ("cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule")


def norm_dist(d: dict) -> dict:
    s = float(sum(v for v in d.values() if v > 0))
    return {k: round(v / s, 6) for k, v in sorted(d.items()) if v > 0} if s > 0 else {}


def normalise(cfg: dict) -> dict:
    out = {"cohorts": [{"id": c["id"], "where": {f: list(v) for f, v in c["where"].items()}}
                       for c in cfg.get("cohorts") or []],
           "allocations": {}, "prompt_version": cfg.get("prompt_version"),
           "schedule": {"hour_weights": {}, "lead_preferred_hour": dict((cfg.get("schedule") or {})
                                                                         .get("lead_preferred_hour") or {})}}
    for key, dims in (cfg.get("allocations") or {}).items():
        out["allocations"][key] = {d: norm_dist(w) for d, w in (dims or {}).items() if w}
    for key, hw in ((cfg.get("schedule") or {}).get("hour_weights") or {}).items():
        out["schedule"]["hour_weights"][key] = {str(int(h)): round(float(w), 6) for h, w in hw.items()}
    return out


def dist(cfg: dict, cohort: str, dim: str) -> dict:
    return (cfg["allocations"].get(cohort) or {}).get(dim) or cfg["allocations"]["default"][dim]


def cohort_ids(cfg: dict) -> list[str]:
    return ["default"] + [c["id"] for c in cfg.get("cohorts") or []]


def changed_dims(old: dict, new: dict) -> list[str]:
    old, new = normalise(old), normalise(new)
    ch = set()
    if old["cohorts"] != new["cohorts"]:
        ch.add("cohorts")
    keys = set(old["allocations"]) | set(new["allocations"])
    for d in AGENT_DIMS:
        if any(_safe_dist(old, k, d) != _safe_dist(new, k, d) for k in keys):
            ch.add(d)
    if old["prompt_version"] != new["prompt_version"]:
        ch.add("prompt_version")
    if old["schedule"] != new["schedule"]:
        ch.add("schedule")
    return [d for d in CHANGE_DIMS if d in ch]


def _safe_dist(cfg, k, d):
    try:
        return dist(cfg, k, d)
    except KeyError:
        return {}


def tv(a: dict, b: dict) -> float:
    return 0.5 * sum(abs(a.get(o, 0) - b.get(o, 0)) for o in set(a) | set(b))


def max_shift(old: dict, new: dict) -> float:
    old, new = normalise(old), normalise(new)
    keys = {"default"} | {c["id"] for c in old["cohorts"]} | {c["id"] for c in new["cohorts"]}
    return max((tv(_safe_dist(old, k, d), _safe_dist(new, k, d)) for k in keys for d in AGENT_DIMS), default=0.0)


def step_towards(cur: dict, target: dict, max_tv: float) -> dict:
    """Move distribution `cur` towards `target` by at most `max_tv` total variation."""
    d = tv(cur, target)
    if d <= max_tv + 1e-9:
        return dict(target)
    f = max_tv / d * 0.999
    keys = set(cur) | set(target)
    return norm_dist({k: cur.get(k, 0) + f * (target.get(k, 0) - cur.get(k, 0)) for k in keys})


def mode(d: dict) -> str:
    return max(sorted(d), key=lambda k: d[k])


def diff_summary(old: dict, new: dict) -> dict:
    """Free-form but structured diff for the approval request and the audit trail."""
    old, new = normalise(old), normalise(new)
    out: dict = {}
    if old["cohorts"] != new["cohorts"]:
        out["cohorts"] = {"before": [c["id"] for c in old["cohorts"]], "after": [c["id"] for c in new["cohorts"]]}
    keys = sorted(set(old["allocations"]) | set(new["allocations"]))
    for d in AGENT_DIMS:
        per = {}
        for k in keys:
            a, b = _safe_dist(old, k, d), _safe_dist(new, k, d)
            if a != b:
                per[k] = {"before": a, "after": b}
        if per:
            out[d] = per
    if old["prompt_version"] != new["prompt_version"]:
        out["prompt_version"] = {"before": old["prompt_version"], "after": new["prompt_version"]}
    if old["schedule"] != new["schedule"]:
        hw_o, hw_n = old["schedule"]["hour_weights"], new["schedule"]["hour_weights"]
        out["schedule"] = {k: {"before": hw_o.get(k), "after": hw_n.get(k)}
                           for k in sorted(set(hw_o) | set(hw_n)) if hw_o.get(k) != hw_n.get(k)}
    return out


def copy_cfg(cfg: dict) -> dict:
    return copy.deepcopy(cfg)
