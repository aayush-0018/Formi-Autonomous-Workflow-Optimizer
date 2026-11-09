"""CLI.

  python -m controller run    --url http://127.0.0.1:8700 [--state-dir state]
  python -m controller status --db state/public_02_incidents.db
  python -m controller view   --db state/public_02_incidents.db [--workflow ks_cancel] [--at "D3 18:00"]
  python -m controller audit  --db state/public_02_incidents.db --workflow ks_cancel --at "D3 14:00"
  python -m controller log    --db state/public_02_incidents.db [--workflow ks_cancel] [--kind incident]
"""
from __future__ import annotations

import argparse
import logging
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--url", default="http://127.0.0.1:8700")
    r.add_argument("--state-dir", default="state")
    r.add_argument("--max-wall-s", type=float, default=None)
    for name in ("status", "view", "audit", "log"):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True)
        p.add_argument("--workflow")
        p.add_argument("--at")
        p.add_argument("--kind")
        p.add_argument("--limit", type=int, default=200)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        from .loop import run
        run(a.url, a.state_dir, a.max_wall_s)
        return
    from . import views
    from .store import Store
    s = Store(a.db)
    out = {"status": views.status, "view": views.view, "audit": views.audit, "log": views.decision_log}[a.cmd](s, a)
    print(out)


if __name__ == "__main__":
    main()
