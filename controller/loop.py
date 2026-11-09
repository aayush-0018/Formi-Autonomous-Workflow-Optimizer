"""The always-on loop: ingest → decide (per workflow) → advance. Restart-safe at every line."""
from __future__ import annotations

import logging
import os
import resource
import secrets
import shutil
import sys
import time
from pathlib import Path

from .governance import Governance
from .ingest import Ingestor
from .planner import Brain
from .simclient import ApiError, SimClient
from .store import Store
from .timeutil import Clock, fmt_minute, hour_of

log = logging.getLogger("controller")

STEP_DAY = 15      # minutes per advance while anything can happen
STEP_NIGHT = 60    # 22:00–07:00: no dialling, no approvers; coarser steps


def rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024


def wait_for_sim(client: SimClient, timeout_s: float = 600):
    t0 = time.time()
    while True:
        try:
            client.http.get("/health", timeout=5).raise_for_status()
            return
        except Exception:  # noqa: BLE001 - simulator still generating history
            if time.time() - t0 > timeout_s:
                raise
            time.sleep(1)


def open_store(state_dir: str, scenario: str, sim_state: dict) -> Store:
    """One DB per scenario run. If the simulator was restarted (a new world), archive the old DB and start
    fresh: our learned state describes a world that no longer exists."""
    path = Path(state_dir) / f"{scenario}.db"
    if path.exists():
        s = Store(path)
        cur_chunk = s.get_meta("cursor_chunk", -1)
        cur_min = s.get_meta("cursor_minute", None)
        stale = sim_state["chunks"] <= cur_chunk or (cur_min is not None and sim_state["sim_minute"] < cur_min)
        if not stale:
            return s
        s.db.close()
        arch = path.with_suffix(f".{int(time.time())}.old")
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(path) + suffix)
            if p.exists():
                shutil.move(str(p), str(arch) + suffix)
        log.warning("simulator world is newer than %s (restarted sim?) — archived to %s", path, arch)
    s = Store(path)
    with s.tx():
        s.set_meta("run_id", secrets.token_hex(3))
        s.set_meta("scenario", scenario)
    return s


def run(url: str, state_dir: str = "state", max_wall_s: float | None = None):
    client = SimClient(url)
    wait_for_sim(client)
    st = client.state()
    store = open_store(state_dir, st["scenario"], st)
    clock = Clock.from_state(st)
    specs = {w["workflow_id"]: w for w in client.workflows()}
    with store.tx():
        for wf, spec in specs.items():
            store.db.execute("INSERT INTO workflows VALUES (?,?) ON CONFLICT(wf) DO UPDATE SET spec=excluded.spec",
                             (wf, __import__("json").dumps(spec)))
    restarts = store.get_meta("restarts", -1) + 1
    with store.tx():
        store.set_meta("restarts", restarts)
        store.decide(st["sim_minute"], None, "controller_start",
                     f"controller started (start #{restarts}) at {fmt_minute(st['sim_minute'])}, pid {os.getpid()}",
                     {"scenario": st["scenario"], "credits": st["credits_remaining"]})
    log.info("scenario %s at %s, %d workflows, restart #%d", st["scenario"], fmt_minute(st["sim_minute"]),
             len(specs), restarts)
    ingestor = Ingestor(store, client, clock, specs)
    gov = Governance(store, client, specs)
    brains = {wf: Brain(wf, spec, store, client, gov, ingestor.codes) for wf, spec in specs.items()}
    t_start = time.time()
    while True:
        st = client.state()
        now = st["sim_minute"]
        ing = ingestor.ingest_available()
        t1 = time.time()
        n_dec0 = store.q1("SELECT COUNT(*) FROM decisions")[0]
        for wf, b in brains.items():
            b.alerts = []
            try:
                b.tick(now)
            except ApiError as e:
                log.error("%s tick failed at %s: %s", wf, fmt_minute(now), e)
                store.decide(now, wf, "error", f"tick failed: {e}", {})
            for a in b.alerts:
                log.warning("%s %s", fmt_minute(now), a)
                store.decide(now, wf, "alert", a, {})
        decide_s = time.time() - t1
        n_dec = store.q1("SELECT COUNT(*) FROM decisions")[0] - n_dec0
        if st["finished"]:
            _metric(store, now, ing, decide_s, 0.0, st, n_dec)
            break
        if max_wall_s and time.time() - t_start > max_wall_s:
            log.warning("wall-clock budget exhausted; stopping")
            break
        step = STEP_NIGHT if (hour_of(now) >= 22 or hour_of(now) < 7) else STEP_DAY
        if hour_of(now) < 7 and hour_of(now + step) >= 7:
            step = max(15, 7 * 60 - now % 1440)  # land exactly on 07:00 for the daily plan
        t2 = time.time()
        try:
            client.advance(step)
        except ApiError as e:
            if e.code == "finished":
                continue
            raise
        _metric(store, now, ing, decide_s, time.time() - t2, st, n_dec)
        if now % 1440 == 0:
            log.info("%s ingested %d events (%.1fs), decide %.2fs, rss %.0f MB, credits %.0f", fmt_minute(now),
                     ing["events"], ing["seconds"], decide_s, rss_mb(), st["credits_remaining"])
    with store.tx():
        store.decide(st["sim_minute"], None, "controller_finish", f"scenario finished at {fmt_minute(st['sim_minute'])}",
                     {"credits_remaining": st["credits_remaining"]})
    log.info("finished %s; credits remaining %.0f", st["scenario"], st["credits_remaining"])


def _metric(store, now, ing, decide_s, adv_s, st, n_dec):
    lag = st["last_event_id"] - (store.get_meta("cursor_event_id", 0) or 0)
    store.db.execute("INSERT INTO metrics(wall, minute, events, ingest_s, decide_s, advance_s, lag_events, credits, "
                     "rss_mb, decisions) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (time.time(), now, ing["events"], ing["seconds"], decide_s, adv_s, lag, st["credits_remaining"],
                      rss_mb(), n_dec))
