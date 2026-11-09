"""Call-time optimisation (C4), per cohort.

The business owns *how many* calls happen each hour. We decide *who* is dialled in that hour. The dialler
picks eligible leads in proportion to their hour weight, so the right signal is comparative advantage:
w(c, h) = (p(c, h) / p̄(c))^γ, i.e. dial each cohort in the hours that are good *for it* relative to its own
average. Every hour inside the dialling window keeps a positive weight (floor 0.1), so no capacity is wasted
and nobody becomes unreachable.

Feature choice is data-driven. For each lead feature we test the hour × value interaction with a likelihood-ratio
(G) statistic against a multiplicative main-effects model (G ~ χ² with (values−1)(hours−1) dof under "no
interaction"). A feature qualifies if p < 1e-3, Bonferroni-corrected over the features tried. Among those that
qualify we pick the one with the largest excess G − dof (the most structure beyond noise). If none qualifies,
we don't touch the schedule. (We first tried a BIC penalty, but on 50k dials it rejected an interaction with
p ≈ 1e-25: too conservative for a reversible, every-hour-stays-positive change.)

Rates are empirical-Bayes shrunk towards the main-effects prediction (pseudo-count 200 dials), so thin cells
don't produce wild weights. History before D0 seeds the estimates (C3). Live data keeps refining them.
"""
from __future__ import annotations

import math
from collections import defaultdict

from .stats import chi2_sf, shrink_rate

GAMMA = 2.0
FLOOR = 0.1
CEIL = 6.0
SHRINK_K = 200.0
LOOKBACK_DAYS = 14
P_FEATURE = 1e-3


def _xlogy(x, y):
    return x * math.log(y) if x > 0 and y > 0 else 0.0


def _table(store, wf: str, day_from: int, code_feats: dict[int, dict], feature: str, hours: range):
    t = defaultdict(lambda: [0, 0])
    rows = store.q("SELECT hour, code, SUM(dials), SUM(conn) FROM agg_sched WHERE wf=? AND day>=? "
                   "GROUP BY hour, code", (wf, day_from))
    for hour, code, n, x in rows:
        if hour not in hours or code not in code_feats:
            continue
        v = code_feats[code].get(feature)
        if v is None:
            continue
        c = t[(v, hour)]
        c[0] += n
        c[1] += x
    return t


def _fit(t: dict, hours: range):
    vals = sorted({v for v, _ in t})
    nv = {v: sum(t[(v, h)][0] for h in hours if (v, h) in t) for v in vals}
    xv = {v: sum(t[(v, h)][1] for h in hours if (v, h) in t) for v in vals}
    nh = {h: sum(t[(v, h)][0] for v in vals if (v, h) in t) for h in hours}
    xh = {h: sum(t[(v, h)][1] for v in vals if (v, h) in t) for h in hours}
    N, X = sum(nv.values()), sum(xv.values())
    if N == 0 or X == 0:
        return None
    pbar = X / N
    pv = {v: xv[v] / nv[v] if nv[v] else pbar for v in vals}
    rh = {h: (xh[h] / nh[h]) / pbar if nh[h] else 1.0 for h in hours}
    return vals, pv, rh, pbar, N, X


def feature_score(t: dict, hours: range) -> tuple[float, dict]:
    fit = _fit(t, hours)
    if not fit:
        return -1e9, {}
    vals, pv, rh, pbar, N, X = fit
    G = 0.0
    for (v, h), (n, x) in t.items():
        if n == 0 or h not in hours:
            continue
        e = min(max(pv[v] * rh[h], 1e-6), 1 - 1e-6) * n
        G += 2 * (_xlogy(x, x / e) + _xlogy(n - x, (n - x) / (n - e)))
    dof = (len(vals) - 1) * (len([h for h in hours if any((v, h) in t for v in vals)]) - 1)
    p = chi2_sf(G, dof) if dof > 0 else 1.0
    return G - dof, {"G": round(G, 1), "dof": dof, "p_value": p, "connected": X, "dials": N}


def plan(store, wf: str, today: int, code_feats: dict[int, dict], features: list[str], window: list[int]
         ) -> dict | None:
    """Returns {"feature", "tables": {value: {hour: w}}, "evidence"} or None if no feature is worth it."""
    hours = range(window[0], window[1])
    day_from = today - LOOKBACK_DAYS
    best = None
    scores = {}
    for f in features:
        t = _table(store, wf, day_from, code_feats, f, hours)
        s, ev = feature_score(t, hours)
        scores[f] = {"excess_G": round(s, 1), **ev}
        if ev and ev["p_value"] < P_FEATURE / max(len(features), 1) and (best is None or s > best[1]):
            best = (f, s, t)
    if not best:
        return {"feature": None, "tables": {}, "evidence": {"feature_scores": scores}}
    f, s, t = best
    vals, pv, rh, pbar, N, X = _fit(t, hours)
    tables, rates = {}, {}
    for v in vals:
        p = {}
        for h in hours:
            n, x = t.get((v, h), [0, 0])
            p[h] = shrink_rate(x, n, min(pv[v] * rh[h], 0.99), SHRINK_K)
        nv = sum(t.get((v, h), [0, 0])[0] for h in hours)
        mean = (sum(p[h] * t.get((v, h), [0, 0])[0] for h in hours) / nv) if nv else sum(p.values()) / len(p)
        tables[v] = {str(h): round(min(max((p[h] / mean) ** GAMMA, FLOOR), CEIL), 3) for h in hours}
        rates[v] = {str(h): round(p[h], 4) for h in hours}
    return {"feature": f, "tables": tables,
            "evidence": {"feature_scores": scores, "chosen": f, "lookback_days": LOOKBACK_DAYS,
                         "shrunk_pickup_by_value_hour": rates, "gamma": GAMMA}}


def dry_run(store, wf: str, today: int, code_feats: dict[int, dict], feature: str, old_tables: dict,
            new_tables: dict, window: list[int]) -> dict:
    """Replay the last days' dialled mix: re-weight who is dialled in each hour and re-score with the
    historical (value, hour) pickup. Ignores eligibility depletion, so it's an upper-bound-ish estimate."""
    hours = range(window[0], window[1])
    t = _table(store, wf, today - LOOKBACK_DAYS, code_feats, feature, hours)

    def expected(tables):
        tot_n = tot_x = 0.0
        for h in hours:
            vals = [v for v in {v for v, _ in t} if (v, h) in t]
            raw = {v: t[(v, h)][0] * (float(tables.get(v, {}).get(str(h), 1.0)) if tables else 1.0) for v in vals}
            s = sum(raw.values())
            n_h = sum(t[(v, h)][0] for v in vals)
            for v in vals:
                n, x = t[(v, h)]
                if n == 0 or s == 0:
                    continue
                share = raw[v] / s
                tot_n += n_h * share
                tot_x += n_h * share * (x / n)
        return tot_x / tot_n if tot_n else 0.0

    old_p, new_p = expected(old_tables), expected(new_tables)
    return {"method": "replay of the last 14 days' dials per hour, re-allocated across "
                      f"{feature} values by hour weight, scored with observed pickup per (value, hour)",
            "expected_pickup_old": round(old_p, 4), "expected_pickup_new": round(new_p, 4),
            "expected_rel_change": round(new_p / old_p - 1, 4) if old_p else None,
            "caveat": "ignores lead-eligibility depletion and caller-ID load; treat as optimistic"}


def material_change(old: dict, new: dict) -> bool:
    if set(old) != set(new):
        return True
    for v in new:
        for h, w in new[v].items():
            w0 = float(old[v].get(h, 0) or 0)
            if w0 <= 0 or abs(math.log(float(w) / w0)) > 0.25:
                return True
    return False
