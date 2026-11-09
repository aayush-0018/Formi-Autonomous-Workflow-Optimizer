"""Controller tests: statistics, false-winner rate, ingestion idempotency, governance rules, config semantics.

Run: python -m pytest -q tests
"""
import json
import math
from collections import defaultdict

import numpy as np
import pytest

from controller import experiments
from controller.cohorts import cohort_of, partition
from controller.configs import changed_dims, max_shift, step_towards, tv
from controller.governance import Governance
from controller.ingest import Ingestor
from controller.stats import binom_sf, bonferroni_z, chi2_sf, stratified_diff
from controller.store import Store
from controller.timeutil import Clock, fmt_minute, parse_label


# ----------------------------------------------------------------------------------------------- stats
def test_binom_sf_matches_brute_force():
    for n, p, k in [(50, 0.02, 3), (300, 0.015, 12), (400, 0.05, 20), (10, 0.5, 0), (10, 0.5, 11)]:
        brute = sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))
        assert binom_sf(k, n, p) == pytest.approx(brute, rel=1e-6, abs=1e-12)


def test_chi2_sf_reasonable():
    assert chi2_sf(33, 33) == pytest.approx(0.47, abs=0.03)
    assert chi2_sf(201.3, 33) < 1e-20


def test_stratified_diff_avoids_simpsons_paradox():
    # Arm A is better within each stratum but over-represented in the low-rate stratum.
    strata = [(900, 270, 100, 20), (100, 80, 900, 630)]  # (n_a, x_a, n_b, x_b)
    pooled_a, pooled_b = (270 + 80) / 1000, (20 + 630) / 1000
    assert pooled_a < pooled_b                       # naive pooling says A is worse
    assert stratified_diff(strata).delta > 0         # stratified estimate says A is better


def test_bonferroni_threshold():
    assert bonferroni_z(0.05, 1) == pytest.approx(1.645, abs=0.01)
    assert bonferroni_z(0.01, 5) == pytest.approx(2.878, abs=0.01)


# ----------------------------------------------------------------------------------------------- A/A test
class FakeData(experiments.Data):
    def __init__(self, rows, feats):
        self.rows, self.feats, self.arm_rows, self.now_slot = rows, feats, [], 0


def _aa_run(rng, days=8, conn_per_day=1000, p=0.55):
    spec = {"options": {"welcome_message": ["W1", "W2", "W3"], "voice": ["V1", "V2", "V3", "V4"]},
            "objective": {"primary": "engagement_rate"}}
    cfg = {"allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1}}}}
    st = experiments.init_state(spec, cfg)
    feats = {0: {"lang": "hi", "age_band": "18-30"}, 1: {"lang": "en", "age_band": "60+"}}
    rows = []
    agg = defaultdict(lambda: [0, 0, 0])
    winners = 0
    for day in range(days):
        want = experiments.desired(st, [])
        wd, vd = want["welcome_message"][0], want["voice"][0]
        for _ in range(3):  # three looks a day
            w = rng.choice(list(wd), conn_per_day // 3, p=np.array(list(wd.values())) / sum(wd.values()))
            v = rng.choice(list(vd), conn_per_day // 3, p=np.array(list(vd.values())) / sum(vd.values()))
            code = rng.integers(0, 2, conn_per_day // 3)
            eng = rng.random(conn_per_day // 3) < p  # NO true effect anywhere
            for wi, vi, ci, ei in zip(w, v, code, eng):
                a = agg[(day, 1, "default", wi, vi, int(ci))]
                a[0] += 1; a[1] += 1; a[2] += int(ei)  # noqa: E702
            rows = [(k[0], k[1], k[2], k[3], k[4], k[5], *v_) for k, v_ in agg.items()]
            decs = experiments.analyse(st, spec, FakeData(rows, feats), day, None, look=True)
            winners += sum(1 for d in decs if d["kind"] == "winner")
    return winners


def test_false_winner_rate_under_null():
    """A/A: no variant has any effect. Across 8 days x 3 looks, how often do we declare *any* winner?"""
    rng = np.random.default_rng(7)
    runs = 40
    any_winner = sum(1 for _ in range(runs) if _aa_run(rng) > 0)
    rate = any_winner / runs
    print(f"false-winner rate (any winner in an 8-day A/A run): {rate:.1%}")
    assert rate <= 0.15


# ----------------------------------------------------------------------------------------------- configs
def _cfg(voice, did, cohorts=None):
    return {"cohorts": cohorts or [], "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": voice,
                                                                 "caller_id": did}},
            "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}


def test_changed_dims_and_shift():
    a = _cfg({"V1": 1}, {"D1": 0.5, "D2": 0.5})
    b = _cfg({"V1": 1}, {"D1": 0.4, "D2": 0.4, "D3": 0.2})
    assert changed_dims(a, b) == ["caller_id"]
    assert max_shift(a, b) == pytest.approx(0.2)
    c = _cfg({"V1": 2, "V2": 2}, {"D1": 1, "D2": 1})  # weights normalise
    assert changed_dims(a, c) == ["voice"]


def test_step_towards_respects_max_shift():
    cur, tgt = {"D1": 0.5, "D2": 0.5}, {"D1": 1 / 3, "D2": 1 / 3, "D3": 1 / 3}
    s = step_towards(cur, tgt, 0.2)
    assert tv(cur, s) <= 0.2 + 1e-9
    assert tv(s, tgt) < tv(cur, tgt)


# ----------------------------------------------------------------------------------------------- cohorts
def test_partition_is_disjoint_and_exhaustive():
    vals = {"age_band": ["18-30", "60+"], "lang": ["hi", "ta"]}
    cells = partition(["age_band", "lang"], vals)
    assert len(cells) == 4 and len({c.id for c in cells}) == 4
    assert cohort_of(cells, {"age_band": "60+", "lang": "ta"}) == "age_band=60+&lang=ta"
    assert cohort_of(cells, {"age_band": "31-45", "lang": "ta"}) == "default"


# ----------------------------------------------------------------------------------------------- governance
SPEC = {"tenant_id": "kestrel_finance", "cadence": [2, 2], "governance": {
    "approver_hours": [10, 19], "approver_latency_minutes": [20, 120], "proposal_ttl_hours": 12,
    "max_requests_per_24h": 4, "auto_apply": {"enabled": True, "allowed_dims": ["schedule", "caller_id", "voice"],
                                              "max_shift": 0.2}}}


def test_governance_timing_and_caps(tmp_path):
    s = Store(tmp_path / "g.db")
    g = Governance(s, client=None, specs={"wf": SPEC})
    assert g.auto_dims("wf") == {"schedule", "caller_id"}           # customer-facing voluntarily excluded
    assert g.caps("wf") == (2, 4)                                    # tenant pref (2/day) < policy-1; urgent=policy
    assert not g.send_window_ok("wf", parse_label("D1 10:30"), urgent=False)[0]   # Kestrel: not before 11:00
    assert g.send_window_ok("wf", parse_label("D1 10:30"), urgent=True)[0]        # incidents may go at 10:00
    assert g.send_window_ok("wf", parse_label("D1 16:00"), urgent=False)[0]
    # 18:00 + 120 min latency spills to next morning 10:00 = 16h > 12h TTL -> would expire: don't send
    assert not g.send_window_ok("wf", parse_label("D1 18:00"), urgent=False)[0]


# ----------------------------------------------------------------------------------------------- ingestion
class FakeClient:
    def __init__(self, chunks):
        self._chunks = chunks  # list of (meta, text)

    def chunks(self, kind, after=-1):
        return [m for m, _ in self._chunks if m["kind"] == kind and m["chunk"] > after]

    def chunk_text(self, n):
        return self._chunks[n][1]


def _chunks():
    ts = "2027-01-04T10:05:00+05:30"
    lead = {"type": "lead", "ts": ts, "workflow_id": "wf", "campaign_id": "wf:c1", "lead_id": "L1",
            "person_id": "P1", "features": {"lang": "hi", "city_tier": "1", "age_band": "60+", "value_band": "low"}}
    camp = {"type": "campaign_created", "ts": ts, "workflow_id": "wf", "campaign_id": "wf:c1", "leads": 1,
            "config_version_at_creation": 1}
    call = {"type": "call", "ts": ts, "workflow_id": "wf", "campaign_id": "wf:c1", "lead_id": "L1",
            "person_id": "P1", "config_version": 1, "cohort_id": "default",
            "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
            "connected": True, "engaged": True, "qa": {"hallucination_flag": True}}
    c0 = "\n".join(json.dumps(e) for e in (camp, lead)) + "\n"
    c1 = "\n".join(json.dumps(dict(call, event_id=i)) for i in range(5)) + "\n"
    return [({"chunk": 0, "kind": "events", "last_event_id": 2, "until_minute": 615}, c0),
            ({"chunk": 1, "kind": "events", "last_event_id": 7, "until_minute": 630}, c1)]


def test_ingest_exactly_once_across_crash(tmp_path):
    specs = {"wf": {"cadence": [2, 2]}}
    clock = Clock.from_state({"ts": "2027-01-04T00:00:00+05:30", "sim_minute": 0})
    s = Store(tmp_path / "i.db")
    ing = Ingestor(s, FakeClient(_chunks()), clock, specs)
    # crash in the middle of chunk 1: the transaction rolls back
    orig = s.set_meta
    def boom(k, v):
        if k == "cursor_chunk" and v == 1:
            raise RuntimeError("SIGKILL")
        return orig(k, v)
    s.set_meta = boom
    with pytest.raises(RuntimeError):
        ing.ingest_available()
    s.set_meta = orig
    assert s.get_meta("cursor_chunk") == 0
    assert s.q1("SELECT SUM(dials) FROM agg_sched")[0] is None       # nothing from chunk 1 leaked
    # restart: a brand-new ingestor on the same DB resumes and counts each call exactly once
    ing2 = Ingestor(s, FakeClient(_chunks()), clock, specs)
    ing2.ingest_available()
    ing2.ingest_available()                                          # idempotent re-run
    assert s.q1("SELECT SUM(dials), SUM(conn), SUM(eng) FROM agg_sched")[0:3] == (5, 5, 5)
    assert s.q1("SELECT SUM(flags) FROM agg_prompt")[0] == 5
    assert s.q1("SELECT done FROM leads WHERE lead_id='L1'")[0] == 1
    assert s.q1("SELECT code FROM agg_sched")[0] == 0                # lead features joined onto the call


def test_time_labels():
    assert fmt_minute(parse_label("D3 14:00")) == "D3 14:00"
    assert fmt_minute(-60) == "D-1 23:00"
