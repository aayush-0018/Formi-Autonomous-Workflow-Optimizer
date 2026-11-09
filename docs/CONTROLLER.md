# Controller runbook

This is for the engineer who owns the controller: how to run it, read it, debug it and extend it. The design
rationale is in [../SUBMISSION.md](../SUBMISSION.md), and the simulator contract is in [SIMULATOR.md](SIMULATOR.md).

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # run.sh does this on first use
./run.sh http://127.0.0.1:8700                                        # contract: runs until the scenario ends

scripts/run_scenario.sh scenarios/public_02_incidents.yaml            # fresh sim + controller + grade → reports/
scripts/crash_test.sh scenarios/public_02_incidents.yaml 5            # SIGKILL x5, restart, verify invariants
scripts/load_test.sh 2 2                                              # public_03 at full rate (2+2 days); `7 5` = full
.venv/bin/python -m pytest -q tests                                   # 17 tests incl. A/A false-winner rate
CONTROLLER_ABLATE=did,schedule,exp scripts/run_scenario.sh …          # switch levers off to measure them
```

State lives in `state/<scenario>.db` (SQLite WAL). If you restart the controller against the **same live simulator**,
it resumes from that file. If you point it at a **new** simulator world, it archives the old file (`*.old`) and
starts fresh.

## Read it (safe while it runs)

```bash
DB=state/public_02_incidents.db
.venv/bin/python -m controller status --db $DB                                   # on-call: lag, decisions/h, credits, RSS, alerts
.venv/bin/python -m controller view   --db $DB --workflow ks_cancel [--at "D3 18:00"]   # the supervisor's one screen
.venv/bin/python -m controller audit  --db $DB --workflow ks_cancel --at "D3 14:00"    # why / who / what next
.venv/bin/python -m controller log    --db $DB --workflow nw_renewal --kind incident   # raw decision trail
sqlite3 $DB "select * from proposals where state in ('pending','approved')"             # anything in flight
```

Decision `kind`s worth knowing:

| Group | Kinds |
|---|---|
| Lifecycle | `bootstrap` · `controller_start` / `controller_finish` |
| Governance | `proposal_sent` / `_approved` / `_rejected` / `_expired` / `_stale` / `_superseded` · `applied` · `auto_applied` · `replan` · `waiting` |
| Experiments | `winner` · `arm_ramped` · `arm_dropped` · `arm_retired` · `decay_suspected` · `decay_detected` |
| Operations | `did_plan` · `did_quarantined` · `did_flagged` · `schedule_plan` · `guardrail_ok` / `guardrail_breach` |
| Incidents | `incident_opened` · `validation` · `incident` · `incident_resolved` |
| Other | `supervisor_edit` · `alert` (with PAGE/ALERT prefix) · `error` |

## Code map (read in this order)

| File | What it owns |
|---|---|
| `loop.py` | the main loop: ingest → per-workflow `Brain.tick` → advance; metrics; DB selection on restart |
| `ingest.py` | chunk-atomic, exactly-once ingestion; lead→feature join; all aggregate tables |
| `store.py` | schema, transactions, proposals and decisions helpers |
| `planner.py` | `Brain`: per-workflow orchestration, target-config composition, request content (impact/dry run/affected) |
| `governance.py` | the **only** code that changes config: budgets, approver-hour timing, state machine, crash reconcile |
| `experiments.py` | welcome × voice: staged exploration, stratified tests, promotion, decay, rollback |
| `callerid.py` | DID exploration, quarantine, emergency collapse detection (version-stratified) |
| `schedule.py` | per-cohort hour weights from a χ² interaction test + shrunk rates |
| `incidents.py` | hallucination baseline, detection, known-good prompts, verification |
| `cohorts.py` | the one shared cohort object |
| `configs.py` | config semantics (normalise, changed dims, TV shift), implemented from the contract |
| `stats.py` | deterministic statistics (stratified diff, exact binomial, χ², Bonferroni) |
| `views.py` | status / view / audit / log |
| `tenant_prefs.json` | client soft preferences stricter than policy (Kestrel: ≤2/day, not before 11:00) |

## Invariants: do not break these

1. **No HTTP call inside a DB transaction that also records its intent.** `governance.submit/auto_apply` commit
   the `submitting` row *before* the call. Breaking this breaks crash safety.
2. **All config changes go through `Governance`.** Never call `client.auto_apply/apply/request_approval` elsewhere.
3. **Never auto-apply while an approval is in flight** for that workflow, except caller-ID emergencies (that
   approval goes stale and gets re-planned).
4. **Analysis uses the variant recorded on the call event, stratified by config version.** Never our intent.
5. **Decisions are deterministic.** No randomness in any decision path. The LLM can only add candidates.

## Debugging recipes

- **"Nothing is being proposed."** Run `log --kind waiting`. You'll see the reason: outside request window, budget
  used, TTL risk. Also check `normal_cooldown_until` in `wf_state` after a rejection.
- **"An approval was approved but never applied."** Look for `proposal_stale` / `proposal_superseded` in the log:
  the config moved (a supervisor edit or an emergency) and we refused to apply on a stale base, by design.
- **"Wrong caller ID quarantined."** The `did_flagged` / `did_quarantined` detail shows dials, expected, z and
  method. Unquarantine by editing `wf_state.did.quarantine` (there's no API; add one if this happens in production).
- **"Ingest lag growing."** `status` shows events/s while ingesting. History chunks are 250k events, and the first
  tick after start-up is slow by design.
- **Simulator 5xx / down.** The client retries with backoff (8 tries). After that, the tick errors, the process
  exits non-zero, and `./run.sh` can simply be re-run: it resumes.

## Extending

- **New lever:**
  1. Add a module that turns `wf_state` + aggregates into a desired fragment.
  2. Merge it in `Brain.compose()`.
  3. Describe it in `Brain.build_request()` (summary, rationale, dry run).
  4. Decide whether its dims are auto-eligible in `Governance.auto_dims()`.
- **New aggregate:** add the table to `store.SCHEMA` and `UPSERTS`, and fill it in `Ingestor._process_chunk`.
  Existing DBs need a fresh run (no migrations yet).
- **New tenant preference:** add it to `tenant_prefs.json`, keyed by `tenant_id`.
