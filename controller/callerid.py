"""Caller-ID health (C5).

Under one config version (and cohort) the caller ID is sampled independently per dial, so DIDs that are live in the
same hours under the same version see the same population. Every comparison below is therefore stratified by
(config version) inside a time window. This matters: on `next_campaign_only` workflows the split is locked per
campaign, so a new DID only dials new campaigns while the old one also dials old ones. An unstratified
"DID vs peers" comparison mixes populations, and in testing it raised two false "collapses".

Two failure modes, two detectors:

* **Sudden flag** (spam-labelled mid-run). Every tick, compare each live DID's pickup over the last 3 hours
  with what its peers did in the same hours, scaled by its own peer-relative ratio over the previous 24 hours.
  The DID must fall to under 60% of its expected pickup with z ≤ −5 on at least 200 dials (≥ 150 peer dials;
  two-sample z, since the peers' pickup is an estimate too), AND below 60% of its *own* previous-24h pickup.
  Spam flags are sudden. Load-driven drift is gradual and is handled by the daily rebalance, not by emergencies. The check runs every
  15 minutes for every live DID, i.e. thousands of looks per scenario. At z ≤ −4 we saw a healthy DID quarantined
  on a chance two-hour dip (z = −4.35), so the bar is −5 (~3e-7 per look). A real spam flag (pickup roughly
  halves) still clears it in about 90 minutes at 100+ dials/hour.
* **Intrinsically bad** (already flagged when we first try it). New DIDs are explored at a small share (5%), so
  the harm is bounded while they're unproven. A *low-share* DID whose pickup is < 80% of the best measured DID
  (z ≤ −3, ≥ 200 dials) is quarantined. We deliberately do *not* quarantine high-share DIDs on that ratio: a DID
  carrying most of the traffic is depressed by its own load, and spreading the load is the cure, not removal.

Allocation policy: split traffic equally across healthy, measured DIDs (rotation spreads load) and give unproven
DIDs the exploration share. Hysteresis: we only change the allocation when it moves ≥ 0.05 in TV distance.
"""
from __future__ import annotations

from .configs import norm_dist, tv
from collections import defaultdict

from .stats import stratified_diff

EXPLORE_SHARE = 0.05
MIN_MEASURED_DIALS = 200
LOW_SHARE = 0.15
Z_EMERGENCY = -5.0


def did_window(store, wf: str, slot_from: int, slot_to: int) -> dict[int, dict[str, list[int]]]:
    """{version: {did: [dials, connected]}} over [slot_from, slot_to)."""
    out: dict = defaultdict(dict)
    for v, d, n, x in store.q("SELECT version, did, SUM(dials), SUM(conn) FROM agg_did WHERE wf=? AND slot>=? AND "
                              "slot<? GROUP BY version, did", (wf, slot_from, slot_to)):
        out[v][d] = [n, x]
    return out


def totals(win: dict) -> dict[str, list[int]]:
    t: dict = defaultdict(lambda: [0, 0])
    for per in win.values():
        for d, (n, x) in per.items():
            t[d][0] += n
            t[d][1] += x
    return dict(t)


def _vs_peers(win: dict, d: str, ratio: float = 1.0):
    """For DID `d`: observed dials/connects, the connects expected from its peers' pickup in the same versions
    (scaled by `ratio`), the sampling variance of that expectation, and the peers' dials."""
    n = x = pdials = 0
    expected = var_e = 0.0
    for per in win.values():
        if d not in per:
            continue
        pn = sum(v[0] for k, v in per.items() if k != d)
        px = sum(v[1] for k, v in per.items() if k != d)
        if pn < 30:
            continue
        q = px / pn
        nd = per[d][0]
        n += nd
        x += per[d][1]
        pdials += pn
        expected += nd * min(q * ratio, 0.99)
        var_e += (nd * ratio) ** 2 * q * (1 - q) / pn   # the peers' rate is an estimate too
    return n, x, expected, var_e, pdials


def emergency_check(store, wf: str, now_slot: int, live: dict) -> list[dict]:
    """Returns [{did, evidence}] for DIDs that just collapsed. `live` = current caller_id dist.

    Two conditions, both required:
      1. vs peers (same hours, same config versions, two-sample z ≤ −5, < 60% of expected), and
      2. vs its own previous 24h (< 60% of its own pickup): a spam flag is *sudden*. Load-driven drift (a DID
         whose share was just raised) moves gradually and must not trigger an emergency rotation.
    """
    recent = did_window(store, wf, now_slot - 3, now_slot)
    prior = did_window(store, wf, now_slot - 27, now_slot - 3)
    prior_tot = totals(prior)
    out = []
    for d, share in live.items():
        if share <= 0:
            continue
        qn, qx, qe, _, qpd = _vs_peers(prior, d)
        ratio = (qx / qe) if qn >= 150 and qpd >= 150 and qe > 0 else 1.0  # its normal level relative to peers
        n, x, e, var_e, pdials = _vs_peers(recent, d, ratio)
        if n < 200 or pdials < 150 or e <= 0:
            continue
        p_exp = e / n
        z = (x - e) / (n * p_exp * (1 - p_exp) + var_e) ** 0.5   # two-sample: both sides are noisy
        own_n, own_x = prior_tot.get(d, [0, 0])
        own_prior = own_x / own_n if own_n >= 300 else None
        sudden = own_prior is None or (x / n) < 0.6 * own_prior
        if x < 0.6 * e and z <= Z_EMERGENCY and sudden:
            out.append({"did": d, "dials_3h": n, "pickup_3h": round(x / n, 4), "expected": round(p_exp, 4),
                        "own_pickup_prior_24h": own_prior and round(own_prior, 4),
                        "ratio_prior_24h": round(ratio, 3), "z": round(z, 2),
                        "method": "two-sample vs peers in the same hours and config versions + drop vs own 24h"})
    return out


def compare(win: dict, a: str, b: str):
    """Stratified (by version) pickup difference of DID a vs DID b."""
    return stratified_diff([(per[a][0], per[a][1], per[b][0], per[b][1]) for per in win.values()
                            if a in per and b in per])


def plan_allocation(store, wf: str, now_slot: int, options: list[str], current: dict, quarantine: dict
                    ) -> tuple[dict, dict]:
    """Daily rebalance. Returns (target distribution, evidence)."""
    win = did_window(store, wf, now_slot - 72, now_slot)       # last 3 days
    tot = totals(win)
    week = totals(did_window(store, wf, now_slot - 168, now_slot))
    measured = {d: v for d, v in tot.items() if v[0] >= MIN_MEASURED_DIALS and d not in quarantine}
    evidence = {"window_hours": 72, "per_did": {d: {"dials": v[0], "pickup": round(v[1] / v[0], 4) if v[0] else None,
                                                    "share_now": round(current.get(d, 0), 3)}
                                                for d, v in sorted(tot.items())}}
    newly_bad = {}
    if measured:
        best = max(measured, key=lambda d: (measured[d][1] / measured[d][0], d))
        for d in measured:
            if d == best or current.get(d, 0) > LOW_SHARE:
                continue
            r = compare(win, d, best)
            if r and r.n_a >= MIN_MEASURED_DIALS and r.rate_a < 0.8 * r.rate_b and r.z <= -3:
                newly_bad[d] = {"pickup": round(r.rate_a, 4), "best": best, "best_pickup": round(r.rate_b, 4),
                                "z": round(r.z, 2), "dials": r.n_a, "method": "stratified by config version"}
    evidence["quarantined_now"] = newly_bad
    good = [d for d in options if d in measured and d not in newly_bad]
    unproven = [d for d in options if d not in quarantine and d not in newly_bad and d not in measured
                and week.get(d, [0, 0])[0] < MIN_MEASURED_DIALS * 3]
    target: dict[str, float] = {}
    explore = unproven[: 3]  # cap exploration at 15% of traffic
    for d in explore:
        target[d] = EXPLORE_SHARE
    rest = 1.0 - sum(target.values())
    if not good:
        base = {d: w for d, w in current.items() if d not in quarantine and d not in newly_bad} or current
        base = norm_dist(base)
        for d, w in base.items():
            target[d] = target.get(d, 0) + rest * w
    else:
        for d in good:
            target[d] = target.get(d, 0) + rest / len(good)
    target = norm_dist(target)
    evidence["good"], evidence["exploring"] = good, explore
    return target, {**evidence, "newly_bad": newly_bad}


def material_change(cur: dict, target: dict) -> bool:
    return tv(norm_dist(cur), norm_dist(target)) >= 0.05 or set(k for k, v in cur.items() if v > 0) != set(target)
