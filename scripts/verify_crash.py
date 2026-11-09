"""Verifies crash-recovery invariants after scripts/crash_test.sh.

Uses only the HTTP API plus our own store, except check 4, which reads the simulator's own event files in the run
dir as an *independent* count. This is test tooling, never part of the controller.
"""
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import httpx

url, db_path, run_dir = sys.argv[1], sys.argv[2], sys.argv[3]
c = httpx.Client(base_url=url, timeout=60)
db = sqlite3.connect(db_path)
ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{' — ' + detail if detail else ''}")


st = c.get("/sim/state").json()
check("scenario finished", st["finished"], st["ts"])
starts = db.execute("SELECT COUNT(*) FROM decisions WHERE kind='controller_start'").fetchone()[0]
check("controller was restarted", starts > 1, f"{starts} starts in our audit trail")

# 1. no proposal sent twice / every server-side approval is known to us with the right state
server = []
for w in c.get("/workflows").json():
    server += c.get("/approvals", params={"workflow_id": w["workflow_id"]}).json()
pids = Counter(a["proposal_id"] for a in server)
check("no duplicate proposal ids on the server", max(pids.values(), default=1) == 1, f"{len(server)} approvals")
ours = {r[0]: (r[1], r[2]) for r in db.execute("SELECT proposal_id, approval_id, state FROM proposals")}
missing = [a["approval_id"] for a in server if a["proposal_id"] not in ours]
check("every server approval is in our store (none lost)", not missing, str(missing))
mism = [(a["approval_id"], a["status"], ours[a["proposal_id"]][1]) for a in server
        if a["proposal_id"] in ours and a["status"] in ("applied", "rejected", "expired", "lapsed")
        and ours[a["proposal_id"]][1] != a["status"] and not (a["status"] == "approved")]
check("terminal states agree with the server", not mism, str(mism[:5]))
stuck = [r for r in db.execute("SELECT proposal_id, state FROM proposals WHERE state IN "
                               "('submitting','applying')")]
check("no proposal left half-done", not stuck, str(stuck))

# 2. no double apply: each approval applied at most once, each of our auto-applies maps to one version
for w in c.get("/workflows").json():
    hist = c.get(f"/workflows/{w['workflow_id']}/config/history").json()
    by_pid = Counter(v["proposal_id"] for v in hist if v["proposal_id"])
    check(f"{w['workflow_id']}: no proposal applied twice", max(by_pid.values(), default=1) == 1,
          f"{len(hist)} versions, {sum(1 for v in hist if v['actor'].startswith('controller'))} by controller")

# 3. simulator-side governance hygiene
log = [json.loads(l) for l in (Path(run_dir) / "sim_log.jsonl").read_text().splitlines() if l.strip()]
kinds = Counter(r["kind"] for r in log)
check("no apply conflicts / lapses caused by restarts", kinds["apply_conflict"] == 0 and kinds["approval_lapsed"] == 0,
      f"conflicts={kinds['apply_conflict']} lapsed={kinds['approval_lapsed']}")

# 4. exactly-once ingestion: our call count == the simulator's call count
calls = 0
for f in sorted((Path(run_dir) / "events").glob("*.jsonl")) + sorted((Path(run_dir) / "history").glob("*.jsonl")):
    with open(f) as fh:
        calls += sum(1 for line in fh if '"type":"call"' in line)
ours_calls = db.execute("SELECT SUM(dials) FROM agg_sched").fetchone()[0]
check("every call event counted exactly once", calls == ours_calls, f"simulator {calls:,} vs ours {ours_calls:,}")
print("RESULT:", "ALL INVARIANTS HOLD" if ok else "FAILURES ABOVE")
sys.exit(0 if ok else 1)
