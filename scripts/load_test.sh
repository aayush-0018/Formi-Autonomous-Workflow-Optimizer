#!/usr/bin/env bash
# Load test on public_03_load (12 workflows, ~650k dials/day, ~850k events/day).
#   scripts/load_test.sh [history_days] [days] [port]
# Defaults run a shortened copy (2 history days + 2 live days) at the FULL per-day rate. The per-day numbers are
# what we report, and the full 7+5 days writes ~4 GB of simulator event files. Use `7 5` for the full scenario.
set -euo pipefail
cd "$(dirname "$0")/.."
HD="${1:-2}"; DAYS="${2:-2}"; PORT="${3:-8730}"
PY="${PYTHON:-.venv/bin/python}"
mkdir -p runs logs reports state
SC=runs/load_${HD}_${DAYS}.yaml
"$PY" - "$HD" "$DAYS" "$SC" <<'EOF'
import sys, yaml
sc = yaml.safe_load(open("scenarios/public_03_load.yaml"))
sc["history_days"], sc["days"] = int(sys.argv[1]), int(sys.argv[2])
sc["name"] = f"public_03_load_h{sys.argv[1]}_d{sys.argv[2]}"
yaml.safe_dump(sc, open(sys.argv[3], "w"), sort_keys=False)
EOF
NAME="public_03_load_h${HD}_d${DAYS}"
rm -f state/"$NAME".db*
T0=$(date +%s)
"$PY" -m sim serve --scenario "$SC" --run-dir "runs/load_mine" --port "$PORT" --force > logs/sim_load.log 2>&1 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
until grep -q "ready in" logs/sim_load.log; do sleep 2; done
T1=$(date +%s)
STATE_DIR=state ./run.sh "http://127.0.0.1:$PORT" > /dev/null 2> logs/controller_load.log
T2=$(date +%s)
echo "simulator world+history generation: $((T1 - T0)) s; controller run (history ingest + $DAYS days): $((T2 - T1)) s"
"$PY" scripts/load_report.py "state/$NAME.db" "$((T1 - T0))" "$((T2 - T1))" "runs/load_mine" \
  | tee reports/public_03_load.md
