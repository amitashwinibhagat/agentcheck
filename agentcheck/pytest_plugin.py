"""Run AgentCheck judgments as pytest tests.

DeepEval's core idea is that an eval is a test: it runs in CI, it fails the
build, and failures print the reason. This plugin gives you the same thing with
AgentCheck's rubrics as the assertion vocabulary.

    # conftest.py is not needed; the plugin registers itself via the entry point.

    # tests/test_agent.py
    import agentcheck

    def test_refund_trace_is_flagged(ac):
        ac.rubric("refund-policy")
        ac.expect(trace, verdict="deny")

    def test_batch(ac):
        ac.rubric("safety")
        ac.expect_all(traces, allow={"pass", "review"})

``--agentcheck-judge`` selects the judge (default: typesafe), and
``--agentcheck-gate`` sets the confidence below which a judgment is reported as
an abstention rather than an assertion.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

DEFAULT_JUDGE = os.environ.get("AGENTCHECK_JUDGE", "typesafe")


def pytest_addoption(parser):  # pragma: no cover - exercised by pytest itself
    group = parser.getgroup("agentcheck")
    group.addoption("--agentcheck-judge", action="store", default=DEFAULT_JUDGE,
                    help="judge used for assertions (default: %(default)s)")
    group.addoption("--agentcheck-gate", action="store", type=float, default=0.6,
                    help="confidence below which a judgment is an abstention")


def pytest_configure(config):  # pragma: no cover
    config.addinivalue_line(
        "markers", "agentcheck: a test that calls a judging model")


class _Runner:
    """Assertion surface handed to tests as the ``ac`` fixture."""

    def __init__(self, judge_name: str, gate: float) -> None:
        self.judge_name = judge_name
        self.gate = gate
        self._rubric = "safety"

    # -- setup ---------------------------------------------------------------
    def rubric(self, name: str) -> "_Runner":
        """Choose the rubric. Accepts a name or a path to a YAML file."""
        from agentcheck import checks as check_lib
        from pathlib import Path
        p = Path(name)
        if p.exists() and p.suffix in (".yaml", ".yml"):
            self._rubric = check_lib.load_yaml(p).name
        else:
            check_lib.get(name)          # raises with the available names
            self._rubric = name
        return self

    # -- judging -------------------------------------------------------------
    def judge(self, trace: dict) -> dict:
        """Return the raw verdict for a trace: value, confidence, checks."""
        from agentcheck import checks as check_lib
        from agentcheck.judges import get_judge
        cs = check_lib.get(self._rubric)
        j = get_judge(self.judge_name)
        judgment = j.ask(trace, list(cs.checks))
        answers = {a.question_id: a for a in judgment.answers}
        verdict = answers.get(cs.verdict)
        return {
            "verdict": str(verdict.value) if verdict else None,
            "confidence": float(verdict.confidence) if verdict else 0.0,
            "abstained": bool(verdict and float(verdict.confidence) < self.gate),
            "checks": {k: {"value": v.value, "confidence": v.confidence}
                       for k, v in answers.items()},
            "rubric": cs.name,
            "model": getattr(j, "_model", None),
        }

    # -- assertions ----------------------------------------------------------
    def expect(self, trace: dict, verdict: str | Iterable[str],
               min_confidence: float | None = None) -> dict:
        """Assert the verdict is (one of) the expected value(s)."""
        want = {verdict} if isinstance(verdict, str) else set(verdict)
        got = self.judge(trace)
        if got["verdict"] not in want:
            raise AssertionError(
                f"expected verdict in {sorted(want)}, got {got['verdict']!r} "
                f"(confidence {got['confidence']:.2f}, rubric {got['rubric']})\n"
                f"  request: {trace.get('request')!r}\n"
                f"  tool:    {trace.get('tool')!r}\n"
                f"  checks:  {_fmt(got['checks'])}")
        floor = self.gate if min_confidence is None else min_confidence
        if got["confidence"] < floor:
            raise AssertionError(
                f"verdict {got['verdict']!r} matched but confidence "
                f"{got['confidence']:.2f} is below {floor}: the judgment is an "
                f"abstention, not evidence.\n  checks: {_fmt(got['checks'])}")
        return got

    def expect_all(self, traces: Iterable[dict], verdict: str | Iterable[str],
                   min_confidence: float | None = None) -> list[dict]:
        """Assert every trace in a batch; report each failure, not just the first."""
        failures: list[str] = []
        out: list[dict] = []
        for i, t in enumerate(traces):
            try:
                out.append(self.expect(t, verdict, min_confidence))
            except AssertionError as e:
                failures.append(f"[{i}] {e}")
        if failures:
            raise AssertionError(
                f"{len(failures)}/{len(out) + len(failures)} traces failed:\n"
                + "\n".join(failures))
        return out

    def expect_not(self, trace: dict, verdict: str = "pass") -> dict:
        """Assert the verdict is anything but the given value."""
        got = self.judge(trace)
        if got["verdict"] == verdict:
            raise AssertionError(
                f"expected a verdict other than {verdict!r}, got it at "
                f"confidence {got['confidence']:.2f}\n  checks: {_fmt(got['checks'])}")
        return got

    def assert_calibrated(self, dataset: str, max_ece: float = 0.10,
                          min_n: int = 30) -> dict:
        """Fail if measured ECE exceeds ``max_ece`` on a labeled dataset."""
        from agentcheck import calibration as cal
        from agentcheck import labels as labels_mod
        rows = labels_mod.load_dataset(dataset)
        rep = cal.calibration(self.judge_name, rows, checkset=self._rubric,
                              gate=self.gate)
        d = rep["decided"]
        if d["n"] < min_n:
            raise AssertionError(
                f"only {d['n']} decided items on {dataset!r}: cannot claim "
                f"calibration (need {min_n})")
        if d["ece"] > max_ece:
            raise AssertionError(
                f"ECE {d['ece']:.3f} exceeds {max_ece} on {dataset!r} "
                f"(accuracy {d['accuracy']}, coverage {d['coverage']}, "
                f"n {d['n']})")
        return rep


def _fmt(checks: dict) -> str:
    return ", ".join(
        f"{k}={v['value']}" if not isinstance(v["value"], float)
        else f"{k}={v['value']:.2f}"
        for k, v in checks.items())


try:  # pragma: no cover - only when pytest is present
    import pytest

    @pytest.fixture
    def ac(request) -> _Runner:
        """The AgentCheck assertion runner."""
        return _Runner(
            request.config.getoption("--agentcheck-judge", DEFAULT_JUDGE),
            request.config.getoption("--agentcheck-gate", 0.6),
        )

    @pytest.fixture
    def agentcheck_runner(request) -> _Runner:
        """Alias for ``ac``, for readability in larger suites."""
        return _Runner(
            request.config.getoption("--agentcheck-judge", DEFAULT_JUDGE),
            request.config.getoption("--agentcheck-gate", 0.6),
        )
except ModuleNotFoundError:  # pytest is optional for the library itself
    pass
