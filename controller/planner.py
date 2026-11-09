"""Per-workflow brain: turns module outputs into one target config and routes it through governance.

Every tick, for each workflow:
  1. read the live config (the simulator is the source of truth for what is live);
  2. notice new versions: supervisor edits get recorded, their dims become supervisor-owned for a cooldown,
     and the experiment on that dim restarts with the supervisor's choice as the new control;
  3. advance in-flight proposals (poll → apply approved → reconcile);
  4. safety first: hallucination incident flow, caller-ID emergency;
  5. hourly: experiment analysis. Daily (07:00): schedule + caller-ID plan;
  6. ops pass: auto-apply operational changes (schedule, caller_id) where policy allows and no approval is in
     flight (an auto-apply would make that approval stale);
  7. approval pass: if the target differs from live in dims that need a human, send ONE batched request, timed
     against approver hours, latency and TTL, within the request budget.

All learned state lives in `wf_state` (JSON) and is written in the same transaction as the decisions that
changed it.
"""
from __future__ import annotations

import json
import logging
import os

from . import callerid, experiments, incidents, schedule
from .cohorts import Cohort, partition, partition_size
from .configs import (AGENT_DIMS, changed_dims, copy_cfg, diff_summary, dist, max_shift, norm_dist, normalise,
                      step_towards, tv)
from .governance import Governance
from .simclient import ApiError, SimClient
from .store import Store
from .timeutil import MIN_PER_DAY, day_of, fmt_minute, hour_of

log = logging.getLogger("controller.plan")

OWN_COOLDOWN_MIN = 48 * 60
DAILY_PLAN_HOUR = 7
NORMAL_KINDS = ("optimise", "rollback")
# Ablation switch for measuring each lever's contribution (never set in production):
#   CONTROLLER_ABLATE=schedule,did,exp,incident
ABLATE = {x.strip() for x in os.environ.get("CONTROLLER_ABLATE", "").split(",") if x.strip()}


class Brain:
    def __init__(self, wf: str, spec: dict, store: Store, client: SimClient, gov: Governance, codes):
        self.wf, self.spec, self.store, self.client, self.gov, self.codes = wf, spec, store, client, gov, codes
        self.st = store.get_state(wf)
        self.cur: dict = {}
        self.ver: int = 0
        self.alerts: list[str] = []

    # ================================================================== helpers
    def save(self):
        self.store.put_state(self.wf, self.st)

    def decide(self, now, kind, summary, detail=None, pid=None):
        self.store.decide(now, self.wf, kind, summary, detail, pid)

    def refresh(self):
        c = self.client.config(self.wf)
        self.cur, self.ver = normalise(c["config"]), c["version"]
        row = self.store.q1("SELECT config FROM config_versions WHERE wf=? AND version=?", (self.wf, self.ver))
        if not row or not row[0]:
            self.store.db.execute(
                "INSERT INTO config_versions(wf, version, actor, minute, config) VALUES (?,?,?,?,?) "
                "ON CONFLICT(wf, version) DO UPDATE SET config=excluded.config",
                (self.wf, self.ver, c.get("actor"), None, json.dumps(self.cur)))

    def owned(self, now) -> set[str]:
        return {d for d, until in (self.st.get("owned") or {}).items() if until > now}

    def values(self) -> dict[str, list[str]]:
        return {f: self.codes.values(f) for f in self.codes.feature_names()}

    def window(self) -> list[int]:
        return self.spec["dialling_window"]

    # ================================================================== init
    def ensure_init(self, now):
        if self.st.get("init"):
            return
        self.st.update({
            "init": True, "owned": {}, "seen_version": self.ver,
            "did": {"quarantine": {}, "target": None},
            "schedule": {"feature": None, "tables": {}},
            "exp": (experiments.init_state(self.spec, self.cur)
                    if experiments.enabled(self.spec) and "exp" not in ABLATE else None),
            "halluc_base": incidents.baseline(self.store, self.wf),
            "prompt_since": {"prompt": self.cur["prompt_version"], "slot": -10 ** 6},
            "incident": None, "val_runs": {}, "normal_cooldown_until": -10 ** 9, "urgent": None,
            "last_daily": None, "last_hourly": None, "ops_block_until": -10 ** 9, "log_once": {},
        })
        self.decide(now, "bootstrap", f"initialised from {self.st['halluc_base']['connected']} historical connected "
                                      f"calls; baseline hallucination-flag rate "
                                      f"{self.st['halluc_base']['rate']:.2%}",
                    {"halluc_base": self.st["halluc_base"], "experiments": bool(self.st["exp"]),
                     "objective": self.spec["objective"], "governance": self.spec["governance"]})

    # ================================================================== tick
    def tick(self, now: int):
        """One decision step. HTTP calls are never made inside a DB transaction: intents are committed first
        (see governance.py), so a crash at any line leaves a state `reconcile()` can resolve."""
        self.refresh()
        with self.store.tx():
            self.ensure_init(now)
            self.save()
        self.handle_new_versions(now)
        for p in self.gov.poll(self.wf, now, self.ver):
            self.on_result(now, p)
        self.refresh()
        self.handle_new_versions(now)
        self.gov.mark_superseded_pending(self.wf, now, self.ver)
        self.incident_step(now)          # may call validation / LLM / approvals; saves state itself
        self.save()
        with self.store.tx():
            self.did_emergency(now)
            slot = day_of(now) * 24 + hour_of(now)
            if self.st.get("last_daily") != day_of(now) and hour_of(now) >= DAILY_PLAN_HOUR:
                self.daily_plan(now)
            if self.st.get("last_hourly") != slot and now % 60 == 0:
                self.hourly_analysis(now)
            self.save()
        self.ops_pass(now)
        self.approval_pass(now)
        self.save()

    # ================================================================== versions / supervisor edits
    def handle_new_versions(self, now):
        seen = self.st.get("seen_version", self.ver)
        if self.ver <= seen:
            return
        rows = self.store.q("SELECT version, actor, changed_dims, minute FROM config_versions WHERE wf=? AND version>? "
                            "ORDER BY version", (self.wf, seen))
        with self.store.tx():
            for v, actor, dims, minute in rows:
                dims = json.loads(dims or "[]")
                if actor == "supervisor":
                    self.on_supervisor_edit(now, v, dims)
            if self.cur["prompt_version"] != self.st["prompt_since"]["prompt"]:
                self.st["prompt_since"] = {"prompt": self.cur["prompt_version"],
                                           "slot": day_of(now) * 24 + hour_of(now)}
            self.st["seen_version"] = self.ver
            self.save()

    def on_supervisor_edit(self, now, version, dims):
        owned = self.st.setdefault("owned", {})
        for d in dims:
            owned[d] = now + OWN_COOLDOWN_MIN
        exp = self.st.get("exp")
        notes = []
        if exp:
            for d in experiments.DIMS:
                if d in dims:
                    # The supervisor made a call on this dimension. We respect it for the rest of the run: no new
                    # exploration against their choice, no promotions on top of it (stakeholders/04).
                    owned[d] = 10 ** 9
                    new_control = max(sorted(self.cur["allocations"]["default"][d]),
                                      key=lambda k: self.cur["allocations"]["default"][d][k])
                    ds = exp[d]
                    for arm, a in list(ds["arms"].items()):
                        if a["status"] in ("winner", "exploring", "ramped"):
                            ds["arms"][arm] = {"status": "paused_supervisor"}
                    ds["arms"].pop(new_control, None)
                    if ds["control"] != new_control:
                        ds["arms"][ds["control"]] = {"status": "paused_supervisor"}
                    ds["control"], ds["promoted"] = new_control, {}
                    notes.append(f"{d} experiment stopped: supervisor chose {new_control}; we will not re-test "
                                 "against it (shown on the supervisor view)")
        self.decide(now, "supervisor_edit",
                    f"supervisor changed {', '.join(dims)} (v{version}); not reverting. Supervisor-owned: " +
                    ", ".join(f"{d} ({'rest of run' if owned[d] > 10 ** 8 else 'until ' + fmt_minute(owned[d])})"
                              for d in dims) + ". " + "; ".join(notes),
                    {"version": version, "dims": dims, "live_config": self.cur})
        if "prompt_version" in dims:
            # A prompt went live without validation evidence (stakeholders/02 #2). Validate it now (3 credits):
            # it is the cheapest, fastest safety signal we have, long before live flags are significant.
            self.st["validate_prompt"] = self.cur["prompt_version"]
            self.decide(now, "watch", f"supervisor shipped prompt {self.cur['prompt_version']} without validation "
                                      "evidence; validating it now, and the live hallucination monitor watches it")

    # ================================================================== proposal results
    def on_result(self, now, p):
        kind, state = p["kind"], p["state"]
        inc = self.st.get("incident")
        with self.store.tx():
            if kind == "incident_fix" and inc and inc.get("proposal_id") == p["proposal_id"]:
                if state == "applied":
                    inc.update(phase="verifying", verify_since_slot=day_of(now) * 24 + hour_of(now),
                               applied_version=p["applied_version"])
                    self.decide(now, "incident", f"prompt fix {inc['candidate']} applied; verifying on live traffic",
                                {"incident": inc}, p["proposal_id"])
                elif state in ("rejected", "expired", "stale", "lapsed", "invalid", "abandoned"):
                    reasons = p.get("reasons") or []
                    if any("validation" in str(r) for r in reasons):
                        inc["tried"].append(inc["candidate"])
                        inc["phase"] = "choose"
                    else:
                        inc["phase"] = "propose"
                    inc["proposal_id"] = None
                    inc["retry_after"] = now + (60 if state != "rejected" else 120)
                    self.decide(now, "incident", f"prompt-fix request {state}; re-planning with fresh evidence",
                                {"reasons": reasons}, p["proposal_id"])
            elif kind == "did_emergency":
                pass
            elif kind in NORMAL_KINDS:
                if state in ("rejected", "expired"):
                    judgement = any("judgement" in str(r) for r in (p.get("reasons") or []))
                    # judgement-call rejection: back off 4h, then re-plan from fresh data (budget caps still apply);
                    # expiry: the approver never looked; re-plan at the next sendable slot
                    cool = 4 * 60 if judgement else 60
                    self.st["normal_cooldown_until"] = now + cool
                    self.decide(now, "replan", f"{kind} request {state}; will re-plan from fresh data after "
                                               f"{fmt_minute(now + cool)} (never resubmitting the stale proposal)",
                                {"reasons": p.get("reasons")}, p["proposal_id"])
                elif state == "invalid":
                    self.st["normal_cooldown_until"] = now + 6 * 60
                elif state == "applied" and kind == "rollback":
                    self.st["urgent"] = None
            self.save()

    # ================================================================== incidents (C8)
    def incident_step(self, now):
        slot = day_of(now) * 24 + hour_of(now)
        base = self.st["halluc_base"]["rate"]
        inc = self.st.get("incident")
        prompt = self.cur["prompt_version"]
        if "incident" in ABLATE:
            return
        if inc is None and self.st.get("validate_prompt"):
            self.validate_live_prompt(now)
            inc = self.st.get("incident")
            if inc is not None:
                return
        if inc is None:
            ev = incidents.check(self.store, self.wf, prompt, self.st["prompt_since"]["slot"], slot + 1, base)
            if ev:
                self.st["incident"] = inc = {"opened": now, "bad_prompt": prompt, "detect": ev, "phase": "choose",
                                             "tried": [], "candidate": None, "proposal_id": None}
                self.alerts.append(f"PAGE {self.wf}: hallucination flags {ev['flag_rate']:.1%} vs baseline "
                                   f"{ev['baseline_rate']:.1%} on prompt {prompt}")
                self.decide(now, "incident_opened",
                            f"hallucination spike on prompt {prompt}: {ev['flags']}/{ev['connected']} flagged "
                            f"({ev['flag_rate']:.1%}) vs baseline {ev['baseline_rate']:.1%}, p={ev['p_value']:.1e}",
                            ev)
            return
        if inc["phase"] in ("choose", "propose", "awaiting") and prompt != inc["bad_prompt"]:
            # someone (supervisor or our own apply) already moved off the bad prompt
            inc.update(phase="verifying", verify_since_slot=slot, candidate=prompt)
            self.decide(now, "incident", f"prompt is now {prompt} (moved off {inc['bad_prompt']}); verifying")
        if inc["phase"] == "choose":
            self.choose_fix(now, inc)
        if inc["phase"] == "propose" and now >= inc.get("retry_after", -1):
            self.propose_fix(now, inc)
        if inc["phase"] == "verifying":
            ok, ev = incidents.verified(self.store, self.wf, self.cur["prompt_version"], inc["verify_since_slot"],
                                        slot, base)
            if ok is True:
                self.decide(now, "incident_resolved",
                            f"prompt {self.cur['prompt_version']} verified: flag rate {ev['flag_rate']:.2%} on "
                            f"{ev['connected']} connected calls (baseline {ev['baseline_rate']:.2%}); "
                            f"time to fix {(now - inc['opened']) // 60}h{(now - inc['opened']) % 60:02d}m", ev)
                self.st["incident"] = None
            elif ok is False:
                inc["tried"].append(self.cur["prompt_version"])
                inc.update(phase="choose", bad_prompt=self.cur["prompt_version"])
                self.decide(now, "incident", f"fix {self.cur['prompt_version']} did not bring flags down; "
                                             "choosing another candidate", ev)

    def validate_live_prompt(self, now):
        pv = self.st.pop("validate_prompt")
        if pv != self.cur["prompt_version"]:
            return
        g = self.spec["governance"]
        run = self.st["val_runs"].get(pv)
        if not run:
            cost = incidents.validation_cost(incidents.VALIDATION_N)
            if self.credits() < cost + incidents.CREDIT_RESERVE:
                self.decide(now, "watch", f"not validating {pv}: credits low; relying on the live flag monitor")
                return
            try:
                run = self.client.validate(self.wf, pv, incidents.VALIDATION_N)
            except ApiError as e:
                self.decide(now, "watch", f"validation of live prompt {pv} failed: {e.code}")
                return
            self.st["val_runs"][pv] = run
            self.save()
            self.decide(now, "validation", f"validated live prompt {pv}: pass rate {run['pass_rate']:.3f} "
                                           f"({run['validation_run_id']}, policy min {g['min_validation_pass_rate']})",
                        run)
        if run["pass_rate"] < g["min_validation_pass_rate"]:
            ev = {"source": "validation", "prompt": pv, "validation_run_id": run["validation_run_id"],
                  "pass_rate": run["pass_rate"], "threshold": g["min_validation_pass_rate"], "n": run["n"],
                  "baseline_rate": round(self.st["halluc_base"]["rate"], 4)}
            self.st["incident"] = {"opened": now, "bad_prompt": pv, "detect": ev, "phase": "choose", "tried": [pv],
                                   "candidate": None, "proposal_id": None}
            self.alerts.append(f"PAGE {self.wf}: live prompt {pv} fails validation ({run['pass_rate']:.3f} < "
                               f"{g['min_validation_pass_rate']})")
            self.decide(now, "incident_opened", f"live prompt {pv} fails safety validation: pass rate "
                                                f"{run['pass_rate']:.3f} < policy {g['min_validation_pass_rate']} "
                                                f"on {run['n']} synthetic conversations", ev)

    @staticmethod
    def detect_text(d: dict) -> str:
        if d.get("source") == "validation":
            return (f"validation pass rate {d['pass_rate']:.3f} < policy {d['threshold']} "
                    f"({d['validation_run_id']}, n={d['n']})")
        return (f"hallucination flags {d['flags']}/{d['connected']} = {d['flag_rate']:.1%} vs "
                f"{d['baseline_rate']:.1%} baseline (p={d['p_value']:.1e})")

    def credits(self) -> float:
        return float(self.client.state()["credits_remaining"])

    def choose_fix(self, now, inc):
        base = self.st["halluc_base"]["rate"]
        opts = self.spec["options"]["prompt_version"]
        cands = [c for c in incidents.known_good(self.store, self.wf, opts, inc["bad_prompt"], base)
                 if c["prompt_version"] not in inc["tried"]]
        source = "known-good prompt from our production records"
        if not cands:
            if self.credits() < 3 + incidents.CREDIT_RESERVE:
                self.decide(now, "incident", "no known-good prompt and too few credits for an LLM call; escalating "
                                             "to a human", {})
                self.alerts.append(f"PAGE {self.wf}: hallucination incident with no fix candidate and low credits")
                inc["phase"] = "stuck"
                return
            try:
                r = self.client.suggest_fixes(self.wf, f"prompt {inc['bad_prompt']} hallucinates: "
                                                       f"{self.detect_text(inc['detect'])}")
            except ApiError as e:
                self.decide(now, "incident", f"LLM suggest-fixes failed: {e.code}", {})
                return
            cands = [{"prompt_version": s["prompt_version"], "source": "llm suggestion", "summary": s["summary"]}
                     for s in r["suggestions"] if s["prompt_version"] not in inc["tried"]]
            source = "LLM suggestion (the LLM proposes; validation + live verification decide)"
            self.decide(now, "llm_suggestions", f"asked LLM for fixes (3 credits): "
                                                f"{[c['prompt_version'] for c in cands]}", r)
        g = self.spec["governance"]
        for c in cands:
            pv = c["prompt_version"]
            run = self.st["val_runs"].get(pv)
            if not run:
                cost = incidents.validation_cost(incidents.VALIDATION_N)
                if self.credits() < cost + incidents.CREDIT_RESERVE:
                    self.decide(now, "incident", "credits too low to validate; escalating", {})
                    inc["phase"] = "stuck"
                    return
                try:
                    run = self.client.validate(self.wf, pv, incidents.VALIDATION_N)
                except ApiError as e:
                    self.decide(now, "incident", f"validation call failed: {e.code}", {})
                    return
                self.st["val_runs"][pv] = run
                self.save()  # paid for: persist before anything else can fail
                self.decide(now, "validation", f"validated {pv}: pass rate {run['pass_rate']:.3f} on "
                                               f"{run['n']} synthetic conversations ({run['validation_run_id']})", run)
            if run["pass_rate"] >= g["min_validation_pass_rate"] + incidents.VALIDATION_MARGIN:
                inc.update(candidate=pv, candidate_info=c, validation=run, phase="propose", source=source)
                self.decide(now, "incident", f"fix chosen: {inc['bad_prompt']}→{pv} ({source}); validation "
                                             f"{run['pass_rate']:.3f} ≥ {g['min_validation_pass_rate']}+margin",
                            {"candidate": c, "validation": run})
                return
            inc["tried"].append(pv)
        self.decide(now, "incident", "no candidate passed validation; escalating to a human", {"tried": inc["tried"]})
        self.alerts.append(f"PAGE {self.wf}: no validated prompt fix available")
        inc["phase"] = "stuck"

    def propose_fix(self, now, inc):
        if any(p["kind"] == "incident_fix" for p in self.gov.in_flight(self.wf)):
            inc["phase"] = "awaiting"
            return
        ok, why = self.gov.can_request(self.wf, now, urgent=True)
        if not ok:
            self.log_once(now, f"incident_wait_{why[:20]}", f"incident fix ready but cannot request yet: {why}")
            return
        cfg = copy_cfg(self.cur)
        cfg["prompt_version"] = inc["candidate"]
        d = inc["detect"]
        stats = incidents.prompt_stats(self.store, self.wf)
        cand = stats.get(inc["candidate"], {})
        fields = {
            "change_summary": f"Safety fix: switch prompt {inc['bad_prompt']} → {inc['candidate']} "
                              f"({'fails validation' if d.get('source') == 'validation' else 'hallucination spike'})",
            "diff": {"prompt_version": {"before": inc["bad_prompt"], "after": inc["candidate"]}},
            "affected_leads": self.active_leads_total(now),
            "dry_run": {"method": "historical replay of the candidate prompt on this workflow"
                        if cand.get("connected") else "no production history for candidate; validation only",
                        "candidate_history": {k: (round(v, 4) if isinstance(v, float) else v)
                                              for k, v in cand.items()},
                        "current_prompt_live": d, "validation": inc["validation"]},
            "impact_score": 0.9,
            "rationale": f"Prompt {inc['bad_prompt']}: {self.detect_text(d)}. Candidate "
                         f"{inc['candidate']} chosen as {inc.get('source')}; validation pass rate "
                         f"{inc['validation']['pass_rate']:.3f}. We will verify on live calls after apply and "
                         "re-open if flags stay high. This does not undo the supervisor's other settings.",
            "evidence": {"validation_run_id": inc["validation"]["validation_run_id"], "detection": d},
        }
        p = self.gov.submit(self.wf, "incident_fix", now, self.ver, cfg, fields,
                            {"incident": {k: inc[k] for k in ("opened", "bad_prompt", "candidate")}})
        if p:
            inc.update(phase="awaiting", proposal_id=p["proposal_id"])
        else:
            inc["retry_after"] = now + 60

    # ================================================================== caller ID (C5)
    def did_emergency(self, now):
        slot = day_of(now) * 24 + hour_of(now)
        live = dist(self.cur, "default", "caller_id")
        q = self.st["did"]["quarantine"]
        for b in callerid.emergency_check(self.store, self.wf, slot + 1, live):
            if b["did"] in q:
                continue
            q[b["did"]] = {"since": now, "reason": "sudden pickup collapse", **b}
            self.alerts.append(f"ALERT {self.wf}: caller ID {b['did']} pickup collapsed to {b['pickup_3h']:.1%} "
                               f"(expected {b['expected']:.1%}); rotating it out")
            self.decide(now, "did_flagged", f"caller ID {b['did']} collapsed: {b['pickup_3h']:.1%} pickup over "
                                            f"{b['dials_3h']} dials vs {b['expected']:.1%} expected (z={b['z']}); "
                                            "quarantined", b)
            self.st["did"]["target"] = self.redistribute(live, q)
        tgt = self.st["did"].get("target")
        if tgt and any(live.get(d, 0) > 0 for d in q):
            self.st["did"]["emergency"] = True

    def redistribute(self, live: dict, q: dict) -> dict:
        keep = {d: w for d, w in live.items() if d not in q and w > 0}
        if not keep:
            opts = [d for d in self.spec["options"]["caller_id"] if d not in q]
            keep = {d: 1.0 for d in opts[:2]} if opts else live
        n = len(keep)
        return norm_dist({d: 1.0 / n for d in keep})  # equal split: spreads the displaced load

    # ================================================================== hourly / daily analysis
    def hourly_analysis(self, now):
        self.st["last_hourly"] = day_of(now) * 24 + hour_of(now)
        exp = self.st.get("exp")
        if not exp:
            return
        slot = day_of(now) * 24 + hour_of(now)
        data = experiments.Data(self.store, self.wf, self.codes.feats, now_slot=slot)
        guard = self.spec["objective"].get("guardrail") or {}
        decs = experiments.analyse(exp, self.spec, data, day_of(now), guard.get("pickup_rate_max_drop"),
                                   look=hour_of(now) in experiments.LOOK_HOURS)
        for d in decs:
            self.decide(now, d["kind"], d["summary"], d)
            if d.get("urgent"):
                self.st["urgent"] = {"reason": d["summary"], "since": now}
                self.alerts.append(f"ALERT {self.wf}: {d['summary']}")

    def daily_plan(self, now):
        self.st["last_daily"] = day_of(now)
        today = day_of(now)
        # schedule
        sp = None if "schedule" in ABLATE else schedule.plan(self.store, self.wf, today, self.codes.feats, self.codes.feature_names(), self.window())
        old = self.st["schedule"]
        if sp and sp["feature"]:
            if sp["feature"] != old.get("feature") or schedule.material_change(old.get("tables") or {}, sp["tables"]):
                self.st["schedule"] = {"feature": sp["feature"], "tables": sp["tables"], "day": today}
                self.decide(now, "schedule_plan", f"call-time: dial by {sp['feature']}; weights updated from "
                                                  f"{sp['evidence']['feature_scores'][sp['feature']]['dials']} dials",
                            sp["evidence"])
        elif sp:
            self.log_once(now, "sched_none", "call-time: no feature shows a significant hour×value interaction; "
                                             "schedule left alone", sp["evidence"])
        self.guardrail_check(now)
        # caller ID
        if "did" in ABLATE:
            return
        slot = today * 24 + hour_of(now)
        live = dist(self.cur, "default", "caller_id")
        q = self.st["did"]["quarantine"]
        target, ev = callerid.plan_allocation(self.store, self.wf, slot, self.spec["options"]["caller_id"], live, q)
        for d, why in ev.get("newly_bad", {}).items():
            q[d] = {"since": now, "reason": "intrinsically poor pickup at low share", **why}
            self.decide(now, "did_quarantined", f"caller ID {d} quarantined: pickup {why['pickup']:.1%} vs "
                                                f"{why['best_pickup']:.1%} on {why['best']} (z={why['z']})", why)
        if callerid.material_change(live, target):
            self.st["did"]["target"] = target
            self.decide(now, "did_plan", f"caller-ID target {target} (live {live})", ev)
        else:
            self.st["did"]["target"] = None

    def guardrail_check(self, now):
        """Daily: is yesterday's guardrail KPI significantly below the pre-D0 baseline by more than the allowed drop?
        `*_max_drop` is read as an absolute drop in the rate (assumption A3 in SUBMISSION.md)."""
        g = self.spec["objective"].get("guardrail") or {}
        if not g:
            return
        key, max_drop = next(iter(g.items()))
        d = day_of(now) - 1
        if d < 0:
            return

        def rates(lo, hi):
            r = self.store.q1("SELECT SUM(dials), SUM(conn), SUM(eng) FROM agg_prompt WHERE wf=? AND slot>=? AND "
                              "slot<?", (self.wf, lo, hi))
            return (r[0] or 0), (r[1] or 0), (r[2] or 0)
        bn, bc, be = rates(-10 ** 6, 0)
        yn, yc, ye = rates(d * 24, d * 24 + 24)
        if key.startswith("pickup"):
            base, now_r, n = (bc / bn if bn else None), (yc / yn if yn else None), yn
        else:
            base, now_r, n = (be / bc if bc else None), (ye / yc if yc else None), yc
        if base is None or now_r is None or n < 300:
            return
        se = (now_r * (1 - now_r) / n) ** 0.5
        drop = base - now_r
        ev = {"guardrail": key, "max_drop_abs": max_drop, "baseline": round(base, 4), "yesterday": round(now_r, 4),
              "drop": round(drop, 4), "n": n}
        if drop > max_drop and (drop - max_drop) / se > 2:
            self.alerts.append(f"ALERT {self.wf}: guardrail {key} breached yesterday: {now_r:.1%} vs baseline "
                               f"{base:.1%} (allowed drop {max_drop})")
            self.decide(now, "guardrail_breach", f"{key}: {now_r:.1%} yesterday vs {base:.1%} baseline", ev)
        else:
            self.decide(now, "guardrail_ok", f"{key}: {now_r:.1%} yesterday vs {base:.1%} baseline "
                                             f"(drop {drop:+.3f}, allowed {max_drop})", ev)

    # ================================================================== composition
    def partition_features(self) -> list[str]:
        vals = self.values()
        feats = []
        f = self.st["schedule"].get("feature")
        if f:
            feats.append(f)
        if self.st.get("exp"):
            for g in experiments.segment_features(self.st["exp"]):
                if g not in feats and partition_size(feats + [g], vals) <= 20:
                    feats.append(g)
        return feats

    def compose(self, now, ops_only: bool = False) -> tuple[dict, dict]:
        """Target config + plan description. ops_only: only schedule/caller_id on existing cohorts."""
        owned = self.owned(now)
        cur = self.cur
        t = copy_cfg(cur)
        plan: dict = {}
        vals = self.values()
        if ops_only or "cohorts" in owned:
            cells = [Cohort(c["id"], c["where"]) for c in cur["cohorts"]]
        else:
            feats = self.partition_features()
            cells = partition(feats, vals)
            t["cohorts"] = [c.to_config() for c in cells]
            if [c["id"] for c in t["cohorts"]] != [c["id"] for c in cur["cohorts"]]:
                plan["cohorts"] = {"features": feats, "n": len(cells)}
        ids = {c.id for c in cells}
        allocs = {"default": dict(cur["allocations"]["default"])}
        for k, v in cur["allocations"].items():
            if k != "default" and k in ids:
                allocs[k] = dict(v)
        exp = self.st.get("exp")
        if exp and not ops_only:
            want = experiments.desired(exp, cells)
            for d in experiments.DIMS:
                if d in owned:
                    continue
                default, overrides = want[d]
                allocs["default"][d] = default
                for k in list(allocs):
                    if k != "default":
                        allocs[k].pop(d, None)
                for cid, dd in overrides.items():
                    allocs.setdefault(cid, {})[d] = dd
                plan.setdefault("experiments", {})[d] = {
                    "control": exp[d]["control"],
                    "arms": {a: s["status"] for a, s in exp[d]["arms"].items()},
                    "promoted": {s: p["variant"] for s, p in exp[d]["promoted"].items()}}
        tgt = self.st["did"].get("target")
        if tgt and "caller_id" not in owned:
            allocs["default"]["caller_id"] = tgt
            plan["caller_id"] = tgt
        t["allocations"] = {k: v for k, v in allocs.items() if k == "default" or v}
        sch = self.st["schedule"]
        if sch.get("feature") and "schedule" not in owned:
            hw = {}
            for c in cells:
                vals_c = c.where.get(sch["feature"])
                if vals_c and len(vals_c) == 1 and vals_c[0] in sch["tables"]:
                    hw[c.id] = sch["tables"][vals_c[0]]
            if hw:
                t["schedule"]["hour_weights"] = hw
                plan["schedule"] = {"feature": sch["feature"]}
        else:
            t["schedule"]["hour_weights"] = {k: v for k, v in cur["schedule"]["hour_weights"].items()
                                             if k == "default" or k in ids}
        return normalise(t), plan

    # ================================================================== ops pass (auto-apply)
    def ops_pass(self, now):
        auto = self.gov.auto_dims(self.wf)
        emergency = self.st["did"].get("emergency")
        if not auto:
            if emergency:
                self.did_emergency_approval(now)
            return
        if now < self.st.get("ops_block_until", -10 ** 9):
            return
        if self.gov.in_flight(self.wf, ("optimise", "rollback", "incident_fix")) and not emergency:
            return
        target, plan = self.compose(now, ops_only=True)
        dims = changed_dims(self.cur, target)
        dims_ok = [d for d in dims if d in auto]
        if not dims_ok:
            if emergency and "caller_id" not in auto:
                self.did_emergency_approval(now)
            return
        # keep only the auto-eligible parts
        t = copy_cfg(self.cur)
        if "caller_id" in dims_ok:
            cur_d = dist(self.cur, "default", "caller_id")
            t["allocations"]["default"]["caller_id"] = step_towards(cur_d, target["allocations"]["default"]["caller_id"],
                                                                    self.gov.max_shift(self.wf))
        if "schedule" in dims_ok:
            t["schedule"] = target["schedule"]
        t = normalise(t)
        if max_shift(self.cur, t) > self.gov.max_shift(self.wf) + 1e-9:
            return
        dims_final = changed_dims(self.cur, t)
        if not dims_final:
            return
        evidence, rationale = {}, []
        if "caller_id" in dims_final:
            q = self.st["did"]["quarantine"]
            evidence["caller_id"] = {"from": dist(self.cur, "default", "caller_id"),
                                     "to": t["allocations"]["default"]["caller_id"],
                                     "quarantined": {d: {k: v for k, v in i.items() if k in ("reason", "pickup",
                                                     "pickup_3h", "expected", "z")} for d, i in q.items()},
                                     "dry_run": self.did_dry_run(dist(self.cur, "default", "caller_id"),
                                                                 t["allocations"]["default"]["caller_id"], now)}
            rationale.append("rotate caller IDs: " + ("emergency move off quarantined "
                             f"{sorted(q)}" if emergency else "spread load over healthy IDs, explore unproven IDs at 5%"))
        if "schedule" in dims_final:
            evidence["schedule"] = {"feature": self.st["schedule"]["feature"]}
            rationale.append(f"call-time weights by {self.st['schedule']['feature']} from observed pickup per hour")
        kind = "did_emergency" if emergency and "caller_id" in dims_final else "ops"
        v = self.gov.auto_apply(self.wf, kind, now, self.ver, t, "; ".join(rationale), evidence,
                                {"dims": dims_final, **plan})
        if v is None:
            self.st["ops_block_until"] = now + 6 * 60
        else:
            self.refresh()
            live = dist(self.cur, "default", "caller_id")
            if not any(live.get(d, 0) > 0 for d in self.st["did"]["quarantine"]):
                self.st["did"]["emergency"] = False
        with self.store.tx():
            self.save()

    def did_emergency_approval(self, now):
        if self.gov.in_flight(self.wf, ("did_emergency",)):
            return
        live = dist(self.cur, "default", "caller_id")
        q = self.st["did"]["quarantine"]
        if not any(live.get(d, 0) > 0 for d in q):
            self.st["did"]["emergency"] = False
            return
        ok, why = self.gov.can_request(self.wf, now, urgent=True)
        if not ok:
            self.log_once(now, f"did_wait_{why[:15]}", f"caller-ID emergency waiting for approver: {why}")
            return
        t = copy_cfg(self.cur)
        tgt = self.redistribute(live, q)
        t["allocations"]["default"]["caller_id"] = tgt
        bad = sorted(d for d in q if live.get(d, 0) > 0)
        next_only = self.spec.get("mid_flight") == "next_campaign_only"
        fields = {"change_summary": f"Urgent: stop dialling from caller ID {', '.join(bad)} (pickup collapsed)",
                  "diff": diff_summary(self.cur, t), "affected_leads": self.affected_leads(now, t),
                  "dry_run": self.did_dry_run(live, tgt, now), "impact_score": 0.85,
                  "rationale": f"{', '.join(bad)} pickup collapsed vs peers in the same hours (see evidence). "
                               "Remaining IDs share the load equally." +
                               (" Note: this workflow locks caller IDs per campaign, so it takes effect from the "
                                "next campaign." if next_only else ""),
                  "evidence": {d: q[d] for d in bad}}
        p = self.gov.submit(self.wf, "did_emergency", now, self.ver, t, fields, {"caller_id": tgt})
        if p is None:
            self.st["did"]["emergency"] = False

    # ================================================================== approval pass
    def approval_pass(self, now):
        if self.gov.in_flight(self.wf, NORMAL_KINDS):
            return
        if now < self.st.get("normal_cooldown_until", -10 ** 9):
            return
        target, plan = self.compose(now)
        dims = changed_dims(self.cur, target)
        if not dims:
            return
        auto = self.gov.auto_dims(self.wf)
        needs_human = [d for d in dims if d not in auto]
        if not needs_human:
            return  # operational-only: the ops pass auto-applies it (in max_shift steps if needed)
        urgent = bool(self.st.get("urgent"))
        ok, why = self.gov.can_request(self.wf, now, urgent=urgent)
        if not ok:
            self.log_once(now, f"wait_{why[:24]}", f"change ready ({', '.join(dims)}) but waiting: {why}",
                          {"dims": dims})
            return
        kind = "rollback" if urgent else "optimise"
        fields = self.build_request(now, target, dims, plan, kind)
        p = self.gov.submit(self.wf, kind, now, self.ver, target, fields, {"dims": dims, **plan})
        with self.store.tx():
            if p is None:
                self.st["normal_cooldown_until"] = now + 60
            self.save()

    # ================================================================== request content (C10)
    def build_request(self, now, target, dims, plan, kind) -> dict:
        primary = self.spec["objective"]["primary"]
        diff = diff_summary(self.cur, target)
        dry: dict = {}
        exp_lift = 0.0
        pick_lift = 0.0
        parts = []
        exp = self.st.get("exp")
        if exp and any(d in dims for d in experiments.DIMS):
            data = experiments.Data(self.store, self.wf, self.codes.feats)
            old_avg, new_avg = self.population_avg(self.cur), self.population_avg(target)
            dr = experiments.dry_run(exp, data, old_avg, new_avg)
            dry["engagement"] = dr
            exp_lift = dr["expected_rel_engagement_change"]
            for d in experiments.DIMS:
                if d in dims:
                    ds = exp[d]
                    prom = {s: p["variant"] for s, p in ds["promoted"].items()}
                    expl = [a for a, s in ds["arms"].items() if s["status"] in ("exploring", "ramped")]
                    bits = []
                    if prom:
                        bits.append("promote " + ", ".join(f"{v} for {s}" for s, v in prom.items()))
                    if expl:
                        bits.append("test " + "/".join(expl) + f" vs {ds['control']}")
                    rb = [a for a, s in ds["arms"].items() if s["status"] in ("decayed", "dropped")
                          and any(v.get(a, 0) > 0 for v in [dist(self.cur, k, d) for k in self.cur["allocations"]])]
                    if rb:
                        bits.append("remove " + "/".join(rb))
                    parts.append(f"{d.replace('_message', '')}: " + "; ".join(bits or ["rebalance"]))
        if "schedule" in dims and self.st["schedule"].get("feature"):
            f = self.st["schedule"]["feature"]
            old_t = self._tables_by_value(self.cur, f)
            dr = schedule.dry_run(self.store, self.wf, day_of(now), self.codes.feats, f, old_t,
                                  self.st["schedule"]["tables"], self.window())
            dry["schedule"] = dr
            pick_lift += dr.get("expected_rel_change") or 0.0
            parts.append(f"call-time by {f}")
        if "caller_id" in dims:
            dr = self.did_dry_run(dist(self.cur, "default", "caller_id"), dist(target, "default", "caller_id"), now)
            dry["caller_id"] = dr
            pick_lift += dr.get("expected_rel_change") or 0.0
            parts.append("caller IDs " + "/".join(sorted(dist(target, "default", "caller_id"))))
        if "cohorts" in dims:
            parts.insert(0, f"cohorts by {'×'.join(plan.get('cohorts', {}).get('features', []))}")
        expected = exp_lift if primary == "engagement_rate" else pick_lift
        if kind == "rollback":
            impact = 0.8
        else:
            impact = round(min(0.75, max(0.05, expected / 0.10 * 0.75)), 3)
        summary = ("ROLLBACK — " if kind == "rollback" else "") + "; ".join(parts)
        why = []
        if kind == "rollback" and self.st.get("urgent"):
            why.append(self.st["urgent"]["reason"])
        if exp:
            for d in experiments.DIMS:
                for s, p in exp[d]["promoted"].items():
                    r = p["ref"]
                    why.append(f"{d}={p['variant']} won for {s}: {r['rel_lift']:+.1%} engagement "
                               f"(z={r['z']}, n={r['n_challenger']}/{r['n_control']}), {int(experiments.HOLDOUT*100)}% "
                               "holdout kept to detect decay")
        if exp:
            for d in experiments.DIMS:
                if d not in dims:
                    continue
                for arm, x in exp[d]["arms"].items():
                    live_somewhere = any(dist(self.cur, k, d).get(arm, 0) > 0 for k in self.cur["allocations"])
                    if x["status"] in ("dropped", "decayed", "retired") and live_somewhere and x.get("reason"):
                        why.append(x["reason"])
        if "schedule" in dims:
            why.append(f"pickup varies by hour differently across {self.st['schedule']['feature']} values "
                       "(likelihood-ratio test on history); weights favour each cohort's better hours, "
                       "every hour stays > 0")
        if "caller_id" in dims:
            why.append("spread dials over healthy caller IDs (load lowers pickup), explore unproven IDs at 5%")
        if exp and any(d in dims for d in experiments.DIMS) and not any(exp[d]["promoted"] for d in experiments.DIMS):
            why.append("exploration: challengers capped at 10–20% each so a bad variant can cost at most a few "
                       "tenths of a percent of engagement while we learn")
        if self.spec.get("mid_flight") == "next_campaign_only" and set(dims) & {"cohorts", *AGENT_DIMS}:
            why.append("this workflow locks cohorts/allocations per campaign: they apply from the next campaign")
        return {"change_summary": summary[:300] or "rebalance",
                "diff": diff or {"note": "no-op"},
                "affected_leads": self.affected_leads(now, target),
                "dry_run": dry or {"note": "no measurable change expected"},
                "impact_score": impact,
                "rationale": " | ".join(why) or "see diff",
                "evidence": {"plan": plan, "primary_kpi": primary,
                             "expected_rel_change_primary": round(expected, 4)}}

    def _tables_by_value(self, cfg, feature) -> dict:
        out = {}
        for c in cfg["cohorts"]:
            v = c["where"].get(feature)
            if v and len(v) == 1 and c["id"] in cfg["schedule"]["hour_weights"]:
                out[v[0]] = cfg["schedule"]["hour_weights"][c["id"]]
        return out

    def population_avg(self, cfg) -> dict[str, dict]:
        """Population-average distribution per dim, weighting cohorts by their share of active leads."""
        counts = self.active_lead_codes(None)
        cells = [Cohort(c["id"], c["where"]) for c in cfg["cohorts"]]
        tot = sum(counts.values()) or 1
        out: dict[str, dict] = {}
        for d in experiments.DIMS:
            acc: dict[str, float] = {}
            for code, n in counts.items():
                f = self.codes.feats.get(code, {})
                cid = next((c.id for c in cells if c.matches(f)), "default")
                for o, w in dist(cfg, cid, d).items():
                    acc[o] = acc.get(o, 0) + w * n / tot
            out[d] = acc
        return out

    def active_lead_codes(self, now) -> dict[int, int]:
        today = self.store.get_meta("cursor_day", 0)
        horizon = len(self.spec["cadence"])
        return dict(self.store.q("SELECT code, COUNT(*) FROM leads WHERE wf=? AND done=0 AND day>? GROUP BY code",
                                 (self.wf, today - horizon)))

    def active_leads_total(self, now) -> int:
        return int(sum(self.active_lead_codes(now).values()))

    def affected_leads(self, now, target) -> int:
        """Active leads whose effective treatment changes (any agent dim or schedule table differs)."""
        if self.spec.get("mid_flight") == "next_campaign_only":
            dims = changed_dims(self.cur, target)
            if set(dims) - {"schedule", "prompt_version"}:
                return int(self.spec["daily_batch"]["size"])  # only new campaigns see agent-facing changes
        counts = self.active_lead_codes(now)
        old_cells = [Cohort(c["id"], c["where"]) for c in self.cur["cohorts"]]
        new_cells = [Cohort(c["id"], c["where"]) for c in target["cohorts"]]
        n = 0
        for code, k in counts.items():
            f = self.codes.feats.get(code, {})
            co = next((c.id for c in old_cells if c.matches(f)), "default")
            cn = next((c.id for c in new_cells if c.matches(f)), "default")
            changed = any(dist(self.cur, co, d) != dist(target, cn, d) for d in AGENT_DIMS)
            hw_o = self.cur["schedule"]["hour_weights"].get(co, self.cur["schedule"]["hour_weights"].get("default"))
            hw_n = target["schedule"]["hour_weights"].get(cn, target["schedule"]["hour_weights"].get("default"))
            if changed or hw_o != hw_n or self.cur["prompt_version"] != target["prompt_version"]:
                n += k
        return int(n)

    def did_dry_run(self, old: dict, new: dict, now) -> dict:
        slot = day_of(now) * 24 + hour_of(now)
        win = callerid.totals(callerid.did_window(self.store, self.wf, slot - 72, slot))
        tot_n = sum(v[0] for v in win.values())
        mean = sum(v[1] for v in win.values()) / tot_n if tot_n else 0.0
        rate = {d: (v[1] / v[0] if v[0] >= 50 else mean) for d, v in win.items()}

        def exp(dd):
            return sum(w * rate.get(d, mean) for d, w in dd.items())
        o, n = exp(old), exp(new)
        return {"method": "last 72h pickup per caller ID (unmeasured IDs = fleet mean), ignores load relief",
                "pickup_by_did": {d: round(r, 4) for d, r in rate.items()},
                "expected_pickup_old": round(o, 4), "expected_pickup_new": round(n, 4),
                "expected_rel_change": round(n / o - 1, 4) if o else None, "tv_shift": round(tv(old, new), 3)}

    def log_once(self, now, key, summary, detail=None):
        lo = self.st.setdefault("log_once", {})
        stamp = f"{key}@{day_of(now)}"
        if lo.get(key) == stamp:
            return
        lo[key] = stamp
        self.decide(now, "waiting", summary, detail)
