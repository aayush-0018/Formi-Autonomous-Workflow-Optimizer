#!/usr/bin/env bash
# Run one scenario end to end locally: fresh simulator + our controller (via run.sh) + grade vs baseline.
#   scripts/run_scenario.sh scenarios/public_01_dev.yaml [port]
set -euo pipefail
cd "$(dirname "$0")/.."
SCENARIO="${1:?usage: scripts/run_scenario.sh <scenario.yaml> [port]}"
PORT="${2:-8710}"
NAME="$(basename "$SCENARIO" .yaml)"
PY="${PYTHON:-.venv/bin/python}"
mkdir -p logs reports state
[ -d "runs/base_$NAME" ] || "$PY" -m sim baseline --scenario "$SCENARIO" --run-dir "runs/base_$NAME"
rm -f "state/$NAME.db" "state/$NAME.db-wal" "state/$NAME.db-shm"
"$PY" -m sim serve --scenario "$SCENARIO" --run-dir "runs/mine_$NAME" --port "$PORT" --force > "logs/sim_$NAME.log" 2>&1 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
STATE_DIR=state ./run.sh "http://127.0.0.1:$PORT" 2> "logs/controller_$NAME.log"
"$PY" -m sim grade --run "runs/mine_$NAME" --baseline "runs/base_$NAME" --out "reports/$NAME.md" > /dev/null
sed -n '1,12p' "reports/$NAME.md"
grep -A3 "^## Changes applied" "reports/$NAME.md" | tail -2
