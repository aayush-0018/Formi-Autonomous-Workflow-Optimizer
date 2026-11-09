#!/usr/bin/env bash
# Starts the controller and runs until the scenario finishes. No manual steps.
#   ./run.sh <SIM_URL>
#
# Runtime: Python 3.10+ with numpy + httpx (both already in requirements.txt). If ./.venv exists we use it;
# otherwise we create one and install requirements.txt (first run only). No Docker needed.
# State: SQLite file at $STATE_DIR/<scenario>.db (default ./state). Re-running against the same live
# simulator resumes from that file (crash recovery); a fresh simulator world starts a fresh DB.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  python3 -m venv .venv >&2
  .venv/bin/pip install -q -r requirements.txt >&2
  PY=.venv/bin/python
fi
exec "$PY" -m controller run --url "$SIM_URL" --state-dir "${STATE_DIR:-state}"
