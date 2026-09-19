"""RAG metrics, red-team corpus, eval matrix, gates, datasets, monitoring."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcheck import metrics as M  # noqa: E402
from agentcheck import monitor as mon  # noqa: E402
from agentcheck import redteam as rt  # noqa: E402
from agentcheck.evals import config as cfgmod  # noqa: E402
from agentcheck.evals import datasets as ds  # noqa: E402
from agentcheck.evals import runner  # noqa: E402
from agentcheck.judges.base import Answer, Judgment  # noqa: E402


class FixedJudge:
    """Returns preset probabilities per question id, so maths is testable."""

    name = "fixed"

    def __init__(self, by_id: dict[str, float], model: str = "fixed-model") -> None:
        self._by_id = by_id
        self._model = model

    def ask(self, state, questions):
        out = []
        for q in questions:
            if q.type == "noul":
                out.append(Answer(question_id=q.id, type="noul",
                                  value=self._by_id.get(q.id, 0.5),
                                  confidence=None, probabilities={}))
            elif q.type == "choice":
                opts = list(q.criteria)
                out.append(Answer(question_id=q.id, type="choice", value=opts[0],
                                  confidence=0.9, probabilities={opts[0]: 0.9}))
            else:
                out.append(Answer(question_id=q.id, type="score", value=1.0,
                                  confidence=0.9, probabilities={1: 0.9}))
        return Judgment(answers=out, request_id="fixed", input_tokens=0,
                        output_tokens=0, server_ms=1.0, model=self._model)


# ── RAG metrics ─────────────────────────────────────────────────────────────

def test_rag_trace_detection():
    assert M.is_rag_trace({"question": "q", "answer": "a", "contexts": ["c"]})
    assert not M.is_rag_trace({"request": "r", "tool": "t"})
    assert not M.is_rag_trace({"question": "q", "answer": "a"})       # no contexts


def test_faithfulness_is_supported_minus_contradicted():
    j = FixedJudge({"answer_is_grounded": 0.9, "answer_contradicts_context": 0.1})
    out = M.score_trace(j, {"question": "q", "answer": "a", "contexts": ["c"]})
    assert abs(out["faithfulness"].value - 0.8) < 1e-9, out["faithfulness"]
    assert "decisiveness" in out["faithfulness"].detail


def test_faithfulness_floor_at_zero():
    j = FixedJudge({"answer_is_grounded": 0.2, "answer_contradicts_context": 0.8})
    out = M.score_trace(j, {"question": "q", "answer": "a", "contexts": ["c"]})
    assert out["faithfulness"].value == 0.0, out["faithfulness"]


def test_ground_truth_metrics_only_when_reference_present():
    j = FixedJudge({"matches_ground_truth": 0.9, "invents_unstated_detail": 0.1})
    without = M.score_trace(j, {"question": "q", "answer": "a", "contexts": ["c"]})
    assert "answer_correctness" not in without
    assert "hallucination" not in without
    with_ref = M.score_trace(j, {"question": "q", "answer": "a", "contexts": ["c"],
                                 "ground_truth": "g"})
    assert abs(with_ref["answer_correctness"].value - 0.8) < 1e-9
    assert abs(with_ref["hallucination"].value - 0.1) < 1e-9


def test_context_utilization_is_a_proportion():
    # build_questions asks ctx_i for each context; FixedJudge defaults 0.5
    class CtxJudge(FixedJudge):
        def ask(self, state, questions):
            out = []
            for q in questions:
                if q.id.startswith("ctx_"):
                    n = int(q.id.split("_")[1])
                    out.append(Answer(question_id=q.id, type="noul",
                                      value=0.9 if n == 0 else 0.1,
                                      confidence=None, probabilities={}))
                elif q.type == "noul":
                    out.append(Answer(question_id=q.id, type="noul", value=0.8,
                                      confidence=None, probabilities={}))
                elif q.type == "choice":
                    o = list(q.criteria)[0]
                    out.append(Answer(question_id=q.id, type="choice", value=o,
                                      confidence=0.9, probabilities={}))
                else:
                    out.append(Answer(question_id=q.id, type="score", value=1.0,
                                      confidence=0.9, probabilities={}))
            return Judgment(answers=out, request_id="x", input_tokens=0,
                            output_tokens=0, server_ms=1.0, model="fixed")

    out = M.score_trace(CtxJudge({}), {"question": "q", "answer": "a",
                                       "contexts": ["one", "two"]})
    cu = out["context_utilization"]
    assert cu.detail["n_contexts"] == 2, cu.detail
    assert cu.detail["used"] == 1, cu.detail
    assert abs(cu.value - 0.5) < 1e-9, cu


def test_confidence_is_none_not_fake_zero():
    """Noul carries no confidence; the metric must say so, not invent 0.0."""
    j = FixedJudge({"answer_is_grounded": 0.9, "answer_contradicts_context": 0.1})
    out = M.score_trace(j, {"question": "q", "answer": "a", "contexts": ["c"]})
    assert out["faithfulness"].confidence is None, out["faithfulness"]
    assert out["faithfulness"].decisiveness > 0.0


def test_aggregate_and_thresholds():
    rows = [{"faithfulness": M.MetricResult("faithfulness", 0.9, None),
             "hallucination": M.MetricResult("hallucination", 0.1, None)},
            {"faithfulness": M.MetricResult("faithfulness", 0.7, None),
             "hallucination": M.MetricResult("hallucination", 0.3, None)}]
    agg = M.aggregate(rows)
    assert agg["faithfulness"]["mean"] == 0.8, agg
    assert agg["faithfulness"]["mean_confidence"] is None, agg
    ok = M.thresholds_pass(agg, {"faithfulness": 0.75},
                           direction={"hallucination": "lower"})
    assert ok["ok"], ok
    bad = M.thresholds_pass(agg, {"faithfulness": 0.9},
                            direction={"hallucination": "lower"})
    assert not bad["ok"] and bad["failures"][0]["metric"] == "faithfulness", bad
    bad2 = M.thresholds_pass(agg, {"hallucination": 0.05},
                             direction={"hallucination": "lower"})
    assert not bad2["ok"], bad2


# ── red team ────────────────────────────────────────────────────────────────

def test_corpus_is_substantial_and_unique():
    attacks = rt.corpus()
    assert len(attacks) >= 30, len(attacks)
    ids = [a.id for a in attacks]
    assert len(ids) == len(set(ids)), "attack ids must be unique"
    fams = {a.family for a in attacks}
    assert fams == set(rt.family_names()), fams


def test_every_attack_shapes_a_valid_trace():
    for a in rt.corpus():
        t = a.trace()
        assert t["request"] and t["tool"]
        assert isinstance(t["args"], dict)
        assert a.note, a.id


def test_corpus_selection_and_unknown_family():
    only = rt.corpus(["obfuscation"])
    assert {a.family for a in only} == {"obfuscation"}
    try:
        rt.corpus(["nope"])
    except KeyError as e:
        assert "obfuscation" in str(e)
    else:
        raise AssertionError("unknown family should raise")


# ── eval config ─────────────────────────────────────────────────────────────

GOOD_CONFIG = """
name: t-gate
datasets: [d1]
rubrics: [safety]
judges: [stub]
gate:
  min_accuracy: 0.9
  max_ece: 0.1
"""


def test_config_parses_and_merges_defaults():
    c = cfgmod.parse_config(GOOD_CONFIG, "t")
    assert c.name == "t-gate" and c.datasets == ["d1"]
    th = c.thresholds()
    assert th["min_accuracy"] == 0.9 and th["max_ece"] == 0.1
    assert th["min_coverage"] == 0.0, "unset keys keep their default"


def test_config_accepts_singular_dataset_and_string_lists():
    c = cfgmod.parse_config("name: x\ndataset: solo\nrubrics: safety\n", "t")
    assert c.datasets == ["solo"] and c.rubrics == ["safety"]


def test_config_rejects_bad_input():
    for label, text, needle in [
        ("no name", "datasets: [d]\n", "name"),
        ("no dataset", "name: x\n", "datasets"),
        ("unknown gate", "name: x\ndatasets: [d]\ngate:\n  min_vibes: 1\n", "unknown gate"),
        ("bad gate type", "name: x\ndatasets: [d]\ngate: []\n", "mapping"),
        ("bad list", "name: x\ndatasets: 5\n", "string"),
    ]:
        try:
            cfgmod.parse_config(text, "t")
        except cfgmod.ConfigError as e:
            assert needle in str(e), f"{label}: {e}"
        else:
            raise AssertionError(f"{label} should raise")


# ── datasets ────────────────────────────────────────────────────────────────

def test_dataset_save_version_and_load():
    with tempfile.TemporaryDirectory() as tmp:
        import os
        os.environ["AGENTCHECK_HOME"] = tmp
        rows = [{"trace": {"request": f"r{i}", "tool": "t", "args": {}},
                 "label_verdict": "pass", "should_fail": False,
                 "labeler": "stub", "labeler_model": "stub"} for i in range(10)]
        p1 = ds.save("d", rows, note="one")
        assert p1.name == "v1.json"
        p2 = ds.save("d", rows)
        assert p2.name == "v2.json"
        assert ds.current_path("d").name == "v2.json"
        assert len(ds.versions("d")) == 2
        assert len(ds.load("d")) == 10
        assert len(ds.load("d", version=1)) == 10
        inv = ds.inventory()
        assert inv[0]["name"] == "d" and inv[0]["versions"] == 2, inv


def test_split_is_deterministic_and_partitions():
    rows = [{"trace": {"request": f"r{i}", "tool": "t", "args": {}}} for i in range(100)]
    dev = ds.apply_split(rows, "dev")
    test = ds.apply_split(rows, "test")
    assert len(dev) + len(test) == 100, (len(dev), len(test))
    assert ds.apply_split(rows, "dev") == dev, "split must not reshuffle"
    assert 0 < len(test) < 40, f"expected a minority test split, got {len(test)}"
    assert ds.apply_split(rows, "all") == rows


def test_split_rejects_unknown():
    try:
        ds.apply_split([], "holdout")
    except ValueError as e:
        assert "dev" in str(e)
    else:
        raise AssertionError("should raise")


# ── runner + gate ───────────────────────────────────────────────────────────

def _report(**over):
    cell = {"dataset": "d", "rubric": "safety", "judge": "stub", "n": 50,
            "precision": 0.9, "recall": 0.9, "f1": 0.9, "accuracy": 0.9,
            "coverage": 0.9, "ece": 0.05, "circular": False,
            "confusion": {"tp": 1, "fp": 0, "tn": 1, "fn": 0}}
    cell.update(over)
    return {"kind": "agentcheck.eval-report", "version": 1, "name": "t",
            "cells": [cell], "gate": {"min_accuracy": 0.0, "max_ece": 1.0,
                                      "min_coverage": 0.0, "min_f1": 0.0,
                                      "max_f1_drop": 1.0, "max_ece_increase": 1.0}}


def test_gate_passes_clean_report():
    v = runner.apply_gate(_report())
    assert v["ok"], v


def test_gate_fails_on_threshold():
    r = _report(accuracy=0.5, ece=0.4)
    r["gate"].update({"min_accuracy": 0.8, "max_ece": 0.1})
    v = runner.apply_gate(r)
    assert not v["ok"]
    metrics = {f["metric"] for f in v["failures"]}
    assert {"accuracy", "ece"} <= metrics, v


def test_gate_detects_regression_against_baseline():
    base = _report(f1=0.95, accuracy=0.95, ece=0.02)
    cand = _report(f1=0.70, accuracy=0.75, ece=0.20)
    cand["gate"].update({"max_f1_drop": 0.05, "max_ece_increase": 0.02})
    v = runner.apply_gate(cand, baseline=base)
    assert not v["ok"]
    metrics = {f["metric"] for f in v["failures"]}
    assert {"f1_regression", "ece_regression", "accuracy_regression"} <= metrics, v


def test_gate_warns_on_circular_and_thin():
    v = runner.apply_gate(_report(circular=True, n=5))
    assert v["ok"], "warnings must not fail the gate on their own"
    reasons = " ".join(w["reason"] for w in v["warnings"])
    assert "self-agreement" in reasons and "too thin" in reasons, v


def test_gate_fails_on_error_cell():
    r = _report()
    r["cells"] = [{"dataset": "d", "rubric": "x", "judge": "stub",
                   "error": "unknown check set"}]
    v = runner.apply_gate(r)
    assert not v["ok"] and "unknown check set" in v["failures"][0]["reason"], v


def test_gate_fails_on_empty_report():
    r = _report()
    r["cells"] = []
    v = runner.apply_gate(r)
    assert not v["ok"], v


def test_report_roundtrip_and_type_check():
    with tempfile.TemporaryDirectory() as tmp:
        p = runner.write_report(_report(), Path(tmp) / "r.json")
        assert runner.load_report(p)["name"] == "t"
        bad = Path(tmp) / "bad.json"
        bad.write_text(json.dumps({"not": "a report"}))
        try:
            runner.load_report(bad)
        except ValueError as e:
            assert "not an agentcheck" in str(e)
        else:
            raise AssertionError("should reject a foreign JSON file")


def test_matrix_survives_a_broken_cell():
    """A missing dataset must not abort the whole matrix."""
    cfg = cfgmod.parse_config(
        "name: t\ndatasets: [definitely-missing]\nrubrics: [safety]\njudges: [stub]\n",
        "t")
    rep = runner.run_matrix(cfg)
    assert len(rep["cells"]) == 1, rep
    assert "error" in rep["cells"][0], rep
    v = runner.apply_gate(rep)
    assert not v["ok"], v


# ── monitor ─────────────────────────────────────────────────────────────────

class FakeStore:
    def __init__(self, rows):
        self._rows = rows

    from contextlib import contextmanager as _cm

    @_cm
    def _conn(self):
        class C:
            def __init__(self, rows): self._rows = rows
            def execute(self, sql, params):
                cut = params[0]
                rows = [r for r in self._rows if r["ts"] >= cut]
                if len(params) > 1:
                    rows = [r for r in rows if r.get("user_key") == params[1]]
                return type("R", (), {"fetchall": lambda self2: rows})()
        yield C(self._rows)


def _rows(n=10, base_ts=1_700_000_000, **kw):
    out = []
    for i in range(n):
        r = {"ts": base_ts + i * 3600, "verdict": "pass", "confidence": 0.9,
             "model": "stub", "assessment": None, "user_key": "k"}
        r.update(kw)
        out.append(r)
    return out


def test_timeline_buckets_and_rates():
    rows = _rows(4, verdict="fail") + _rows(6, verdict="pass")
    for i, r in enumerate(rows):
        r["ts"] = 1_700_000_000 + i * 3600
    tl = mon.timeline(FakeStore(rows), bucket="day", since_days=3650)
    assert tl["total"] == 10, tl
    assert len(tl["buckets"]) >= 1
    b = tl["buckets"][0]
    assert 0.0 <= b["flagged_rate"] <= 1.0
    assert b["mean_confidence"] == 0.9, b


def test_timeline_rejects_unknown_bucket():
    try:
        mon.timeline(FakeStore([]), bucket="fortnight")
    except ValueError as e:
        assert "hour" in str(e)
    else:
        raise AssertionError("should raise")


def test_drift_needs_two_buckets_then_compares():
    # one hour apart, each 1s apart -> a single hour bucket
    one_bucket = []
    for i in range(6):
        one_bucket.append({"ts": 1_700_000_000 + i, "verdict": "pass",
                           "confidence": 0.9, "model": "stub",
                           "assessment": None, "user_key": "k"})
    d = mon.drift(FakeStore(one_bucket), bucket="hour", since_days=3650)
    assert d["ok"] is False and "two buckets" in d["reason"], d

    rows = []
    for i in range(6):
        rows.append({"ts": 1_700_000_000 + i * 3600, "verdict": "fail",
                     "confidence": 0.9, "model": "stub", "assessment": None,
                     "user_key": "k"})
    for i in range(6):
        rows.append({"ts": 1_700_000_000 + 86400 + i * 3600, "verdict": "pass",
                     "confidence": 0.9, "model": "stub", "assessment": None,
                     "user_key": "k"})
    d = mon.drift(FakeStore(rows), bucket="day", since_days=3650)
    assert d["ok"], d
    assert d["change"]["flagged_rate"] < 0, d
    assert any("15pp" in n for n in d["notes"]), d


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("\nall eval-platform tests passed")
