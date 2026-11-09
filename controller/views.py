"""Read-only views over the controller's store. Safe to run while the controller is running (SQLite WAL).

  status : loop health for on-call: ingest lag, decisions/hour, credit burn, memory, open alerts (C14)
  view   : the supervisor's one screen per workflow (C11): what's being tested, what's winning, what we need from
           you, what changed today, open incidents
  audit  : "why did workflow X change at time T, who approved it, and what happened next?" (C12)
  log    : the raw decision trail, filterable by workflow / kind
"""
from __future__ import annotations

import json

from .configs import dist
from .timeutil import MIN_PER_DAY, fmt_minute, parse_label


def _j(x):
    return json.loads(x) if isinstance(x, str) and x else (x or {})


def _now(s) -> int:
    r = s.q1("SELECT MAX(minute) FROM metrics")
    return r[0] if r and r[0] is not None else (s.get_meta("cursor_minute", 0) or 0)


def _fmt_dist(d: dict) -> str:
    return " ".join(f"{k}:{v:.0%}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------------------------------------------- status
def status(s, a) -> str:
    now = _now(s)
    L = [f"scenario {s.get_meta('scenario')}  ·  sim time {fmt_minute(now)}  ·  controller starts: "
         f"{(s.get_meta('restarts', 0) or 0) + 1}", ""]
    m = s.q("SELECT wall, minute, events, ingest_s, decide_s, advance_s, lag_events, credits, rss_mb, decisions "
            "FROM metrics ORDER BY id")
    if m:
        last = m[-1]
        day = [r for r in m if r[1] > now - MIN_PER_DAY]
        ev = sum(r[2] for r in day)
        ing = sum(r[3] for r in day) or 1e-9
        wall_day = (day[-1][0] - day[0][0]) if len(day) > 1 else 0
        credits_day = (day[0][7] - day[-1][7]) if day else 0
        L += ["loop health (last simulated 24h)",
              f"  ingest lag now          : {last[6]} events behind the simulator",
              f"  events ingested         : {ev:,}  ({ev / ing:,.0f} events/s while ingesting)",
              f"  wall-clock per sim day  : {wall_day:,.0f} s   (ingest {ing:,.1f}s · decide "
              f"{sum(r[4] for r in day):,.1f}s · simulator {sum(r[5] for r in day):,.1f}s)",
              f"  decisions               : {sum(r[9] for r in day)} ({sum(r[9] for r in day) / 24:.1f}/h)",
              f"  credits                 : {last[7]:.0f} remaining, {credits_day:.0f} spent in 24h",
              f"  peak memory (RSS)       : {max(r[8] for r in m):,.0f} MB", ""]
    infl = s.q("SELECT wf, kind, state, approval_id, created_minute FROM proposals WHERE state IN "
               "('submitting','pending','approved','applying') ORDER BY created_minute")
    L.append(f"in-flight proposals: {len(infl)}")
    for wf, kind, st, aid, cm in infl:
        L.append(f"  {wf:<16} {kind:<14} {st:<9} {aid or '-':<10} since {fmt_minute(cm)}")
    al = s.q("SELECT minute, wf, summary FROM decisions WHERE kind='alert' AND minute > ? ORDER BY id",
             (now - MIN_PER_DAY,))
    L += ["", f"alerts in the last 24h: {len(al)}"] + [f"  {fmt_minute(t)} {wf}: {x}" for t, wf, x in al]
    err = s.q("SELECT minute, wf, summary FROM decisions WHERE kind='error' ORDER BY id DESC LIMIT 5")
    if err:
        L += ["", "recent errors:"] + [f"  {fmt_minute(t)} {wf}: {x}" for t, wf, x in err]
    return "\n".join(L)


# ---------------------------------------------------------------------------------------------------- view
def view(s, a) -> str:
    now = parse_label(a.at) if a.at else _now(s)
    wfs = [a.workflow] if a.workflow else [r[0] for r in s.q("SELECT wf FROM workflows ORDER BY wf")]
    return "\n\n".join(_view_one(s, wf, now) for wf in wfs)


def _live_config(s, wf, now):
    r = s.q1("SELECT version, config FROM config_versions WHERE wf=? AND config IS NOT NULL AND "
             "(minute IS NULL OR minute<=?) ORDER BY version DESC LIMIT 1", (wf, now))
    return (r[0], _j(r[1])) if r else (None, None)


def _view_one(s, wf, now) -> str:
    spec = _j(s.q1("SELECT spec FROM workflows WHERE wf=?", (wf,))[0])
    st = s.get_state(wf)
    ver, cfg = _live_config(s, wf, now)
    obj = spec["objective"]
    g = next(iter(obj.get("guardrail", {}).items()), ("-", "-"))
    L = ["=" * 100, f"{wf}  ({spec['tenant_id']}, {spec.get('use_case')})  ·  {fmt_minute(now)}",
         f"objective: maximise {obj['primary']}  ·  guardrail: {g[0]} {g[1]}  ·  live config v{ver}", "=" * 100]
    # KPIs: today vs yesterday vs history
    def kpi(lo, hi):
        r = s.q1("SELECT SUM(dials), SUM(conn), SUM(eng) FROM agg_prompt WHERE wf=? AND slot>=? AND slot<?",
                 (wf, lo, hi))
        n, c, e = (r[0] or 0), (r[1] or 0), (r[2] or 0)
        return f"pickup {c / n:6.1%}  engagement {e / c:6.1%}  ({n:,} dials)" if n and c else "no calls"
    d = now // MIN_PER_DAY
    L += ["KPIs (never blended)",
          f"  today      {kpi(d * 24, d * 24 + 24)}",
          f"  yesterday  {kpi((d - 1) * 24, d * 24)}",
          f"  history    {kpi(-10 ** 6, 0)}   (pre-D0, business default config)", ""]
    # what we need from you
    pend = s.q("SELECT kind, approval_id, created_minute, request FROM proposals WHERE wf=? AND state IN "
               "('pending','approved')", (wf,))
    L.append("WHAT WE NEED FROM YOU")
    if not pend:
        L.append("  nothing right now")
    for kind, aid, cm, req in pend:
        req = _j(req)
        L.append(f"  [{aid}] {req.get('change_summary')}  (sent {fmt_minute(cm)}, impact {req.get('impact_score')}, "
                 f"{req.get('affected_leads')} leads)")
    # tests
    L += ["", "WHAT'S BEING TESTED / WHAT'S WINNING  (arm status = latest learned state; use `log` for history)"]
    exp = st.get("exp")
    if not exp:
        L.append("  no welcome/voice experiments on this workflow (primary KPI is pickup; those dims act after pickup)")
    else:
        for dim in ("welcome_message", "voice"):
            ds = exp[dim]
            live = _fmt_dist(dist(cfg, "default", dim)) if cfg else "?"
            L.append(f"  {dim}: control {ds['control']}  ·  live (default cohort) {live}")
            for seg, p in ds["promoted"].items():
                r = p["ref"]
                rec = p.get("recent") or {}
                L.append(f"    WINNER {p['variant']} for {seg}: {r['rel_lift']:+.1%} at promotion (z={r['z']}); "
                         f"last 24h {rec.get('rel_lift', float('nan')):+.1%}")
            for arm, x in sorted(ds["arms"].items()):
                last = x.get("last") or x.get("evidence") or {}
                lift = last.get("rel_lift")
                L.append(f"    {arm:<4} {x['status']:<18} " +
                         (f"{lift:+.1%} vs control (z={last.get('z')}, n={last.get('n_challenger')})"
                          if lift is not None else "not enough data yet"))
    # caller IDs and schedule
    L += ["", "OPERATIONS"]
    if cfg:
        L.append(f"  caller IDs live: {_fmt_dist(dist(cfg, 'default', 'caller_id'))}")
    q = (st.get("did") or {}).get("quarantine") or {}
    for did, i in q.items():
        L.append(f"  caller ID {did} QUARANTINED since {fmt_minute(i['since'])}: {i.get('reason')}")
    sch = st.get("schedule") or {}
    L.append(f"  call-time: by {sch['feature']} ({len(sch['tables'])} groups)" if sch.get("feature")
             else "  call-time: business default (no significant hour × cohort pattern found)")
    if cfg and cfg.get("cohorts"):
        L.append(f"  cohorts: {', '.join(c['id'] for c in cfg['cohorts'])}")
    inc = st.get("incident")
    if inc:
        L.append(f"  OPEN INCIDENT since {fmt_minute(inc['opened'])}: prompt {inc['bad_prompt']} — phase {inc['phase']}"
                 f", fix candidate {inc.get('candidate')}")
    own = {k: v for k, v in (st.get("owned") or {}).items() if v > now}
    if own:
        L.append("  supervisor-owned (we won't change): " +
                 ", ".join(f"{k} ({'rest of run' if v > 10 ** 8 else 'until ' + fmt_minute(v)})" for k, v in own.items()))
    # changes today
    L += ["", "WHAT CHANGED TODAY"]
    rows = s.q("SELECT version, actor, minute, changed_dims, approval_id, proposal_id FROM config_versions WHERE wf=? "
               "AND minute>=? AND minute<=? ORDER BY version", (wf, d * MIN_PER_DAY, now))
    if not rows:
        L.append("  no config changes")
    for v, actor, m, dims, aid, pid in rows:
        why = ""
        if pid:
            r = s.q1("SELECT request FROM proposals WHERE proposal_id=?", (pid,))
            req = _j(r[0]) if r else {}
            why = req.get("change_summary") or req.get("rationale") or ""
        L.append(f"  {fmt_minute(m)} v{v} by {actor} {'(' + aid + ')' if aid else ''} {', '.join(_j(dims) or [])}"
                 f"{' — ' + why[:110] if why else ''}")
    return "\n".join(L)


# ---------------------------------------------------------------------------------------------------- audit
def audit(s, a) -> str:
    if not a.workflow or not a.at:
        return "usage: audit --db DB --workflow WF --at 'D3 14:00'"
    wf, t = a.workflow, parse_label(a.at)
    rows = s.q("SELECT version, actor, minute, changed_dims, proposal_id, approval_id, config FROM config_versions "
               "WHERE wf=? AND minute IS NOT NULL ORDER BY version", (wf,))
    live = [r for r in rows if r[2] <= t]
    if not live:
        return f"{wf}: no config change at or before {fmt_minute(t)}; business default config was live"
    v, actor, m, dims, pid, aid, cfg = live[-1]
    L = [f"AUDIT {wf} @ {fmt_minute(t)}",
         f"live config: v{v}, applied {fmt_minute(m)} by {actor}; changed dims {', '.join(_j(dims) or []) or '-'}"]
    if actor == "supervisor":
        L.append("  → a client supervisor edited the config directly (outside governance). Our reaction:")
        for dm, kind, summ in s.q("SELECT minute, kind, summary FROM decisions WHERE wf=? AND minute BETWEEN ? AND ? "
                                  "AND kind IN ('supervisor_edit','watch','validation','incident_opened') ORDER BY id",
                                  (wf, m, m + 60)):
            L.append(f"     {fmt_minute(dm)} {kind}: {summ}")
    if pid:
        p = s.proposal(pid)
        req = p.get("request") or {}
        L += ["", "WHY (the proposal, as sent)",
              f"  proposal {pid} ({p['kind']}, via {p['channel']})",
              f"  summary   : {req.get('change_summary', '-')}",
              f"  rationale : {req.get('rationale', '-')}",
              f"  impact    : {req.get('impact_score', '-')}   affected leads: {req.get('affected_leads', '-')}",
              f"  dry run   : {json.dumps(req.get('dry_run'), default=str)[:600]}",
              f"  evidence  : {json.dumps(req.get('evidence'), default=str)[:600]}",
              "", "WHO"]
        if p["channel"] == "approval":
            ev = s.q1("SELECT minute, status, reasons FROM approval_events WHERE approval_id=? ORDER BY event_id "
                      "LIMIT 1", (aid,))
            L.append(f"  requested {fmt_minute(p['created_minute'])}; approval {aid} decided "
                     f"{fmt_minute(ev[0]) if ev else '?'} by the {(_wf_tenant(s, wf))} approver ({ev[1] if ev else '?'}); "
                     f"applied {fmt_minute(p['applied_minute'])} as v{p['applied_version']}")
        else:
            L.append(f"  auto-applied by the controller under the workflow's auto-apply policy at "
                     f"{fmt_minute(p['applied_minute'])} (no human approval needed for these dims)")
        L += ["", "DECISIONS THAT LED TO IT"]
        for dm, kind, summ in s.q("SELECT minute, kind, summary FROM decisions WHERE wf=? AND minute BETWEEN ? AND ? "
                                  "AND kind IN ('winner','arm_ramped','arm_dropped','decay_detected','did_flagged',"
                                  "'did_quarantined','did_plan','schedule_plan','incident_opened','incident',"
                                  "'validation') ORDER BY id", (wf, p["created_minute"] - 6 * 60, p["created_minute"])):
            L.append(f"  {fmt_minute(dm)} {kind}: {summ[:160]}")
    # what happened next: KPIs under this version vs the previous one, plus later decisions
    L += ["", "WHAT HAPPENED NEXT"]
    nxt = [r for r in rows if r[0] > v]
    end_slot = (nxt[0][2] // 60) if nxt else 10 ** 6

    def kpi(ver, lo=-10 ** 6, hi=10 ** 6):
        r = s.q1("SELECT SUM(dials), SUM(conn), SUM(eng), SUM(flags) FROM agg_prompt WHERE wf=? AND version=? AND "
                 "slot>=? AND slot<?", (wf, ver, lo, hi))
        n, c, e, f = (x or 0 for x in r)
        return (f"{n:>7,} dials  pickup {c / n:6.1%}  engagement {e / c:6.1%}  halluc flags {f / c:5.2%}"
                if n and c else "no calls")
    prev = [r for r in rows if r[0] < v]
    if prev:
        L.append(f"  before (v{prev[-1][0]}): {kpi(prev[-1][0])}")
    L.append(f"  under  (v{v}): {kpi(v)}" + (f"   until {fmt_minute(nxt[0][2])} (v{nxt[0][0]} by {nxt[0][1]})"
                                             if nxt else "   (still live)"))
    for dm, kind, summ in s.q("SELECT minute, kind, summary FROM decisions WHERE wf=? AND minute>? AND minute<=? AND "
                              "kind IN ('winner','decay_detected','arm_dropped','did_flagged','incident_opened',"
                              "'incident_resolved','supervisor_edit','applied','auto_applied','proposal_rejected') "
                              "ORDER BY id LIMIT 12", (wf, m, m + 2 * MIN_PER_DAY)):
        L.append(f"  {fmt_minute(dm)} {kind}: {summ[:150]}")
    _ = end_slot
    return "\n".join(L)


def _wf_tenant(s, wf):
    return _j(s.q1("SELECT spec FROM workflows WHERE wf=?", (wf,))[0]).get("tenant_id")


def decision_log(s, a) -> str:
    sql, args = "SELECT minute, wf, kind, summary FROM decisions WHERE 1=1", []
    if a.workflow:
        sql += " AND wf=?"
        args.append(a.workflow)
    if a.kind:
        sql += " AND kind LIKE ?"
        args.append(f"%{a.kind}%")
    rows = s.q(sql + " ORDER BY id LIMIT ?", (*args, a.limit))
    return "\n".join(f"{fmt_minute(m)}  {wf or '-':<14} {k:<18} {x}" for m, wf, k, x in rows)
