"""Welcome-message × voice experimentation (C6) and promotion / decay / rollback (C7).

Design
------
* Only for workflows whose primary KPI is engagement. Welcome message and voice act after pickup, so they
  can't move a pickup-primary workflow's objective and would only spend its scarce approvals.
* Factorial: welcome and voice are randomised independently per call (the simulator samples each dimension
  independently), so one live allocation estimates both main effects at once, and the cross cells are
  available for interaction checks.
* Control keeps the bulk of traffic. A challenger starts at 10% ("exploring"), moves to 20% once it looks
  promising (z ≥ 1, "ramped"), and is dropped as soon as it is significantly harmful (z ≤ −2.5). This bounds
  the expected harm of exploration at apply time while evidence accumulates. Autonomy is earned.
* Allocation changes on Northwind/Kestrel need a human approval, so this is a staged design (explore → ramp →
  promote → monitor) rather than a bandit that re-weights every hour.

Deciding a winner
-----------------
Two-proportion test on engagement (engaged ÷ connected), stratified by (config_version, cohort). Each stratum
is a clean randomised comparison, and CMH weighting avoids Simpson's paradox when allocations change. A
challenger is promoted for the whole population when all of the following hold:
  z ≥ Bonferroni(α=0.01 over all challengers), ≥ 300 connected calls in each arm, relative lift ≥ 3%,
  the last 24h lift still clears the 3% minimum, the last two days are not trending down against the earlier
  data (DiD z > −1; one run promoted a variant mid-decay without this), and its pickup (the guardrail) is not
  significantly worse.
It is promoted for a segment (feature = value) under a much stricter threshold: Bonferroni over challengers ×
segments. Promoted variants get 85% of their cells and the control keeps a 15% holdout.

Decay & rollback
----------------
Every promoted variant is re-tested hourly: the winner's last 24 hours against the control arm's last 72 hours
(holdout plus any control traffic), stratified by cohort. The longer control window gives the monitor real
power with only a 15% holdout. Cohort stratification absorbs the mix shifts the call-time schedule causes.
A variant is flagged if either holds:
  * it is now significantly worse than control (z ≤ −2), or
  * its lift has collapsed and is now negative: difference-in-differences z ≤ −2 against the *lower 90% bound*
    of its lift at promotion. The point estimate at promotion is inflated by the winner's curse (we promote when
    z first crosses the bar), and comparing against it caused a false rollback in testing.
It is rolled back (urgent proposal) once it was flagged and, at least 6 hours later, it is either flagged again
or its lift is ≤ −3%. Suspicion clears only when the lift turns positive. Suspicion is not reset by a single borderline check, because that made one decay flap for 3 days.
Rolled-back variants are blacklisted for the rest of the run (novelty effects don't come back). The same
novelty check runs on arms still in exploration: if an arm's lift over the last two days has reversed versus
its earlier lift (DiD z ≤ −2, now negative), it is dropped (cheap and safe: traffic returns to control). Otherwise a ramped arm whose strong early data
props up its cumulative z could keep decaying at 20% share.

Looks
-----
Explore/ramp/drop/promote decisions are taken at three fixed looks a day (10:00, 13:00, 16:00, inside approver
hours), not every hour. Fewer looks means less false-positive inflation from repeated testing. Decay monitoring
runs every hour, because a rollback's false positive only costs some lift, while a missed decay costs customers.
"""
from __future__ import annotations

from collections import defaultdict

from .cohorts import Cohort, cohort_in_segment, parse_segment
from .stats import Diff, bonferroni_z, stratified_diff

DIMS = ("welcome_message", "voice")
EXPLORE_SHARE = 0.10
RAMP_SHARE = 0.20
MAX_EXPLORE_TOTAL = 0.45
HOLDOUT = 0.20
MIN_CONN = 300
MIN_CONN_EARLY = 150
MIN_REL = 0.03
Z_RAMP = 1.0
Z_DROP = -2.5
ALPHA = 0.01
MIN_SEG_CONN = 300
LOOK_HOURS = (10, 13, 16)


def enabled(spec: dict) -> bool:
    return spec["objective"]["primary"] == "engagement_rate"


def init_state(spec: dict, current_cfg: dict) -> dict:
    out = {}
    for d in DIMS:
        control = _mode(current_cfg["allocations"]["default"][d])
        out[d] = {"control": control, "promoted": {},
                  "arms": {o: {"status": "exploring"} for o in spec["options"][d] if o != control}}
    return out


def _mode(dist: dict) -> str:
    return max(sorted(dist), key=lambda k: dist[k])


class Data:
    """Experiment rows for one workflow, loaded once per analysis."""

    def __init__(self, store, wf: str, code_feats: dict[int, dict], now_slot: int | None = None):
        self.rows = store.q("SELECT day, version, cohort, wm, voice, code, dials, conn, eng FROM agg_exp "
                            "WHERE wf=? AND day>=0", (wf,))
        self.feats = code_feats
        self.arm_rows = store.q("SELECT slot, cohort, wm, voice, conn, eng FROM agg_arm WHERE wf=? AND slot>=?",
                                (wf, (now_slot or 0) - 72)) if now_slot is not None else []
        self.now_slot = now_slot

    def monitor(self, dim: str, winner: str, control: str, seg: str | None, win_hours: int = 24,
                ctl_hours: int = 72) -> Diff | None:
        """Winner over the last `win_hours` vs control over the last `ctl_hours`, stratified by cohort.
        Segment membership of a cohort is read from its id (our ids are `feature=value&...`)."""
        fv = parse_segment(seg) if seg else None
        idx = 2 if dim == "welcome_message" else 3
        st: dict = defaultdict(lambda: {"a": [0, 0], "b": [0, 0]})
        for slot, cohort, wm, voice, conn, eng in self.arm_rows:
            if fv and f"{fv[0]}={fv[1]}" not in cohort.split("&"):
                continue
            v = (slot, cohort, wm, voice)[idx]
            if v == winner and slot >= self.now_slot - win_hours:
                c = st[cohort]["a"]
            elif v == control and slot >= self.now_slot - ctl_hours:
                c = st[cohort]["b"]
            else:
                continue
            c[0] += conn
            c[1] += eng
        return stratified_diff([(c["a"][0], c["a"][1], c["b"][0], c["b"][1]) for c in st.values()])

    def strata(self, dim: str, seg: str | None, day_from: int = 0, metric: str = "eng", day_to: int = 10 ** 6):
        """{(version, cohort): {variant: [n, x]}} for a dimension and optional segment."""
        fv = parse_segment(seg) if seg else None
        idx = 3 if dim == "welcome_message" else 4
        out: dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for r in self.rows:
            if r[0] < day_from or r[0] >= day_to:
                continue
            if fv and self.feats.get(r[5], {}).get(fv[0]) != fv[1]:
                continue
            c = out[(r[1], r[2])][r[idx]]
            if metric == "eng":
                c[0] += r[7]
                c[1] += r[8]
            else:
                c[0] += r[6]
                c[1] += r[7]
        return out

    def compare(self, dim: str, arm: str, control: str, seg: str | None = None, day_from: int = 0,
                metric: str = "eng", day_to: int = 10 ** 6) -> Diff | None:
        st = self.strata(dim, seg, day_from, metric, day_to)
        rows = []
        for cell in st.values():
            a, b = cell.get(arm), cell.get(control)
            if a and b:
                rows.append((a[0], a[1], b[0], b[1]))
        return stratified_diff(rows)

    def segments(self) -> list[str]:
        vals = defaultdict(set)
        for f in self.feats.values():
            for k, v in f.items():
                vals[k].add(v)
        return [f"{k}={v}" for k in sorted(vals) for v in sorted(vals[k])]


def analyse(state: dict, spec: dict, data: Data, today: int, guardrail_max_drop: float | None,
            look: bool = True) -> list[dict]:
    """Updates `state` (the workflow's experiment state) in place. Returns decision records for the audit.
    `look`: whether explore/promote decisions may be taken now (decay monitoring always runs)."""
    decisions = []
    n_challengers = sum(len(state[d]["arms"]) for d in DIMS)
    z_all = bonferroni_z(ALPHA, n_challengers)
    segs = data.segments()
    z_seg = bonferroni_z(ALPHA, n_challengers * max(len(segs), 1))
    for d in DIMS:
        ds = state[d]
        control = ds["control"]
        # ---- monitor promoted variants (decay / rollback)
        for seg, pr in list(ds["promoted"].items()):
            recent = data.monitor(d, pr["variant"], control, None if seg == "ALL" else seg)
            if not recent or recent.n_b < 100 or recent.n_a < 150:
                continue
            ref = pr["ref"]
            ref_lb = ref["delta"] - 1.645 * ref["se"]
            did_se = (recent.se ** 2 + ref["se"] ** 2) ** 0.5
            did_z = (recent.delta - ref_lb) / did_se if did_se > 0 else 0.0
            pr["recent"] = {**recent.as_dict(), "did_z_vs_ref_lb": round(did_z, 2)}
            flagged = recent.z <= -2.0 or (did_z <= -2.0 and recent.delta < 0)
            if not flagged:
                # clear suspicion only when the evidence turns positive again; a lift hovering just under the bar
                # is what decay looks like, and resetting on every borderline check let one decay run for 3 days
                if recent.delta > 0:
                    pr.pop("suspect_since", None)
                # confirmation without a fresh flag needs the lift clearly negative (≤ −3%), not merely ≤ 0: a real
                # winner's noisy 24h window dips below zero now and then (one false rollback in testing)
                if pr.get("suspect_since") is None or data.now_slot - pr["suspect_since"] < 6 or \
                        recent.rel > -MIN_REL:
                    continue
            if pr.get("suspect_since") is None:
                pr["suspect_since"] = data.now_slot
                decisions.append({"kind": "decay_suspected", "dim": d, "segment": seg, "variant": pr["variant"],
                                  "summary": f"{d}={pr['variant']} ({seg}) may be decaying: lift now "
                                             f"{recent.rel:+.1%} (z={recent.z:.1f}, DiD z={did_z:.1f}); "
                                             "re-checking before acting", "evidence": pr["recent"]})
                continue
            if data.now_slot - pr["suspect_since"] < 6:
                continue
            ds["promoted"].pop(seg)
            ds["arms"][pr["variant"]] = {"status": "decayed", "since_day": today,
                                         "evidence": pr["recent"], "rollback": True}
            decisions.append({"kind": "decay_detected", "dim": d, "segment": seg, "variant": pr["variant"],
                              "urgent": True,
                              "summary": f"{d}={pr['variant']} ({seg}) stopped winning: lift now "
                                         f"{recent.rel:+.1%} (z={recent.z:.1f}) vs {ref['rel_lift']:+.1%} at "
                                         f"promotion (DiD vs lower bound z={did_z:.1f}, persisted ≥6h) → "
                                         f"roll back to {control}",
                              "evidence": {"recent": recent.as_dict(), "at_promotion": ref,
                                           "did_z_vs_ref_lb": round(did_z, 2)}})
        # ---- exploring / ramped arms (only at the fixed looks)
        if not look:
            continue
        for arm, a in ds["arms"].items():
            if a["status"] not in ("exploring", "ramped"):
                continue
            r = data.compare(d, arm, control)
            if not r or r.n_a < MIN_CONN_EARLY:
                continue
            a["last"] = r.as_dict()
            if r.z <= Z_DROP:
                a.update(status="dropped", since_day=today)
                decisions.append({"kind": "arm_dropped", "dim": d, "variant": arm,
                                  "summary": f"{d}={arm} is significantly worse than {control} "
                                             f"({r.rel:+.1%}, z={r.z:.1f}); stop exposing customers to it",
                                  "evidence": r.as_dict()})
                continue
            # novelty decay while still exploring: the arm's last two days vs its own earlier lift (DiD)
            early = data.compare(d, arm, control, day_to=today - 1)
            late = data.compare(d, arm, control, day_from=today - 1)
            if early and late and early.n_a >= MIN_CONN_EARLY and late.n_a >= MIN_CONN_EARLY:
                dz = (late.delta - early.delta) / (late.se ** 2 + early.se ** 2) ** 0.5
                if dz <= -2.0 and late.delta < 0:  # dropping an arm only returns traffic to control: cheap
                    a.update(status="decayed", since_day=today, evidence={"early": early.as_dict(),
                                                                          "last_2d": late.as_dict()})
                    decisions.append({"kind": "arm_dropped", "dim": d, "variant": arm,
                                      "summary": f"{d}={arm} effect reversed: {early.rel:+.1%} earlier → "
                                                 f"{late.rel:+.1%} in the last 2 days (DiD z={dz:.1f}); novelty "
                                                 "wore off, stop exposing customers to it",
                                      "evidence": a["evidence"]})
                    continue
            # retire arms that are going nowhere: either recently negative (90% one-sided) without a strong
            # cumulative case, or confidently below the minimum lift worth promoting (futility)
            ub_rel = (r.delta + 1.645 * r.se) / r.rate_b if r.rate_b else 1.0
            recent_neg = late is not None and late.n_a >= MIN_CONN_EARLY and late.z <= -1.28
            recent_bad = late is not None and late.n_a >= MIN_CONN_EARLY and late.z <= -1.645
            if (recent_neg and r.z < Z_RAMP) or recent_bad or (r.n_a >= 600 and ub_rel < MIN_REL):
                why = (f"last 2 days {late.rel:+.1%} (z={late.z:.1f})" if recent_neg
                       else f"upper 90% bound of lift {ub_rel:+.1%} < {MIN_REL:.0%} after {r.n_a} calls")
                a.update(status="retired", since_day=today, evidence={"overall": r.as_dict(),
                                                                      "last_2d": late and late.as_dict()})
                decisions.append({"kind": "arm_retired", "dim": d, "variant": arm,
                                  "summary": f"{d}={arm} retired: {why}; not worth more customer exposure",
                                  "evidence": a["evidence"]})
                continue
            pick = data.compare(d, arm, control, metric="pickup")
            guard_ok = True
            if pick and guardrail_max_drop is not None:
                guard_ok = not (pick.z <= -2.0 and -pick.delta > guardrail_max_drop)
            recent = data.monitor(d, arm, control, None)
            recent_ok = recent is None or recent.n_a < 80 or recent.rel >= MIN_REL
            # stability: a winner must not be on a downward trend (last 2 days vs earlier, DiD)
            if early and late and early.n_a >= MIN_CONN_EARLY and late.n_a >= MIN_CONN_EARLY:
                trend_z = (late.delta - early.delta) / (late.se ** 2 + early.se ** 2) ** 0.5
                recent_ok = recent_ok and trend_z > -1.0
            if (r.z >= z_all and r.n_a >= MIN_CONN and r.n_b >= MIN_CONN and r.rel >= MIN_REL
                    and guard_ok and recent_ok and "ALL" not in ds["promoted"]):
                a.update(status="winner", since_day=today)
                ds["promoted"]["ALL"] = {"variant": arm, "since_day": today, "ref": {**r.as_dict(), "se": r.se}}
                decisions.append({"kind": "winner", "dim": d, "variant": arm, "segment": "ALL",
                                  "summary": f"{d}={arm} beats {control} for everyone: {r.rel:+.1%} engagement "
                                             f"(z={r.z:.2f} ≥ {z_all:.2f}, n={r.n_a}/{r.n_b}) → promote with "
                                             f"{int(HOLDOUT * 100)}% holdout",
                                  "evidence": {"overall": r.as_dict(), "pickup_guardrail": pick and pick.as_dict(),
                                               "recent_2d": recent and recent.as_dict(),
                                               "threshold_z": round(z_all, 2)}})
                continue
            # segment-level winners (heterogeneous effects), much stricter threshold
            if a["status"] in ("exploring", "ramped"):
                for seg in segs:
                    if seg in ds["promoted"]:
                        continue
                    rs = data.compare(d, arm, control, seg)
                    if not (rs and rs.z >= z_seg and rs.n_a >= MIN_SEG_CONN and rs.n_b >= MIN_SEG_CONN
                            and rs.rel >= MIN_REL):
                        continue
                    # same "is it still winning?" checks as a population-level promotion
                    late_s = data.compare(d, arm, control, seg, day_from=today - 1)
                    if (late is not None and late.n_a >= 80 and late.delta <= 0) or \
                            (late_s is not None and late_s.n_a >= 80 and late_s.delta <= 0):
                        continue
                    ds["promoted"][seg] = {"variant": arm, "since_day": today,
                                           "ref": {**rs.as_dict(), "se": rs.se}}
                    decisions.append({"kind": "winner", "dim": d, "variant": arm, "segment": seg,
                                      "summary": f"{d}={arm} beats {control} for {seg}: {rs.rel:+.1%} "
                                                 f"(z={rs.z:.2f} ≥ {z_seg:.2f}) → promote in that cohort",
                                      "evidence": {"segment": rs.as_dict(), "threshold_z": round(z_seg, 2)}})
            # a ramp takes traffic from the current main variant (the promoted winner if any), so it has to beat
            # that, and its last 24h must agree (no ramping something that is already decaying)
            main = ds["promoted"].get("ALL", {}).get("variant", control)
            vs_main = r if main == control else data.compare(d, arm, main)
            recent_ramp = data.monitor(d, arm, main, None)
            if (a["status"] == "exploring" and vs_main is not None and vs_main.z >= Z_RAMP
                    and (recent_ramp is None or recent_ramp.n_a < 80 or recent_ramp.delta > 0)):
                a.update(status="ramped", since_day=today)
                decisions.append({"kind": "arm_ramped", "dim": d, "variant": arm,
                                  "summary": f"{d}={arm} trending better ({r.rel:+.1%}, z={r.z:.1f}); raise its "
                                             f"share {int(EXPLORE_SHARE * 100)}%→{int(RAMP_SHARE * 100)}% "
                                             "to decide faster", "evidence": r.as_dict()})
    # every arm we stop exposing customers to carries its reason into the next proposal's rationale
    for dec in decisions:
        if dec["kind"] in ("arm_dropped", "arm_retired", "decay_detected"):
            arm_state = state[dec["dim"]]["arms"].get(dec["variant"])
            if arm_state is not None:
                arm_state["reason"] = dec["summary"]
    return decisions


def segment_features(state: dict) -> list[str]:
    out = []
    for d in DIMS:
        for seg in state[d]["promoted"]:
            fv = parse_segment(seg)
            if fv and fv[0] not in out:
                out.append(fv[0])
    return out


def _explore_shares(ds: dict) -> dict[str, float]:
    sh = {a: (RAMP_SHARE if s["status"] == "ramped" else EXPLORE_SHARE)
          for a, s in ds["arms"].items() if s["status"] in ("exploring", "ramped")}
    tot = sum(sh.values())
    if tot > MAX_EXPLORE_TOTAL:
        sh = {a: v * MAX_EXPLORE_TOTAL / tot for a, v in sh.items()}
    return sh


def _dist(main: str, control: str, explore: dict[str, float], holdout: bool) -> dict[str, float]:
    out = dict(explore)
    rest = 1.0 - sum(explore.values())
    if holdout and main != control:
        out[control] = out.get(control, 0) + HOLDOUT
        rest -= HOLDOUT
    out[main] = out.get(main, 0) + rest
    return {k: round(v, 4) for k, v in out.items() if v > 0}


def desired(state: dict, cells: list[Cohort]) -> dict[str, tuple[dict, dict]]:
    """dim -> (default distribution, {cohort_id: distribution override})."""
    out = {}
    for d in DIMS:
        ds = state[d]
        control = ds["control"]
        explore = _explore_shares(ds)
        allp = ds["promoted"].get("ALL")
        main = allp["variant"] if allp else control
        default = _dist(main, control, {k: v for k, v in explore.items() if k != main}, bool(allp))
        overrides = {}
        segp = {s: p for s, p in ds["promoted"].items() if s != "ALL"}
        for c in cells:
            best = None
            for s, p in segp.items():
                if cohort_in_segment(c, s) and (best is None or p["ref"]["rel_lift"] > best["ref"]["rel_lift"]):
                    best = p
            if best and best["variant"] != main:
                overrides[c.id] = _dist(best["variant"], control,
                                        {k: v for k, v in explore.items() if k != best["variant"]}, True)
        out[d] = (default, overrides)
    return out


def dry_run(state: dict, data: Data, old_avg: dict[str, dict], new_avg: dict[str, dict]) -> dict:
    """Expected change in engagement from moving the population-average allocation old→new, using the
    observed per-arm lifts (unknown arms count as 0 lift: their effect is what we're trying to learn)."""
    res, total, var = {}, 0.0, 0.0
    for d in DIMS:
        control = state[d]["control"]
        per = {}
        for v in set(old_avg.get(d, {})) | set(new_avg.get(d, {})):
            dw = new_avg.get(d, {}).get(v, 0) - old_avg.get(d, {}).get(v, 0)
            if abs(dw) < 1e-9 or v == control:
                per[v] = {"share_change": round(dw, 3)}
                continue
            r = data.compare(d, v, control)
            lift = r.rel if r else 0.0
            se = (r.se / r.rate_b) if r and r.rate_b else 0.1
            total += dw * lift
            var += (dw * se) ** 2
            per[v] = {"share_change": round(dw, 3), "observed_rel_lift": round(lift, 4) if r else None,
                      "n_connected": r.n_a if r else 0}
        res[d] = per
    sd = var ** 0.5
    return {"method": "population-average allocation change × observed stratified lift per arm (vs control); "
                      "arms with no data contribute 0 ± 10%", "per_dim": res,
            "expected_rel_engagement_change": round(total, 4),
            "ci90": [round(total - 1.645 * sd, 4), round(total + 1.645 * sd, 4)]}
