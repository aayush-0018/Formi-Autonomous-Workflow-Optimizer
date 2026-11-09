"""SQLite store: the controller's only state.

Why SQLite: it is an embedded file, not a new server to operate (stakeholders/03). WAL mode gives atomic
multi-table transactions, which is what crash safety rests on:

  * ingestion commits a chunk's aggregate deltas *and* the cursor in one transaction, so every event is
    counted exactly once no matter where the process dies;
  * every outward action (proposal, auto-apply, apply) is written as an intent row *before* the HTTP call,
    and reconciled against the simulator after a restart.

In production the same schema maps 1:1 onto the Postgres SRE already runs.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS workflows(wf TEXT PRIMARY KEY, spec TEXT NOT NULL);
-- dictionary-encoded lead features: one small int per distinct feature combination
CREATE TABLE IF NOT EXISTS codes(code INTEGER PRIMARY KEY, features TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS campaigns(campaign_id TEXT PRIMARY KEY, wf TEXT, day INT, minute INT,
    version_at_creation INT, leads INT);
CREATE TABLE IF NOT EXISTS leads(lead_id TEXT PRIMARY KEY, wf TEXT, campaign_id TEXT, day INT, code INT,
    person_id TEXT, done INT DEFAULT 0) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS leads_wf_day ON leads(wf, day);
-- pickup by (day, hour, feature-code): call-time optimisation + cohort sizing
CREATE TABLE IF NOT EXISTS agg_sched(wf TEXT, day INT, hour INT, code INT, dials INT, conn INT, eng INT,
    PRIMARY KEY(wf, day, hour, code)) WITHOUT ROWID;
-- pickup by (absolute hour slot, caller id): caller-id health
CREATE TABLE IF NOT EXISTS agg_did(wf TEXT, slot INT, version INT, did TEXT, dials INT, conn INT,
    PRIMARY KEY(wf, slot, version, did)) WITHOUT ROWID;
-- experiment cells: what was actually dialled (variant from the call event, never from our intent)
CREATE TABLE IF NOT EXISTS agg_exp(wf TEXT, day INT, version INT, cohort TEXT, wm TEXT, voice TEXT, code INT,
    dials INT, conn INT, eng INT, PRIMARY KEY(wf, day, version, cohort, wm, voice, code)) WITHOUT ROWID;
-- per hour slot x cohort x arm (no feature code: small) for fast decay monitoring
CREATE TABLE IF NOT EXISTS agg_arm(wf TEXT, slot INT, version INT, cohort TEXT, wm TEXT, voice TEXT, conn INT, eng INT,
    PRIMARY KEY(wf, slot, version, cohort, wm, voice)) WITHOUT ROWID;
-- per hour slot x config version x prompt: hallucination flags, KPIs per version (audit "what happened next")
CREATE TABLE IF NOT EXISTS agg_prompt(wf TEXT, slot INT, version INT, prompt TEXT, dials INT, conn INT, eng INT,
    flags INT, PRIMARY KEY(wf, slot, version, prompt)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS config_versions(wf TEXT, version INT, actor TEXT, minute INT, changed_dims TEXT,
    proposal_id TEXT, approval_id TEXT, config TEXT, PRIMARY KEY(wf, version));
CREATE TABLE IF NOT EXISTS approval_events(event_id INT PRIMARY KEY, wf TEXT, approval_id TEXT, status TEXT,
    reasons TEXT, minute INT);
CREATE TABLE IF NOT EXISTS proposals(proposal_id TEXT PRIMARY KEY, wf TEXT, kind TEXT, channel TEXT,
    state TEXT, created_minute INT, base_version INT, config TEXT, request TEXT, approval_id TEXT,
    decided_minute INT, apply_by TEXT, applied_version INT, applied_minute INT, reasons TEXT, plan TEXT,
    updated_minute INT);
CREATE INDEX IF NOT EXISTS proposals_wf_state ON proposals(wf, state);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, minute INT, wf TEXT, kind TEXT,
    summary TEXT, detail TEXT, proposal_id TEXT);
CREATE INDEX IF NOT EXISTS decisions_wf_minute ON decisions(wf, minute);
CREATE TABLE IF NOT EXISTS wf_state(wf TEXT PRIMARY KEY, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY AUTOINCREMENT, wall REAL, minute INT, events INT,
    ingest_s REAL, decide_s REAL, advance_s REAL, lag_events INT, credits REAL, rss_mb REAL, decisions INT);
"""

UPSERTS = {
    "agg_sched": ("INSERT INTO agg_sched VALUES (?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                  "dials=dials+excluded.dials, conn=conn+excluded.conn, eng=eng+excluded.eng"),
    "agg_did": ("INSERT INTO agg_did VALUES (?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "dials=dials+excluded.dials, conn=conn+excluded.conn"),
    "agg_exp": ("INSERT INTO agg_exp VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "dials=dials+excluded.dials, conn=conn+excluded.conn, eng=eng+excluded.eng"),
    "agg_arm": ("INSERT INTO agg_arm VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "conn=conn+excluded.conn, eng=eng+excluded.eng"),
    "agg_prompt": ("INSERT INTO agg_prompt VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                   "dials=dials+excluded.dials, conn=conn+excluded.conn, eng=eng+excluded.eng, "
                   "flags=flags+excluded.flags"),
}


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")  # WAL+NORMAL: durable across process crash
        self.db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._depth = 0

    # ---- transactions
    @contextmanager
    def tx(self):
        """Re-entrant transaction. Only the outermost block commits."""
        with self._lock:
            if self._depth == 0:
                self.db.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self.db
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.db.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self.db.execute("COMMIT")

    def q(self, sql: str, args=()) -> list[tuple]:
        return self.db.execute(sql, args).fetchall()

    def q1(self, sql: str, args=()):
        r = self.db.execute(sql, args).fetchone()
        return r

    # ---- meta
    def get_meta(self, k: str, default=None):
        r = self.q1("SELECT v FROM meta WHERE k=?", (k,))
        return json.loads(r[0]) if r else default

    def set_meta(self, k: str, v):
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (k, json.dumps(v)))

    # ---- workflow learned state (one JSON blob per workflow; small, rewritten atomically)
    def get_state(self, wf: str) -> dict:
        r = self.q1("SELECT state FROM wf_state WHERE wf=?", (wf,))
        return json.loads(r[0]) if r else {}

    def put_state(self, wf: str, state: dict):
        self.db.execute("INSERT INTO wf_state VALUES (?,?) ON CONFLICT(wf) DO UPDATE SET state=excluded.state",
                        (wf, json.dumps(state, sort_keys=True)))

    # ---- audit
    def decide(self, minute: int, wf: str | None, kind: str, summary: str, detail: dict | None = None,
               proposal_id: str | None = None):
        self.db.execute("INSERT INTO decisions(minute, wf, kind, summary, detail, proposal_id) VALUES (?,?,?,?,?,?)",
                        (minute, wf, kind, summary, json.dumps(detail or {}, default=str), proposal_id))

    # ---- proposals
    def proposal(self, pid: str) -> dict | None:
        cur = self.db.execute("SELECT * FROM proposals WHERE proposal_id=?", (pid,))
        r = cur.fetchone()
        if not r:
            return None
        return _row_to_proposal(cur.description, r)

    def proposals(self, wf: str | None = None, states: tuple[str, ...] | None = None) -> list[dict]:
        sql, args = "SELECT * FROM proposals WHERE 1=1", []
        if wf:
            sql += " AND wf=?"
            args.append(wf)
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            args += list(states)
        cur = self.db.execute(sql + " ORDER BY created_minute, proposal_id", args)
        return [_row_to_proposal(cur.description, r) for r in cur.fetchall()]

    def update_proposal(self, pid: str, **fields):
        for k in ("config", "request", "reasons", "plan"):
            if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
                fields[k] = json.dumps(fields[k], default=str)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE proposals SET {sets} WHERE proposal_id=?", (*fields.values(), pid))


def _row_to_proposal(desc, r) -> dict:
    d = {c[0]: v for c, v in zip(desc, r)}
    for k in ("config", "request", "reasons", "plan"):
        if d.get(k):
            d[k] = json.loads(d[k])
    return d
