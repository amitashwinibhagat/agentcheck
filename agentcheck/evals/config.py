"""Declarative eval configs, a matrix runner, and release gates.

Promptfoo's shape, applied to judgment rather than generation. AgentCheck does
not produce answers — it grades them — so the matrix is:

    rubrics x judges x datasets

That is the honest analogue of promptfoo's prompt x provider x test matrix, and
it answers the question a team actually has: *which rubric and which judge should
be gating my agent, on my data?*

    # evals/refund.yaml
    name: refund-gate
    datasets: [refund-cases]
    rubrics: [refund-policy, safety]
    judges: [typesafe]
    gate:
      min_accuracy: 0.90
      max_ece: 0.10
      min_coverage: 0.80
      max_f1_drop: 0.05

    agentcheck run evals/refund.yaml --out report.json
    agentcheck gate report.json --baseline previous.json
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_GATE = {
    "min_accuracy": 0.0,
    "max_ece": 1.0,
    "min_coverage": 0.0,
    "min_f1": 0.0,
    "max_f1_drop": 1.0,
    "max_ece_increase": 1.0,
}


class ConfigError(ValueError):
    """An eval config that cannot be run."""


@dataclass
class EvalConfig:
    name: str
    datasets: list[str] = field(default_factory=list)
    rubrics: list[str] = field(default_factory=lambda: ["safety"])
    judges: list[str] = field(default_factory=lambda: ["typesafe"])
    gate: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_GATE))
    gate_values: dict[str, float] = field(default_factory=dict)
    description: str = ""
    source: str = ""

    def thresholds(self) -> dict[str, float]:
        merged = dict(DEFAULT_GATE)
        merged.update({k: float(v) for k, v in (self.gate or {}).items()})
        return merged


def _yaml():
    try:
        import yaml
    except ModuleNotFoundError as e:  # pragma: no cover
        raise ConfigError("eval configs need PyYAML: pip install pyyaml") from e
    return yaml


def parse_config(text: str, source: str = "<string>") -> EvalConfig:
    data = _yaml().safe_load(text)
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level")
    name = str(data.get("name") or "").strip()
    if not name:
        raise ConfigError(f"{source}: 'name' is required")

    def as_list(key: str, default: list[str]) -> list[str]:
        v = data.get(key)
        if v is None:
            return list(default)
        if isinstance(v, str):
            return [v]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ConfigError(f"{source}: {key!r} must be a string or list of strings")
        return list(v)

    datasets = as_list("datasets", [])
    if not datasets:
        one = data.get("dataset")
        if isinstance(one, str) and one.strip():
            datasets = [one]
    if not datasets:
        raise ConfigError(f"{source}: 'datasets' (or 'dataset') is required")

    # Check the raw value: `gate: []` is falsy, and `or {}` would silently
    # turn a malformed gate into no gate at all.
    gate = data.get("gate")
    if gate is None:
        gate = {}
    if not isinstance(gate, dict):
        raise ConfigError(
            f"{source}: 'gate' must be a mapping of threshold -> number, "
            f"got {type(gate).__name__}")
    bad = [k for k in gate if k not in DEFAULT_GATE]
    if bad:
        raise ConfigError(
            f"{source}: unknown gate keys {bad}; supported: {sorted(DEFAULT_GATE)}")

    return EvalConfig(
        name=name,
        description=str(data.get("description") or "").strip(),
        datasets=datasets,
        rubrics=as_list("rubrics", ["safety"]),
        judges=as_list("judges", ["typesafe"]),
        gate={k: float(v) for k, v in gate.items()},
        source=source,
    )


def load_config(path: str | Path) -> EvalConfig:
    p = Path(path)
    try:
        text = p.read_text()
    except OSError as e:
        raise ConfigError(f"cannot read {p}: {e}") from e
    return parse_config(text, str(p))
