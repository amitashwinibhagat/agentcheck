"""PLG mechanics: allowances, 402 collision, funnel, trials, export data."""

import json
import sqlite3
import tempfile
from pathlib import Path

from agentcheck.store import Store  # noqa: E402


def _tmp():
    return Path(tempfile.mkdtemp()) / "t.db"


def test_migration_adds_allowance_plan_trial():
    tmp = _tmp()
    c = sqlite3.connect(tmp)
    c.executescript("""
CREATE TABLE results (id TEXT PRIMARY KEY,user_key TEXT NOT NULL,ts REAL NOT NULL,request TEXT,
tool TEXT,args_json TEXT,extra_json TEXT,verdict TEXT,confidence REAL,severity REAL,
checks_json TEXT NOT NULL,usage_json TEXT,model TEXT,request_id TEXT,cached INTEGER NOT NULL DEFAULT 0,assessment TEXT);
CREATE TABLE api_keys (key TEXT PRIMARY KEY,name TEXT NOT NULL,created REAL NOT NULL,qpm_limit INTEGER NOT NULL DEFAULT 600);
""")
    c.commit(); c.close()
    s = Store(tmp)
    k = s.create_key("u")
    meta = s.key_meta(k)
    assert meta["monthly_allowance"] == 500, meta
    assert meta["plan"] == "free", meta
    assert meta["trial_active"] is False


def test_key_plans_and_trials():
    s = Store(_tmp())
    pro = s.create_key("pro-user", plan="pro", monthly_allowance=10000)
    assert s.key_meta(pro)["plan"] == "pro"
    trial = s.create_key("trial-user", plan="trial", trial_days=14)
    meta = s.key_meta(trial)
    assert meta["trial_active"] is True, meta
    # trial with no future end date is not active
    past = s.create_key("past", plan="trial")
    assert s.key_meta(past)["trial_active"] is False


def test_monthly_usage_excludes_cache():
    s = Store(_tmp())
    k = s.create_key("u", monthly_allowance=10)
    k = s.lookup_key(k)["kid"]
    for _ in range(3):
        s.record(user_key=k, judge="stub", model="stub", request_id=None,
                 input_tokens=1, output_tokens=1, questions=5,
                 server_ms=1.0, cached=0, ok=1)
    s.record(user_key=k, judge="stub", model="stub", request_id=None,
             input_tokens=0, output_tokens=0, questions=5,
             server_ms=None, cached=1, ok=1)
    assert s.used_this_month(k) == 15, s.used_this_month(k)


def test_fail_count_this_month():
    s = Store(_tmp())
    k = s.create_key("u")
    k = s.lookup_key(k)["kid"]
    for i, verdict in enumerate(("fail", "fail", "pass")):
        s.save_result(k, {"request": f"r-{i}-{verdict}", "tool": "t", "args": {}},
                      {"trace_verdict": verdict, "confidence": 0.9, "severity": 1,
                       "checks": {}, "usage": {}, "model": "stub", "cached": False})
    assert s.fail_count_this_month(k) == 2


def test_funnel_counts():
    s = Store(_tmp())
    a = s.create_key("a")          # signed up only
    b = s.create_key("b")          # activated
    c = s.create_key("c")          # engaged
    a, b, c = (s.lookup_key(k)["kid"] for k in (a, b, c))
    s.record(user_key=b, judge="stub", model="stub", request_id=None,
             input_tokens=1, output_tokens=1, questions=5,
             server_ms=1.0, cached=0, ok=1)
    s.record(user_key=c, judge="stub", model="stub", request_id=None,
             input_tokens=1, output_tokens=1, questions=5,
             server_ms=1.0, cached=0, ok=1)
    rid = s.save_result(c, {"request": "r", "tool": "t", "args": {}},
                        {"trace_verdict": "pass", "confidence": 0.9, "severity": 0,
                         "checks": {}, "usage": {}, "model": "stub", "cached": False})["id"]
    assert s.assess(c, rid, "looks_correct") is True
    f = s.funnel()
    assert f["stages"]["signed_up"] == 3, f
    assert f["stages"]["activated"] == 2, f
    assert f["stages"]["engaged"] == 1, f
    assert f["stages"]["returning_7d"] == 2, f


def test_results_by_run_and_counts():
    s = Store(_tmp())
    k = s.create_key("u")
    k = s.lookup_key(k)["kid"]
    s.save_result(k, {"request": "r1", "tool": "t", "args": {}},
                  {"trace_verdict": "fail", "confidence": 0.9, "severity": 2,
                   "checks": {}, "usage": {}, "model": "stub", "cached": False},
                  run_id="run_abc")
    s.save_result(k, {"request": "r2", "tool": "t", "args": {}},
                  {"trace_verdict": "pass", "confidence": 0.9, "severity": 0,
                   "checks": {}, "usage": {}, "model": "stub", "cached": False},
                  run_id="run_abc")
    rows = s.results_by_run(k, "run_abc")
    assert len(rows) == 2, rows
    assert s.results_by_run(k, "run_nope") == []
    counts = s.result_counts(k)
    assert counts["batches"] == 1, counts
    assert counts["assessed"] == 0, counts


def test_events_recorded():
    s = Store(_tmp())
    k = s.create_key("u")
    k = s.lookup_key(k)["kid"]
    s.record_event(k, "quota_collision", {"used": 500, "allowance": 500})
    conn = sqlite3.connect(s.path)
    row = conn.execute("SELECT event, props_json FROM events WHERE user_key = ?",
                       (k,)).fetchall()
    conn.close()
    kinds = [r[0] for r in row]
    assert "key_created" in kinds, kinds
    assert "quota_collision" in kinds, kinds


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("\nall plg tests passed")


def test_key_meta_resolves_post_auth_kid():
    """Regression: _authorize returns the kid, so every allowance lookup in
    the request path addresses the key by kid. When key_meta only understood
    raw tokens it returned None and the quota fell back to the 500 default —
    a funded key then reported its allowance as exhausted."""
    s = Store(_tmp())
    raw = s.create_key("api", qpm_limit=1234, monthly_allowance=9999)
    kid = s.lookup_key(raw)["kid"]
    assert s.key_meta(raw) == s.key_meta(kid)
    meta = s.key_meta(kid)
    assert meta["qpm_limit"] == 1234, meta
    assert meta["monthly_allowance"] == 9999, meta
    assert s.key_meta("ac_not_a_key") is None
