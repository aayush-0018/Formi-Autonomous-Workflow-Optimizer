"""Governance client (C9): the only code that changes a workflow's config.

Proposal state machine (our store, `proposals.state`):

    submitting ──POST ok──▶ pending ──decided──▶ approved ──▶ applying ──▶ applied
        │                     │                      │            │
        │ 4xx/crash           ├─▶ rejected           ├─▶ stale (base moved: supervisor edit / other apply)
        ▼                     ├─▶ expired            └─▶ lapsed (apply window missed)
     abandoned / invalid      └─▶ stale (base moved before decision; we will not apply it)

    auto-apply:  submitting ──PUT ok──▶ applied | refused (403) | stale (409)

Every transition is committed *before* the next HTTP call. After a crash, `reconcile()` asks the simulator what
actually happened (GET /approvals, GET config/history by proposal_id) instead of guessing, so nothing is sent
twice and no approval is forgotten. Expired / rejected / stale / lapsed proposals are never resubmitted: the
planner re-plans from fresh data (stakeholders/05: "an expired proposal is a rejection").
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .simclient import ApiError, SimClient
from .store import Store
from .timeutil import MIN_PER_DAY, fmt_minute

log = logging.getLogger("controller.gov")

IN_FLIGHT = ("submitting", "pending", "approved", "applying")
_PREFS_PATH = Path(__file__).with_name("tenant_prefs.json")


def load_prefs() -> dict:
    try:
        return json.loads(_PREFS_PATH.read_text())
    except FileNotFoundError:
        return {}


class Governance:
    def __init__(self, store: Store, client: SimClient, specs: dict[str, dict]):
        self.store, self.client, self.specs = store, client, specs
        self.prefs = load_prefs()
        self.run_id = store.get_meta("run_id")

    # ------------------------------------------------------------------ policy helpers
    def gov(self, wf: str) -> dict:
        return self.specs[wf]["governance"]

    def pref(self, wf: str) -> dict:
        return self.prefs.get(self.specs[wf]["tenant_id"], {})

    def auto_dims(self, wf: str) -> set[str]:
        """Dims we are willing to auto-apply: the policy's allowed dims ∩ operational dims.

        Policy may also allow welcome_message/voice (Kestrel). We voluntarily route customer-facing changes
        through a human approval (stakeholders/02 and /04); see SUBMISSION.md §6.
        """
        aa = self.gov(wf).get("auto_apply") or {}
        if not aa.get("enabled"):
            return set()
        return set(aa.get("allowed_dims") or []) & {"schedule", "caller_id"}

    def max_shift(self, wf: str) -> float:
        return float((self.gov(wf).get("auto_apply") or {}).get("max_shift") or 0.0)

    def caps(self, wf: str) -> tuple[int, int]:
        """(normal cap, urgent cap) on approval requests per rolling 24h."""
        policy = int(self.gov(wf).get("max_requests_per_24h", 1))
        reserve = 1 if policy >= 2 else 0  # one slot always kept for incidents / rollbacks
        normal = policy - reserve
        if self.pref(wf).get("max_requests_per_day"):
            normal = min(normal, int(self.pref(wf)["max_requests_per_day"]))
        return max(normal, 0), policy

    def requests_last_24h(self, wf: str, now: int) -> int:
        r = self.store.q1("SELECT COUNT(*) FROM proposals WHERE wf=? AND channel='approval' AND "
                          "created_minute > ? AND (approval_id IS NOT NULL OR state='submitting')",
                          (wf, now - MIN_PER_DAY))
        return r[0]

    def send_window_ok(self, wf: str, now: int, urgent: bool) -> tuple[bool, str]:
        """Can a request sent *now* be decided before it expires, inside approver hours?"""
        g = self.gov(wf)
        lo, hi = g["approver_hours"]
        earliest = lo * 60
        if not urgent and self.pref(wf).get("earliest_request_hour") is not None:
            earliest = max(earliest, int(self.pref(wf)["earliest_request_hour"]) * 60)
        mod = now % MIN_PER_DAY
        if mod < earliest or mod >= hi * 60:
            return False, f"outside request window [{earliest // 60}:00, {hi}:00)"
        max_lat = g["approver_latency_minutes"][1]
        worst = now + max_lat
        if worst % MIN_PER_DAY >= hi * 60 or worst // MIN_PER_DAY > now // MIN_PER_DAY:
            worst = (now // MIN_PER_DAY + 1) * MIN_PER_DAY + lo * 60
        if worst > now + g["proposal_ttl_hours"] * 60:
            return False, "worst-case decision would land after the TTL; wait for the next approver morning"
        return True, "ok"

    def in_flight(self, wf: str, kinds: tuple[str, ...] | None = None) -> list[dict]:
        ps = self.store.proposals(wf, IN_FLIGHT)
        return [p for p in ps if kinds is None or p["kind"] in kinds]

    def can_request(self, wf: str, now: int, urgent: bool) -> tuple[bool, str]:
        ok, why = self.send_window_ok(wf, now, urgent)
        if not ok:
            return False, why
        normal_cap, urgent_cap = self.caps(wf)
        used = self.requests_last_24h(wf, now)
        cap = urgent_cap if urgent else normal_cap
        if used >= cap:
            return False, f"request budget used ({used}/{cap} in 24h)"
        return True, "ok"

    # ------------------------------------------------------------------ ids
    def _new_pid(self, wf: str, kind: str) -> str:
        seq = self.store.get_meta("proposal_seq", 0) + 1
        self.store.set_meta("proposal_seq", seq)
        return f"{self.run_id}-{wf}-{kind}-{seq:04d}"

    # ------------------------------------------------------------------ submit (approval path)
    def submit(self, wf: str, kind: str, now: int, base_version: int, config: dict, fields: dict,
               plan: dict) -> dict | None:
        with self.store.tx() as db:
            pid = self._new_pid(wf, kind)
            body = {"proposal_id": pid, "workflow_id": wf, "base_version": base_version,
                    "proposed_config": config, **fields}
            db.execute("INSERT INTO proposals(proposal_id, wf, kind, channel, state, created_minute, base_version, "
                       "config, request, plan, updated_minute) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, wf, kind, "approval", "submitting", now, base_version, json.dumps(config),
                        json.dumps(fields, default=str), json.dumps(plan, default=str), now))
        try:
            r = self.client.request_approval(body)
        except ApiError as e:
            state = "stale" if e.code == "stale_base_version" else "invalid"
            if e.code == "duplicate_proposal_id":  # we crashed after a successful POST in a previous life
                return self._reconcile_one(self.store.proposal(pid), now, base_version)
            with self.store.tx():
                self.store.update_proposal(pid, state=state, reasons=[e.code, e.detail], updated_minute=now)
                self.store.decide(now, wf, "proposal_failed", f"{kind} proposal {pid} not accepted: {e.code}",
                                  {"detail": e.detail}, pid)
            return None
        with self.store.tx():
            self.store.update_proposal(pid, state="pending", approval_id=r["approval_id"], updated_minute=now)
            self.store.decide(now, wf, "proposal_sent",
                              f"{kind}: requested approval {r['approval_id']} — {fields['change_summary']}",
                              {"approval_id": r["approval_id"], "expires_at": r.get("expires_at"),
                               "impact_score": fields.get("impact_score"),
                               "affected_leads": fields.get("affected_leads")}, pid)
        return self.store.proposal(pid)

    # ------------------------------------------------------------------ auto-apply path
    def auto_apply(self, wf: str, kind: str, now: int, base_version: int, config: dict, rationale: str,
                   evidence: dict, plan: dict) -> int | None:
        with self.store.tx() as db:
            pid = self._new_pid(wf, kind)
            fields = {"rationale": rationale, "evidence": evidence}
            db.execute("INSERT INTO proposals(proposal_id, wf, kind, channel, state, created_minute, base_version, "
                       "config, request, plan, updated_minute) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, wf, kind, "auto", "submitting", now, base_version, json.dumps(config),
                        json.dumps(fields, default=str), json.dumps(plan, default=str), now))
        body = {"proposal_id": pid, "base_version": base_version, "proposed_config": config,
                "rationale": rationale, "evidence": evidence}
        try:
            r = self.client.auto_apply(wf, body)
        except ApiError as e:
            state = {"stale_base_version": "stale", "auto_apply_not_permitted": "refused"}.get(e.code, "invalid")
            if e.code == "duplicate_proposal_id":
                p = self._reconcile_one(self.store.proposal(pid), now, base_version)
                return p and p.get("applied_version")
            with self.store.tx():
                self.store.update_proposal(pid, state=state, reasons=[e.code, e.detail], updated_minute=now)
                self.store.decide(now, wf, "auto_apply_failed", f"{kind} auto-apply {pid} refused: {e.code}",
                                  {"detail": e.detail}, pid)
            return None
        with self.store.tx():
            self.store.update_proposal(pid, state="applied", applied_version=r["version"], applied_minute=now,
                                       updated_minute=now)
            self._record_config(wf, r["version"], "controller_auto", now, r.get("changed_dims"), pid, None, config)
            self.store.decide(now, wf, "auto_applied", f"{kind}: auto-applied v{r['version']} ({rationale})",
                              {"changed_dims": r.get("changed_dims"), "evidence": evidence}, pid)
        return r["version"]

    # ------------------------------------------------------------------ lifecycle
    def poll(self, wf: str, now: int, current_version: int) -> list[dict]:
        """Advance every in-flight proposal of `wf`. Returns proposals that changed state this call."""
        changed = []
        for p in self.store.proposals(wf, IN_FLIGHT):
            before = p["state"]
            p2 = self._reconcile_one(p, now, current_version)
            if p2 and p2["state"] != before:
                changed.append(p2)
        return changed

    def _reconcile_one(self, p: dict, now: int, current_version: int) -> dict | None:
        pid, wf = p["proposal_id"], p["wf"]
        if p["state"] == "submitting":
            return self._reconcile_submitting(p, now)
        try:
            a = self.client.approval(p["approval_id"])
        except ApiError as e:
            log.warning("approval lookup %s failed: %s", p["approval_id"], e)
            return p
        st = a["status"]
        if st == "pending":
            return p
        if st in ("rejected", "expired", "lapsed"):
            with self.store.tx():
                self.store.update_proposal(pid, state=st, reasons=a.get("reasons"), updated_minute=now,
                                           decided_minute=now)
                self.store.decide(now, wf, f"proposal_{st}", f"{p['kind']} {p['approval_id']} {st}",
                                  {"reasons": a.get("reasons"), "decided_at": a.get("decided_at")}, pid)
            return self.store.proposal(pid)
        if st == "applied":
            v = self._applied_version_from_history(wf, pid)
            with self.store.tx():
                self.store.update_proposal(pid, state="applied", applied_version=v, applied_minute=now,
                                           updated_minute=now)
                self.store.decide(now, wf, "apply_reconciled", f"{p['approval_id']} found applied as v{v}",
                                  {}, pid)
            return self.store.proposal(pid)
        # approved: apply if (and only if) nothing moved underneath us
        if p["state"] == "pending":
            with self.store.tx():
                self.store.update_proposal(pid, state="approved", decided_minute=now, apply_by=a.get("apply_by"),
                                           updated_minute=now)
                self.store.decide(now, wf, "proposal_approved",
                                  f"{p['kind']} {p['approval_id']} approved by client approver at "
                                  f"{a.get('decided_at')}", {"apply_by": a.get("apply_by")}, pid)
        if p["base_version"] != current_version:
            with self.store.tx():
                self.store.update_proposal(pid, state="stale", updated_minute=now,
                                           reasons=[f"config moved v{p['base_version']}→v{current_version} "
                                                    "before apply; not applying, will re-plan"])
                self.store.decide(now, wf, "proposal_stale",
                                  f"{p['approval_id']} approved but base v{p['base_version']} is no longer current "
                                  f"(v{current_version}); not applied, re-planning", {}, pid)
            return self.store.proposal(pid)
        with self.store.tx():
            self.store.update_proposal(pid, state="applying", updated_minute=now)
        try:
            r = self.client.apply(wf, p["approval_id"])
        except ApiError as e:
            if e.code == "not_approved" and (e.detail or {}).get("status") == "applied":
                return self._reconcile_one({**p, "state": "applying"}, now, current_version)
            state = {"approval_lapsed": "lapsed", "stale_base_version": "stale"}.get(e.code, "invalid")
            with self.store.tx():
                self.store.update_proposal(pid, state=state, reasons=[e.code, e.detail], updated_minute=now)
                self.store.decide(now, wf, f"apply_{state}", f"apply of {p['approval_id']} failed: {e.code}",
                                  {"detail": e.detail}, pid)
            return self.store.proposal(pid)
        with self.store.tx():
            self.store.update_proposal(pid, state="applied", applied_version=r["version"], applied_minute=now,
                                       updated_minute=now)
            self._record_config(wf, r["version"], "controller_approved", now, r.get("changed_dims"), pid,
                                p["approval_id"], p["config"])
            self.store.decide(now, wf, "applied", f"{p['kind']}: applied {p['approval_id']} as v{r['version']}",
                              {"changed_dims": r.get("changed_dims")}, pid)
        return self.store.proposal(pid)

    def _reconcile_submitting(self, p: dict, now: int) -> dict | None:
        """We wrote the intent but may have died before/after the HTTP call. Ask the world."""
        pid, wf = p["proposal_id"], p["wf"]
        if p["channel"] == "approval":
            match = [a for a in self.client.approvals(workflow_id=wf) if a["proposal_id"] == pid]
            with self.store.tx():
                if match:
                    self.store.update_proposal(pid, state="pending", approval_id=match[0]["approval_id"],
                                               updated_minute=now)
                    self.store.decide(now, wf, "reconciled", f"{pid} found on server as {match[0]['approval_id']}",
                                      {}, pid)
                else:
                    self.store.update_proposal(pid, state="abandoned", updated_minute=now,
                                               reasons=["crashed before the request reached the server"])
                    self.store.decide(now, wf, "reconciled", f"{pid} never reached the server; abandoned, re-plan",
                                      {}, pid)
            p2 = self.store.proposal(pid)
            return p2
        v = self._applied_version_from_history(wf, pid)
        with self.store.tx():
            if v:
                self.store.update_proposal(pid, state="applied", applied_version=v, applied_minute=now,
                                           updated_minute=now)
                self._record_config(wf, v, "controller_auto", now, None, pid, None, p["config"])
                self.store.decide(now, wf, "reconciled", f"auto-apply {pid} found applied as v{v}", {}, pid)
            else:
                self.store.update_proposal(pid, state="abandoned", updated_minute=now,
                                           reasons=["crashed before the auto-apply reached the server"])
                self.store.decide(now, wf, "reconciled", f"auto-apply {pid} never applied; abandoned", {}, pid)
        return self.store.proposal(pid)

    def _applied_version_from_history(self, wf: str, pid: str) -> int | None:
        for v in self.client.config_history(wf):
            if v.get("proposal_id") == pid:
                return v["version"]
        return None

    def _record_config(self, wf, version, actor, minute, dims, pid, aid, config):
        self.store.db.execute(
            "INSERT INTO config_versions(wf, version, actor, minute, changed_dims, proposal_id, approval_id, config) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(wf, version) DO UPDATE SET config=excluded.config",
            (wf, version, actor, minute, json.dumps(dims or []), pid, aid, json.dumps(config)))

    def mark_superseded_pending(self, wf: str, now: int, current_version: int):
        """A pending request whose base moved (e.g. supervisor edit) can never apply. Log it once."""
        for p in self.store.proposals(wf, ("pending",)):
            if p["base_version"] != current_version and not (p.get("plan") or {}).get("_superseded_logged"):
                plan = dict(p.get("plan") or {})
                plan["_superseded_logged"] = True
                with self.store.tx():
                    self.store.update_proposal(p["proposal_id"], plan=plan)
                    self.store.decide(now, wf, "proposal_superseded",
                                      f"{p['approval_id']} was proposed on v{p['base_version']} but config is now "
                                      f"v{current_version}; it will not be applied even if approved",
                                      {}, p["proposal_id"])


def describe_window(wf_spec: dict) -> str:
    g = wf_spec["governance"]
    return (f"approver {g['approver_hours'][0]}:00–{g['approver_hours'][1]}:00, latency "
            f"{g['approver_latency_minutes'][0]}–{g['approver_latency_minutes'][1]} min, TTL {g['proposal_ttl_hours']}h, "
            f"apply window {g['apply_window_hours']}h, ≤{g['max_requests_per_24h']}/24h")


__all__ = ["Governance", "IN_FLIGHT", "describe_window", "fmt_minute"]
