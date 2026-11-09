"""Hallucination incident response (C8): detect → choose fix → validate → propose with evidence → verify.

Detection is on populations. `qa.hallucination_flag` is a noisy detector (it fires on some clean calls and
misses some bad ones), so a flag is only evidence in aggregate. We compare the live prompt's flag rate since it
went live (capped at the last 6 hours) with the workflow's baseline flag rate learned from history. We open an
incident only when all three hold:

    connected ≥ 300,  rate ≥ max(2 × baseline, baseline + 1pp),  P(X ≥ flags | baseline) < 1e-5   (exact binomial)

The p-value bar accounts for repeated looks: the 6h window is re-tested every 15 minutes (~4 independent
windows a day per workflow). At 1e-3 we saw a false incident on the baseline prompt in public_02 (15/405 flags,
p=5e-4). At 1e-5 the expected false alarms are ≈ 1 per 25k workflow-days, while a real spike (≈5% vs 1.5%) still
clears the bar within ~3 hours of traffic. Prompts that a supervisor ships are validated immediately anyway
(planner.validate_live_prompt), so the live monitor is the backstop, not the first line.

Choosing a fix is deterministic and credit-frugal (stakeholders/06: spend credits only where they change a
decision):
  1. Prefer a **known-good** prompt: one with ≥ 1000 connected calls in *our* records for this workflow, a flag
     rate not significantly above baseline, ranked by observed engagement. Validation measures safety, not
     engagement, so production evidence beats a synthetic pass rate (stakeholders/04's "word for word" fix).
  2. Only if there is none, ask the LLM for suggestions (3 credits) and take them in the order returned.
Each candidate gets one validation run (n=300, 3 credits). It must clear the policy pass rate plus a 0.5pp margin.
The run id is cached and reused if the proposal must be re-sent. After apply, the incident stays open until ≥ 300
connected calls on the new version show a flag rate that is no longer elevated.
"""
from __future__ import annotations

import math

from .stats import binom_sf

MIN_CONN = 300
P_ALERT = 1e-5
WINDOW_SLOTS = 6
VALIDATION_N = 300
VALIDATION_MARGIN = 0.005
VERIFY_CONN = 300
CREDIT_RESERVE = 10.0


def baseline(store, wf: str) -> dict:
    r = store.q1("SELECT SUM(conn), SUM(flags), SUM(eng) FROM agg_prompt WHERE wf=? AND slot<0", (wf,))
    n, f = (r[0] or 0), (r[1] or 0)
    return {"rate": (f / n) if n else 0.015, "connected": n, "flags": f}


def window(store, wf: str, prompt: str, slot_from: int, slot_to: int) -> tuple[int, int, int]:
    r = store.q1("SELECT SUM(conn), SUM(flags), SUM(eng) FROM agg_prompt WHERE wf=? AND prompt=? AND slot>=? "
                 "AND slot<?", (wf, prompt, slot_from, slot_to))
    return (r[0] or 0), (r[1] or 0), (r[2] or 0)


def check(store, wf: str, prompt: str, since_slot: int, now_slot: int, base: float) -> dict | None:
    lo = max(since_slot, now_slot - WINDOW_SLOTS)
    n, f, _ = window(store, wf, prompt, lo, now_slot)
    if n < MIN_CONN:
        return None
    rate = f / n
    p = binom_sf(f, n, base)
    if rate >= max(2 * base, base + 0.01) and p < P_ALERT:
        return {"prompt": prompt, "connected": n, "flags": f, "flag_rate": round(rate, 4),
                "baseline_rate": round(base, 4), "p_value": p, "window_slots": [lo, now_slot]}
    return None


def prompt_stats(store, wf: str) -> dict[str, dict]:
    out = {}
    for prompt, n, f, e in store.q("SELECT prompt, SUM(conn), SUM(flags), SUM(eng) FROM agg_prompt WHERE wf=? "
                                   "GROUP BY prompt", (wf,)):
        out[prompt] = {"connected": n, "flags": f, "flag_rate": f / n if n else None,
                       "engagement": e / n if n else None}
    return out


def known_good(store, wf: str, options: list[str], bad: str, base: float) -> list[dict]:
    st = prompt_stats(store, wf)
    good = []
    for p in options:
        s = st.get(p)
        if p == bad or not s or s["connected"] < 1000:
            continue
        if binom_sf(s["flags"], s["connected"], base) < 0.01 or s["flag_rate"] > 1.5 * base:
            continue
        good.append({"prompt_version": p, "source": "production history", **{k: (round(v, 4) if isinstance(v, float)
                                                                               else v) for k, v in s.items()}})
    return sorted(good, key=lambda g: (-g["engagement"], g["prompt_version"]))


def validation_cost(n: int) -> int:
    return math.ceil(n / 100)


def verified(store, wf: str, prompt: str, since_slot: int, now_slot: int, base: float) -> tuple[bool | None, dict]:
    """None = not enough data yet; True = fixed; False = still elevated."""
    n, f, e = window(store, wf, prompt, since_slot, now_slot + 1)
    ev = {"prompt": prompt, "connected": n, "flags": f, "flag_rate": round(f / n, 4) if n else None,
          "engagement": round(e / n, 4) if n else None, "baseline_rate": round(base, 4)}
    if n < VERIFY_CONN:
        return None, ev
    elevated = (f / n) >= max(2 * base, base + 0.01) and binom_sf(f, n, base) < 0.01
    return (not elevated), ev
