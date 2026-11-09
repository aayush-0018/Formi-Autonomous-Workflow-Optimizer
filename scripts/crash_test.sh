#!/usr/bin/env bash
# Crash-recovery test (C13). Starts a fresh simulator, runs the controller, SIGKILLs it N times at random moments,
# restarts it each time, lets it finish, then verifies the invariants with scripts/verify_crash.py.
#   scripts/crash_test.sh [scenario.yaml] [kills] [port]
set -euo pipefail
cd "$(dirname "$0")/.."
SCENARIO="${1:-scenarios/public_02_incidents.yaml}"
KILLS="${2:-5}"
PORT="${3:-8720}"
NAME="$(basename "$SCENARIO" .yaml)"
PY="${PYTHON:-.venv/bin/python}"
STATE=state/crash
LOG=reports/crash_recovery.log
mkdir -p logs reports "$STATE"
rm -f "$STATE/$NAME".db*
"$PY" -m sim serve --scenario "$SCENARIO" --run-dir "runs/crash_$NAME" --port "$PORT" --force > logs/sim_crash.log 2>&1 &
SIM_PID=$!
trap 'kill $SIM_PID 2>/dev/null || true' EXIT
{
  echo "# crash-recovery test — $(date -u +%FT%TZ) — scenario $NAME — $KILLS SIGKILLs"
  for i in $(seq 1 "$KILLS"); do
    STATE_DIR=$STATE ./run.sh "http://127.0.0.1:$PORT" 2>> logs/controller_crash.log &
    CPID=$!
    sleep "$(( (RANDOM % 9) + 4 ))"
    if kill -0 $CPID 2>/dev/null; then
      MIN=$(curl -s "http://127.0.0.1:$PORT/sim/state" | "$PY" -c 'import sys,json; s=json.load(sys.stdin); print(s["ts"])')
      kill -9 $CPID
      wait $CPID 2>/dev/null || true
      echo "kill #$i: SIGKILL controller pid $CPID while the world was at $MIN"
    fi
  done
  echo "final restart: running to completion"
  STATE_DIR=$STATE ./run.sh "http://127.0.0.1:$PORT" 2>> logs/controller_crash.log
  echo "controller finished; verifying invariants"
  "$PY" scripts/verify_crash.py "http://127.0.0.1:$PORT" "$STATE/$NAME.db" "runs/crash_$NAME"
} 2>&1 | tee "$LOG"
[ -d "runs/base_$NAME" ] && "$PY" -m sim grade --run "runs/crash_$NAME" --baseline "runs/base_$NAME" \
  --out "reports/${NAME}_crash_run.md" > /dev/null && sed -n '5,10p' "reports/${NAME}_crash_run.md" | tee -a "$LOG"
