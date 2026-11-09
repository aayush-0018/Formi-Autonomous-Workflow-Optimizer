"""Event ingestion (C1) and the lead/feature join (C2).

Contract with the simulator: chunk numbers are global and increasing, and a chunk's content never changes.
We therefore keep one cursor (`meta.cursor_chunk`) and process chunks strictly in order. Each chunk's aggregate
deltas, lead rows, config/approval events *and* the cursor bump are committed in a single SQLite transaction:
a crash before COMMIT replays the chunk from scratch, a crash after it skips it. Exactly-once by construction.

We aggregate on the way in rather than storing calls. At the load envelope (~6.5 lakh dials/day) per-call rows
would be the dominant cost, and every decision the controller takes is about populations anyway (constraint 5).
"""
from __future__ import annotations

import io
import json
import logging
import time
from collections import defaultdict

from .simclient import SimClient
from .store import UPSERTS, Store
from .timeutil import Clock

log = logging.getLogger("controller.ingest")


class FeatureCodes:
    """Dictionary-encodes a lead's feature combination into one small int. Persisted, so codes are stable."""

    def __init__(self, store: Store):
        self.store = store
        self.code_of: dict[str, int] = {}
        self.feats: dict[int, dict[str, str]] = {}
        for code, f in store.q("SELECT code, features FROM codes"):
            self.code_of[f] = code
            self.feats[code] = json.loads(f)

    def code(self, features: dict[str, str]) -> int:
        key = json.dumps(features, sort_keys=True)
        c = self.code_of.get(key)
        if c is None:
            c = len(self.code_of)
            self.code_of[key] = c
            self.feats[c] = dict(features)
            self.store.db.execute("INSERT INTO codes VALUES (?,?)", (c, key))
        return c

    def feature_names(self) -> list[str]:
        names: set[str] = set()
        for f in self.feats.values():
            names.update(f)
        return sorted(names)

    def values(self, feature: str) -> list[str]:
        return sorted({f[feature] for f in self.feats.values() if feature in f})


class Ingestor:
    def __init__(self, store: Store, client: SimClient, clock: Clock, specs: dict[str, dict]):
        self.store, self.client, self.clock, self.specs = store, client, clock, specs
        self.codes = FeatureCodes(store)
        self.camp_leads: dict[str, dict[str, int]] = {}
        self.camp_day: dict[str, int] = {}
        self.camp_wf: dict[str, str] = {}
        self.events_total = 0
        self._reload_active_leads()

    # ------------------------------------------------------------------ restart support
    def _keep_days(self, wf: str) -> int:
        return len(self.specs[wf]["cadence"]) + 1

    def _reload_active_leads(self):
        cur = self.store.get_meta("cursor_day", None)
        if cur is None:
            return
        for cid, wf, day in self.store.q("SELECT campaign_id, wf, day FROM campaigns"):
            if wf in self.specs and day >= cur - self._keep_days(wf):
                self.camp_day[cid] = day
                self.camp_wf[cid] = wf
                self.camp_leads[cid] = dict(self.store.q("SELECT lead_id, code FROM leads WHERE campaign_id=?",
                                                         (cid,)))

    # ------------------------------------------------------------------ main entry
    @property
    def cursor(self) -> int:
        return self.store.get_meta("cursor_chunk", -1)

    def pending(self) -> list[dict]:
        after = self.cursor
        metas = self.client.chunks("history", after) + self.client.chunks("events", after)
        return sorted(metas, key=lambda m: m["chunk"])

    def ingest_available(self) -> dict:
        t0, n_events, n_chunks, last_eid = time.time(), 0, 0, None
        for meta in self.pending():
            if meta["chunk"] <= self.cursor:  # already done (listing raced with a previous run)
                continue
            text = self.client.chunk_text(meta["chunk"])
            n_events += self._process_chunk(meta, text)
            del text
            n_chunks += 1
            last_eid = meta["last_event_id"]
        self.events_total += n_events
        return {"events": n_events, "chunks": n_chunks, "seconds": time.time() - t0, "last_event_id": last_eid}

    # ------------------------------------------------------------------ chunk processing
    def _process_chunk(self, meta: dict, text: bytes | str) -> int:
        sched = defaultdict(lambda: [0, 0, 0])
        did = defaultdict(lambda: [0, 0])
        exp = defaultdict(lambda: [0, 0, 0])
        prm = defaultdict(lambda: [0, 0, 0, 0])
        arm = defaultdict(lambda: [0, 0])
        lead_rows, done_rows, camp_rows, cfg_rows, apr_rows = [], [], [], [], []
        clock, codes, camp_leads = self.clock, self.codes, self.camp_leads
        n = 0
        max_day = None
        with self.store.tx() as db:
            # bytes + BytesIO: iterating a str via StringIO would hold a UCS-4 copy (4x the chunk size)
            for line in io.BytesIO(text if isinstance(text, bytes) else text.encode()):
                if len(line) < 2:
                    continue
                ev = json.loads(line)
                n += 1
                t = ev["type"]
                if t == "call":
                    wf = ev["workflow_id"]
                    ts = ev["ts"]
                    day = clock.day_of(ts)
                    hour = int(ts[11:13])
                    slot = day * 24 + hour
                    cid = ev["campaign_id"]
                    code = camp_leads.get(cid, {}).get(ev["lead_id"])
                    if code is None:
                        code = self._lookup_code(ev["lead_id"])
                    conn = 1 if ev["connected"] else 0
                    eng = 1 if ev["engaged"] else 0
                    v = ev["variant"]
                    s = sched[(wf, day, hour, code)]
                    s[0] += 1; s[1] += conn; s[2] += eng  # noqa: E702
                    d = did[(wf, slot, ev["config_version"], v["caller_id"])]
                    d[0] += 1; d[1] += conn  # noqa: E702
                    e = exp[(wf, day, ev["config_version"], ev["cohort_id"], v["welcome_message"], v["voice"], code)]
                    e[0] += 1; e[1] += conn; e[2] += eng  # noqa: E702
                    if conn and day >= 0:
                        am = arm[(wf, slot, ev["config_version"], ev["cohort_id"], v["welcome_message"], v["voice"])]
                        am[0] += 1; am[1] += eng  # noqa: E702
                    p = prm[(wf, slot, ev["config_version"], v["prompt_version"])]
                    p[0] += 1; p[1] += conn; p[2] += eng  # noqa: E702
                    if conn and ev["qa"] and ev["qa"].get("hallucination_flag"):
                        p[3] += 1
                    if eng:
                        done_rows.append((ev["lead_id"],))
                    max_day = day if max_day is None or day > max_day else max_day
                elif t == "lead":
                    cid = ev["campaign_id"]
                    code = codes.code(ev["features"])
                    camp_leads.setdefault(cid, {})[ev["lead_id"]] = code
                    day = self.camp_day.get(cid)
                    if day is None:
                        day = clock.day_of(ev["ts"])
                    lead_rows.append((ev["lead_id"], ev["workflow_id"], cid, day, code, ev.get("person_id")))
                elif t == "campaign_created":
                    cid = ev["campaign_id"]
                    day = clock.day_of(ev["ts"])
                    self.camp_day[cid] = day
                    self.camp_wf[cid] = ev["workflow_id"]
                    camp_leads.setdefault(cid, {})
                    camp_rows.append((cid, ev["workflow_id"], day, clock.minute_of(ev["ts"]),
                                      ev.get("config_version_at_creation"), ev.get("leads")))
                elif t == "config_changed":
                    cfg_rows.append((ev["workflow_id"], ev["version"], ev["actor"], clock.minute_of(ev["ts"]),
                                     json.dumps(ev.get("changed_dims") or []), ev.get("proposal_id"),
                                     ev.get("approval_id")))
                elif t == "approval_decided":
                    apr_rows.append((ev["event_id"], ev["workflow_id"], ev["approval_id"], ev["status"],
                                     json.dumps(ev.get("reasons") or []), clock.minute_of(ev["ts"])))
            # writes, in dependency order, all inside the one transaction
            db.executemany("INSERT OR IGNORE INTO campaigns VALUES (?,?,?,?,?,?)", camp_rows)
            db.executemany("INSERT OR IGNORE INTO leads(lead_id, wf, campaign_id, day, code, person_id) "
                           "VALUES (?,?,?,?,?,?)", lead_rows)
            db.executemany("UPDATE leads SET done=1 WHERE lead_id=?", done_rows)
            db.executemany(UPSERTS["agg_sched"], [(*k, *v) for k, v in sched.items()])
            db.executemany(UPSERTS["agg_did"], [(*k, *v) for k, v in did.items()])
            db.executemany(UPSERTS["agg_exp"], [(*k, *v) for k, v in exp.items()])
            db.executemany(UPSERTS["agg_prompt"], [(*k, *v) for k, v in prm.items()])
            db.executemany(UPSERTS["agg_arm"], [(*k, *v) for k, v in arm.items()])
            db.executemany("INSERT INTO config_versions(wf, version, actor, minute, changed_dims, proposal_id, "
                           "approval_id) VALUES (?,?,?,?,?,?,?) ON CONFLICT(wf, version) DO UPDATE SET "
                           "actor=excluded.actor, minute=excluded.minute, changed_dims=excluded.changed_dims, "
                           "proposal_id=excluded.proposal_id, approval_id=excluded.approval_id", cfg_rows)
            db.executemany("INSERT OR IGNORE INTO approval_events VALUES (?,?,?,?,?,?)", apr_rows)
            self.store.set_meta("cursor_chunk", meta["chunk"])
            self.store.set_meta("cursor_event_id", meta["last_event_id"])
            self.store.set_meta("cursor_minute", meta["until_minute"])
            if max_day is not None:
                self.store.set_meta("cursor_day", max(max_day, self.store.get_meta("cursor_day", max_day)))
        self._evict()
        return n

    def _lookup_code(self, lead_id: str) -> int:
        r = self.store.q1("SELECT code FROM leads WHERE lead_id=?", (lead_id,))
        return r[0] if r else -1

    def _evict(self):
        today = self.store.get_meta("cursor_day", None)
        if today is None:
            return
        for cid in list(self.camp_day):
            wf = self.camp_wf.get(cid)
            keep = self._keep_days(wf) if wf in self.specs else 5
            if self.camp_day[cid] < today - keep:
                self.camp_day.pop(cid, None)
                self.camp_wf.pop(cid, None)
                self.camp_leads.pop(cid, None)
