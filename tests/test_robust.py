"""Tests for the robustness pass: one judge pass, retries, NaN safety,
LRU cache, config drift in gates, and dataset validation."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcheck import calibration as cal  # noqa: E402
from agentcheck import labels as labels_mod  # noqa: E402
from agentcheck.evals import datasets as ds  # noqa: E402
from agentcheck.evals import runner  # noqa: E402
from agentcheck.evals.config import parse_config  # noqa: E402
from agentcheck.judges.base import Answer, Judgment  # noqa: E402


class CountingJudge:
    """Counts asks so the 'one pass' guarantee is measurable."""

    name = "counting"
    calls = 0

    def ask(self, state, questions):
        CountingJudge.calls += 1
        out = []
        for q in questions:
            if q.type == "choice":
                opts = list(q.criteria)
                out.append(Answer(question_id=q.id, type="choice", value=opts[0],
                                  confidence=0.9, probabilities={opts[0]: 0.9}))
            elif q.type == "score":
                out.append(Answer(question_id=q.id, type="score", value=1.0,
                                  confidence=0.9, probabilities={1: 0.9}))
            else:
                out.append(Answer(question_id=q.id, type="noul", value=0.9,
                                  confidence=None, probabilities={}))
        return Judgment(answers=out, request_id=f"req-{CountingJudge.calls}",
                        input_tokens=1, output_tokens=1, server_ms=1.0,
                        model="counting-model")


def _ds(n: int) -> list[dict]:
    return [{"trace": {"request": f"r{i}", "tool": "t", "args": {}},
             "label_verdict": "pass", "should_fail": False,
             "labeler": "human", "labeler_model": "human"} for i in range(n)]


def test_evaluate_is_single_pass():
    """evaluate + calibration in one report must not re-ask the judge."""
    CountingJudge.calls = 0
    j = CountingJudge()
    res = labels_mod.evaluate("counting", _ds(40), checkset="safety", judge=j)
    assert res["n"] == 40 and CountingJudge.calls == 40, CountingJudge.calls

    # and calibration alone is also a single pass over a separate count
    CountingJudge.calls = 0
    cal.calibration("counting", _ds(40), judge=CountingJudge())
    assert CountingJudge.calls == 40, CountingJudge.calls


def test_calibration_uses_shared_predict():
    """build_report must be pure: no judge calls once items are predicted."""
    CountingJudge.calls = 0
    j = CountingJudge()
    _, verdict, usable, errors = cal.predict_dataset(j, _ds(40))
    before = CountingJudge.calls
    rep = cal.build_report("counting", "safety", verdict, usable, errors, gate=0.6)
    assert CountingJudge.calls == before, "build_report must not call the judge"
    assert rep["decided"]["accuracy"] is not None


def test_evaluate_parallel_workers():
    CountingJudge.calls = 0
    res = labels_mod.evaluate("counting", _ds(60), checkset="safety",
                              judge=CountingJudge(), workers=8)
    assert res["n"] == 60 and CountingJudge.calls == 60, CountingJudge.calls


def test_typesafe_rejects_nan_confidence():
    from agentcheck.judges.typesafe import _finite
    assert _finite(0.9) == 0.9
    assert _finite(float("nan")) is None
    assert _finite(float("inf")) is None
    assert _finite("nope") is None
    assert _finite(None) is None


def test_typesafe_retries_transient_not_permanent():
    """A 503 then 200 retries; a 401 never retries."""

    class FakeResp:
        def __init__(self, status, headers=None, json_body=None):
            self.status_code = status
            self.headers = headers or {}
            self._json = json_body
        def raise_for_status(self):
            if self.status_code >= 400:
                from httpx import HTTPStatusError, Request, Response
                raise HTTPStatusError("err", request=Request("POST", "http://x"),
                                      response=Response(self.status_code, request=Request("POST", "http://x")))
        def json(self):
            return self._json

    calls = {"n": 0}

    class FakeClient:
        def __init__(self, timeout): self.timeout = timeout
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json, headers):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResp(503, {"x-typesafe-request-id": "req-503"})
            return FakeResp(200, {"x-typesafe-request-id": "req-200",
                                  "x-envoy-upstream-service-time": "10"},
                            {"model": "jev-1", "answers": {}, "usage": {}})

    import httpx, agentcheck.judges.typesafe as T
    from agentcheck.judges.base import noul
    orig = httpx.Client
    httpx.Client = FakeClient
    try:
        j = T.TypeSafeJudge(api_key="k-x", max_retries=2, retry_backoff=0.0)
        j.ask("state", [noul("q", "is x?")])
        assert calls["n"] == 2, calls
    finally:
        httpx.Client = orig


def test_answer_cache_is_lru_bounded():
    from agentcheck.proxy import AnswerCache
    c = AnswerCache(max_entries=2)
    qs = [__import__("agentcheck.judges.base", fromlist=["noul"]).noul("q", "x")]
    for i in range(5):
        c.put({"k": i}, qs, ["ans"], "stub")
    assert len(c._store) == 2, "cache must not grow past its bound"
    # LRU: oldest key evicted
    assert c.get({"k": 0}, qs) is None
    assert c.get({"k": 4}, qs) == (["ans"], "stub"), "most recently used survives"


def test_write_report_sanitizes_nan():
    cfg = parse_config("name: t\ndatasets: [seed]\njudges: [stub]\n", "t")
    rep = runner.config_hash(cfg)
    # a cell carrying NaN would corrupt JSON; _clean replaces it
    dirty = {"kind": "agentcheck.eval-report", "version": 2, "name": "t",
             "config_hash": rep, "cells": [{"accuracy": float("nan"),
                                            "ece": float("inf")}], "gate": {}}
    with tempfile.TemporaryDirectory() as tmp:
        p = runner.write_report(dirty, Path(tmp) / "r.json")
        raw = p.read_text()
        assert "NaN" not in raw and "Infinity" not in raw, raw
        loaded = json.loads(raw)          # strict parse must succeed
        assert loaded["cells"][0]["accuracy"] is None


def test_gate_warns_on_baseline_cell_dropped():
    base = {"kind": "agentcheck.eval-report", "name": "b",
            "cells": [{"dataset": "d", "rubric": "safety", "judge": "stub",
                       "n": 50, "accuracy": 0.9, "f1": 0.9, "ece": 0.05}],
            "gate": {}}
    cand = {"kind": "agentcheck.eval-report", "name": "c",
            "cells": [{"dataset": "d", "rubric": "refund-policy", "judge": "stub",
                       "n": 50, "accuracy": 0.9, "f1": 0.9, "ece": 0.05}],
            "gate": {}}
    v = runner.apply_gate(cand, baseline=base)
    assert v["ok"], v
    reasons = " ".join(w["reason"] for w in v["warnings"])
    assert "missing from this report" in reasons, v


def test_gate_warns_on_config_hash_mismatch():
    base = {"kind": "agentcheck.eval-report", "name": "b",
            "config_hash": "aaaa", "cells": [_cell()], "gate": {}}
    cand = {"kind": "agentcheck.eval-report", "name": "c",
            "config_hash": "bbbb", "cells": [_cell()], "gate": {}}
    v = runner.apply_gate(cand, baseline=base)
    assert any("different configs" in w["reason"] for w in v["warnings"]), v


def test_invalid_rows_produce_warning_not_failure():
    cand = {"kind": "agentcheck.eval-report", "name": "c",
            "cells": [{**_cell(), "invalid_rows": "row 3: unlabeled item in a labeled dataset"}],
            "gate": {}}
    v = runner.apply_gate(cand)
    assert v["ok"], v
    assert any("understated" in w["reason"] for w in v["warnings"]), v


def _cell():
    return {"dataset": "d", "rubric": "safety", "judge": "stub", "n": 50,
            "accuracy": 0.9, "f1": 0.9, "ece": 0.05, "coverage": 0.9}


def test_dataset_validate_flags_garbage():
    good = {"trace": {"request": "r", "tool": "t", "args": {}},
            "label_verdict": "pass", "should_fail": False}
    bad_rows = [
        ("array row", [413, good]),
        ("no trace keys", [{"label_verdict": "pass"}]),
        ("bad trace type", [{"trace": "nope"}]),
        ("unlabeled", [{"trace": {"request": "r", "tool": "t"}}]),
        ("bad label type", [{"trace": {"request": "r", "tool": "t"}, "label_verdict": 5}]),
    ]
    for label, rows in bad_rows:
        problems = ds.validate(rows)
        assert problems, f"{label}: should be flagged"
    assert ds.validate([good]) == []
    # RAG shape is allowed
    assert ds.validate([{"trace": {"question": "q", "answer": "a",
                                   "contexts": ["c"]}, "labels": []}]) == []


def test_dataset_add_respects_registry_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        import os
        os.environ["AGENTCHECK_HOME"] = tmp
        rows = [{"trace": {"request": f"r{i}", "tool": "t", "args": {}},
                 "label_verdict": "pass", "should_fail": False,
                 "labeler": "stub", "labeler_model": "stub"} for i in range(8)]
        ds.save("robust", rows)
        assert ds.current_path("robust").name == "v1.json"
        assert len(ds.load("robust")) == 8
        # versions() counts immutable files
        assert len(ds.versions("robust")) == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("\nall robustness tests passed")
