"""Builds reports/public_03_load.md from the controller's own metrics table (C14)."""
import sqlite3
import statistics
import sys
from pathlib import Path

db_path, gen_s, run_s, run_dir = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
db = sqlite3.connect(db_path)
rows = db.execute("SELECT wall, minute, events, ingest_s, decide_s, advance_s, lag_events, rss_mb, decisions "
                  "FROM metrics ORDER BY id").fetchall()
first = rows[0]
hist_events, hist_s = first[2], first[3]
live = rows[1:]
by_day = {}
for r in live:
    by_day.setdefault(r[1] // 1440, []).append(r)
dec = sorted(r[4] for r in live)
pct = lambda xs, q: xs[min(len(xs) - 1, int(q * len(xs)))]
size_mb = sum(p.stat().st_size for p in Path(db_path).parent.glob(Path(db_path).name + "*")) / 1e6
events_on_disk = sum(p.stat().st_size for p in Path(run_dir).rglob("*.jsonl")) / 1e9
nwf = db.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
L = ["# Load test — public_03_load (12 workflows, full per-day rate)", "",
     f"Machine: developer laptop (Apple Silicon, 8 cores), single controller process, SQLite WAL. "
     f"Simulator and controller on the same machine.", "",
     f"- simulator world + history generation: {gen_s} s (simulator cost, not ours)",
     f"- history bootstrap (C3): {hist_events:,} events ingested in {hist_s:.1f} s → "
     f"{hist_events / max(hist_s, 1e-9):,.0f} events/s", "",
     "| sim day | events | controller ingest s | controller decide s | simulator advance s | wall-clock s | "
     "ingest events/s | decisions |", "|---|---|---|---|---|---|---|---|"]
for d, rs in sorted(by_day.items()):
    ev = sum(r[2] for r in rs); ing = sum(r[3] for r in rs); de = sum(r[4] for r in rs); adv = sum(r[5] for r in rs)
    wall = rs[-1][0] - rs[0][0] if len(rs) > 1 else ing + de + adv
    L.append(f"| D{d} | {ev:,} | {ing:.1f} | {de:.1f} | {adv:.1f} | {wall:.0f} | {ev / max(ing, 1e-9):,.0f} | "
             f"{sum(r[8] for r in rs)} |")
L += ["",
      f"- decision step per tick (all {nwf} workflows): p50 {statistics.median(dec):.2f} s, p95 {pct(dec, .95):.2f} s, "
      f"max {max(dec):.2f} s",
      f"- ingest lag at decision time: max {max(r[6] for r in live)} events (we always drain every chunk before "
      "deciding); decision freshness is therefore bounded by the 15-minute tick",
      f"- peak controller RSS: {max(r[7] for r in rows):,.0f} MB",
      f"- controller SQLite store: {size_mb:,.0f} MB; simulator event files: {events_on_disk:.1f} GB",
      f"- total controller run: {run_s} s", ""]
print("\n".join(L))
