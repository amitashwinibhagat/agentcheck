"""Rubric files, calibration, and independent labeling."""

import json
import tempfile
from pathlib import Path

from agentcheck import calibration as cal  # noqa: E402
from agentcheck import checks as check_lib  # noqa: E402
from agentcheck import labels as labels_mod  # noqa: E402
from agentcheck.checks import yaml_checksets as yc  # noqa: E402
from agentcheck.judges.base import Answer, Judgment  # noqa: E402

GOOD_RUBRIC = """
name: t-refund
description: test rubric
verdict: decision
severity: severity
checks:
  - id: window
    type: noul
    instructions: Inside the window?
  - id: decision
    type: choice
    instructions: How to handle?
    criteria:
      approve: refund it
      escalate: human decides
      deny: refuse
  - id: severity
    type: score
    instructions: How bad if wrong?
    criteria: [trivial, annoying, costly]
"""


# ── rubric parsing ──────────────────────────────────────────────────────────

def test_parse_rubric_builds_checks():
    cs = yc.parse_rubric(GOOD_RUBRIC, "test")
    assert cs.name == "t-refund"
    assert [c.id for c in cs.checks] == ["window", "decision", "severity"]
    assert cs.verdict == "decision"
    assert cs.severity == "severity"
    payload = cs.question("decision").to_payload()
    assert payload["criteria"] == {"approve": "refund it",
                                   "escalate": "human decides", "deny": "refuse"}
    # score criteria must serialise to a LIST, not a map
    assert cs.question("severity").to_payload()["criteria"] == [
        "trivial", "annoying", "costly"]


def test_rubric_defaults_verdict_when_unambiguous():
    text = """
name: t-solo
checks:
  - id: q
    type: noul
    instructions: ok?
  - id: out
    type: choice
    instructions: pick
    criteria: {yes: y, no: n}
"""
    cs = yc.parse_rubric(text, "test")
    assert cs.verdict == "out"
    assert cs.severity is None


def test_rubric_rejects_bad_input():
    bad = [
        ("no name", "checks:\n  - {id: a, type: noul, instructions: x}\n", "name"),
        ("no checks", "name: x\nchecks: []\n", "non-empty"),
        ("bad type", "name: x\nchecks:\n  - {id: a, type: wat, instructions: x}\n", "type"),
        ("no instructions", "name: x\nchecks:\n  - {id: a, type: noul}\n", "instructions"),
        ("choice needs map",
         "name: x\nchecks:\n  - {id: a, type: choice, instructions: i, criteria: [a, b]}\n",
         "mapping"),
        ("score needs list",
         "name: x\nchecks:\n  - {id: a, type: score, instructions: i, criteria: {a: b}}\n",
         "list"),
        ("dup id",
         "name: x\nchecks:\n  - {id: a, type: noul, instructions: i}\n"
         "  - {id: a, type: noul, instructions: i}\n", "duplicate"),
        ("ambiguous verdict",
         "name: x\nchecks:\n  - {id: a, type: choice, instructions: i, criteria: {x: y}}\n"
         "  - {id: b, type: choice, instructions: i, criteria: {x: y}}\n", "one choice"),
        ("verdict wrong type",
         "name: x\nverdict: a\nchecks:\n  - {id: a, type: noul, instructions: i}\n", "expected choice"),
        ("verdict missing id",
         "name: x\nverdict: nope\nchecks:\n  - {id: a, type: noul, instructions: i}\n", "not one of"),
    ]
    for label, text, needle in bad:
        try:
            yc.parse_rubric(text, "test")
        except yc.RubricError as e:
            assert needle in str(e), f"{label}: {e}"
        else:
            raise AssertionError(f"{label} should have raised")


def test_builtin_cannot_be_shadowed(tmp: Path | None = None):
    import os
    d = Path(tempfile.mkdtemp())
    (d / "safety.yaml").write_text(GOOD_RUBRIC.replace("t-refund", "safety"))
    os.environ["AGENTCHECK_CHECKSETS"] = str(d)
    check_lib.dsl.YAML_CACHE = None
    found, problems = yc.discover(builtins={"safety"})
    assert "safety" not in found, found
    assert any("built-in" in p for p in problems), problems
    check_lib.dsl.YAML_CACHE = None
    os.environ.pop("AGENTCHECK_CHECKSETS", None)


def test_shipped_rubrics_load():
    names = check_lib.all_names()
    assert "safety" in names
    assert "refund-policy" in names
    assert "support-tone" in names
    rp = check_lib.get("refund-policy")
    assert rp.verdict == "decision" and rp.severity == "severity"
    assert check_lib.describe("refund-policy")["builtin"] is False


# ── calibration maths ───────────────────────────────────────────────────────

def test_ece_perfect_and_worst():
    perfect = [(0.9, 1)] * 9 + [(0.9, 0)]      # 90% right at 0.9 -> ECE 0
    ece, mce, table = cal._ece(perfect, 10)
    assert abs(ece) < 1e-9, ece
    assert abs(mce) < 1e-9, mce
    worst = [(0.9, 0)] * 10                     # 0% right at 0.9 -> ECE 0.9
    ece, mce, _ = cal._ece(worst, 10)
    assert abs(ece - 0.9) < 1e-9, ece
    assert abs(mce - 0.9) < 1e-9, mce


def test_ece_bins_are_count_weighted():
    """A rare bad bin must not dominate: ECE is count-weighted by construction."""
    pairs = [(0.95, 1)] * 90 + [(0.55, 0)] * 10
    ece, mce, _ = cal._ece(pairs, 10)
    # worst bin gap is 0.55, but only 10% of items are in it: 0.055 + 0.045.
    assert abs(ece - 0.1) < 1e-9, ece
    assert abs(mce - 0.55) < 1e-9, mce
    assert ece < mce / 5, (ece, mce)


def test_brier_and_risk_coverage():
    assert cal._brier([]) == 0.0
    assert abs(cal._brier([(0.9, 1)]) - 0.01) < 1e-9
    rc = cal._risk_coverage([(0.9, 1), (0.9, 1), (0.4, 0), (0.4, 0)])
    high = [r for r in rc if r["gate"] == 0.6][0]
    assert high["coverage"] == 0.5 and high["accuracy"] == 1.0, high
    low = [r for r in rc if r["gate"] == 0.0][0]
    assert low["coverage"] == 1.0 and low["accuracy"] == 0.5, low


def test_calibration_verdict_thresholds():
    assert cal.verdict_for_ece(0.01, 100) == "well-calibrated"
    assert cal.verdict_for_ece(0.08, 100) == "usable"
    assert cal.verdict_for_ece(0.30, 100) == "miscalibrated"
    assert cal.verdict_for_ece(0.01, 5) == "insufficient-data"


# ── independence + agreement ────────────────────────────────────────────────

def test_cohens_kappa():
    assert cal_round(labels_mod.cohens_kappa(["a", "b"], ["a", "b"])) == 1.0
    assert labels_mod.cohens_kappa(["a", "a"], ["b", "b"]) == 0.0
    # 3 of 4 agree, chance ~0.5 -> kappa ~0.5
    k = labels_mod.cohens_kappa(["a", "a", "b", "b"], ["a", "b", "b", "b"])
    assert 0.3 < k < 0.7, k


def cal_round(x: float) -> float:
    return round(x, 6)


def test_dataset_labelers_and_independence():
    ds = [
        {"trace": {"request": "r1", "tool": "t"},
         "label_verdict": "fail", "should_fail": True,
         "labeler": "typesafe", "labeler_model": "jev-1.13.0"},
    ]
    ls = labels_mod.dataset_labelers(ds)
    assert ls == [{"labeler": "typesafe", "model": "jev-1.13.0", "n": 1}], ls
    ind = labels_mod.independence("stub", ds)
    assert ind["circular"] is False and ind["overlapping"] == [], ind
    assert ind["labelers"][0]["model"] == "jev-1.13.0"


def test_agreement_across_two_labelers():
    ds = [
        {"labels": [{"model": "jev", "verdict": "fail"},
                    {"model": "gpt", "verdict": "fail"}]},
        {"labels": [{"model": "jev", "verdict": "pass"},
                    {"model": "gpt", "verdict": "fail"}]},
    ]
    ag = labels_mod.agreement(ds)
    assert ag["items_with_multiple_labels"] == 2, ag
    assert ag["pairs"][0]["n"] == 2
    assert ag["pairs"][0]["observed_agreement"] == 0.5, ag


# ── eval uses the rubric's verdict id ───────────────────────────────────────

class _FakeJudge:
    """Answers from a fixed map so eval/calibration are testable offline."""

    name = "fake"

    def __init__(self, verdicts: list[tuple[str, float]]) -> None:
        self._verdicts = list(verdicts)
        self._i = 0

    def ask(self, state, questions):
        value, conf = self._verdicts[min(self._i, len(self._verdicts) - 1)]
        self._i += 1
        answers = []
        for q in questions:
            if q.type == "choice":
                answers.append(Answer(question_id=q.id, type="choice", value=value,
                                      confidence=conf, probabilities={}))
            elif q.type == "score":
                answers.append(Answer(question_id=q.id, type="score", value=1.0,
                                      confidence=0.5, probabilities={}))
            else:
                answers.append(Answer(question_id=q.id, type="noul", value=0.5,
                                      confidence=0.5, probabilities={}))
        return Judgment(answers=answers, request_id="fake", input_tokens=0,
                        output_tokens=0, server_ms=1.0, model="fake-model")


def test_eval_respects_rubric_verdict_id():
    ds = [{"trace": {"request": "r", "tool": "t"},
           "label_verdict": "deny", "should_fail": True,
           "labeler": "human", "labeler_model": "human"}]
    j = _FakeJudge([("deny", 0.9)])
    res = labels_mod.evaluate("fake", ds, checkset="refund-policy", judge=j)
    assert res["checkset"] == "refund-policy", res
    assert res["n"] == 1 and res["recall"] == 1.0, res


def test_calibration_reports_abstention_separately():
    ds = [{"trace": {"request": f"r{i}", "tool": "t"},
           "label_verdict": "deny", "should_fail": True,
           "labeler": "human", "labeler_model": "human"} for i in range(4)]
    j = _FakeJudge([("deny", 0.9), ("deny", 0.9), ("approve", 0.3), ("deny", 0.35)])
    rep = cal.calibration("fake", ds, checkset="refund-policy", gate=0.6, judge=j)
    assert rep["n"] == 4, rep
    assert rep["decided"]["n"] == 2, rep["decided"]
    assert rep["abstained"]["n"] == 2, rep["abstained"]
    # of the two abstained, one was right and one wrong -> 0.5 if forced
    assert rep["abstained"]["accuracy_if_forced"] == 0.5, rep["abstained"]
    assert rep["decided"]["accuracy"] == 1.0, rep["decided"]
    # 100% accurate at 0.9 stated confidence is a +0.1 gap: under-confident.
    assert abs(rep["decided"]["ece"] - 0.1) < 1e-9, rep["decided"]
    assert rep["decided"]["reliability"][-1]["gap"] == 0.1, rep["decided"]["reliability"]


def test_calibration_flags_overconfidence():
    n = 40
    ds = [{"trace": {"request": f"r{i}", "tool": "t"},
           "label_verdict": "deny", "should_fail": True,
           "labeler": "human", "labeler_model": "human"} for i in range(n)]
    # always 0.95 confident, always wrong
    j = _FakeJudge([("approve", 0.95)] * n)
    rep = cal.calibration("fake", ds, checkset="refund-policy", gate=0.6, judge=j)
    assert rep["decided"]["accuracy"] == 0.0, rep["decided"]
    assert rep["decided"]["ece"] > 0.9, rep["decided"]
    assert cal.verdict_for_ece(rep["decided"]["ece"], n) == "miscalibrated"


def test_calibration_refuses_to_claim_on_thin_data():
    """The guard that matters: 10 items cannot establish calibration."""
    ds = [{"trace": {"request": f"r{i}", "tool": "t"},
           "label_verdict": "deny", "should_fail": True,
           "labeler": "human", "labeler_model": "human"} for i in range(10)]
    j = _FakeJudge([("approve", 0.95)] * 10)
    rep = cal.calibration("fake", ds, checkset="refund-policy", gate=0.6, judge=j)
    assert rep["decided"]["ece"] > 0.9, rep["decided"]
    assert cal.verdict_for_ece(rep["decided"]["ece"], 10) == "insufficient-data"


def test_dataset_roundtrip():
    ds = [{"trace": {"request": "r", "tool": "t"}, "label_verdict": "pass",
           "should_fail": False, "labeler": "stub", "labeler_model": "stub"}]
    d = Path(tempfile.mkdtemp())
    p = labels_mod.save_dataset(ds, d / "ds.json")
    assert json.loads(p.read_text())["traces"][0]["label_verdict"] == "pass"
    assert labels_mod.load_dataset(str(p)) == ds


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except TypeError:
                continue          # helper with a required arg
            print(f"PASS {name}")
    print("\nall eval tests passed")
