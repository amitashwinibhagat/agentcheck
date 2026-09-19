"""Run an eval matrix and compare reports against a baseline.

The runner builds on ``labels.evaluate``, which already runs the judge exactly
once per trace and returns precision/recall, an independence verdict, and ECE.
This layer adds what a product needs on top:

  * parallelism — ``workers`` threads the judge calls inside each cell
  * report integrity — a report records which dataset versions produced it and a
    config hash, so a number can be traced and a stale baseline detected
  * config drift in gates — a cell that existed in the baseline but vanished from
    the candidate is a warning, because removing it silently skips its regression
  * NaN safety — a judge returning NaN/Inf can never be written into a report
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import time
from pathlib import Path
from typing import Any, Callable

from agentcheck.evals import datasets as ds
from agentcheck.evals.config import EvalConfig
from agentcheck import labels as labels_mod


def config_hash(cfg: EvalConfig) -> str:
    """Stable content hash of the run definition, so gates detect drift."""
    blob = json.dumps({
        "name": cfg.name,
        "datasets": cfg.datasets,
        "rubrics": cfg.rubrics,
        "judges": cfg.judges,
        "gate": cfg.gate,
        "split": None,
    }, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def dataset_versions(cfg: EvalConfig) -> dict[str, str | None]:
    """Which version of each registered dataset the report used."""
    out = {}
    for name in cfg.datasets:
        cur = ds.current_path(name)
        if cur is None:
            # seed or raw path, not in the registry — flag distinctly
            out[name] = "external"
        else:
            out[name] = cur.stem  # v1, v2 ...
    return out


def _clean(obj: Any) -> Any:
    """Recursively replace non-finite floats with None before serialising.

    json.dumps(NaN) emits invalid JSON (bare NaN), which a gate or any other
    JSON parser will then refuse. A judge cannot corrupt a report with that.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def run_matrix(cfg: EvalConfig, split: str | None = None,
               on_cell: Callable[[str], None] | None = None,
               workers: int = 1, cell_retries: int = 1) -> dict:
    """Evaluate every (dataset, rubric, judge) cell in the config.

    ``workers`` threads the judge calls inside each cell. ``cell_retries``
    re-runs a whole cell once if it fails at a level above a single trace.
    """
    cells: list[dict] = []
    for dataset, rubric, judge in itertools.product(
            cfg.datasets, cfg.rubrics, cfg.judges):
        label = f"{dataset} x {rubric} x {judge}"
        if on_cell:
            on_cell(label)
        status = {"dataset": dataset, "rubric": rubric, "judge": judge}
        try:
            rows = ds.load(dataset, split=split)
        except FileNotFoundError as e:
            cells.append({**status, "error": str(e)})
            continue
        if not rows:
            cells.append({**status,
                          "error": f"dataset {dataset!r} is empty for split {split!r}",
                          "n": 0})
            continue
        # Validate before trusting any number out of it.
        problems = ds.validate(rows)
        warning = f"{len(problems)} invalid row(s): first: {problems[0]}" if problems else None

        attempt = 0
        while True:
            attempt += 1
            try:
                res = labels_mod.evaluate(judge, rows, checkset=rubric, workers=workers)
            except Exception as e:
                if attempt <= cell_retries:
                    if on_cell:
                        on_cell(f"{label} (retry {attempt})")
                    continue
                cells.append({**status, "error": f"{type(e).__name__}: {e}"})
                break
            cells.append({
                **status,
                "judge": res["judge"],
                "model": res.get("model"),
                "checkset": res["checkset"],
                "n": res["n"],
                "errors": res.get("errors"),
                "invalid_rows": warning,
                "precision": res["precision"],
                "recall": res["recall"],
                "f1": res["f1"],
                "confusion": res["confusion"],
                "accuracy": res["calibration"]["accuracy_decided"],
                "coverage": res["calibration"]["coverage"],
                "ece": res["calibration"]["ece_decided"],
                "calibration_read": res["calibration"]["read"],
                "circular": res["independence"]["circular"],
                "labelers": res["independence"]["labelers"],
            })
            break

    return {
        "kind": "agentcheck.eval-report",
        "version": 2,
        "name": cfg.name,
        "description": cfg.description,
        "config_hash": config_hash(cfg),
        "dataset_versions": dataset_versions(cfg),
        "ran_at": time.time(),
        "split": split or "all",
        "config_source": cfg.source,
        "gate": cfg.thresholds(),
        "workers": workers,
        "cells": cells,
    }


def write_report(report: dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_clean(report), indent=2) + "\n")
    return p


def load_report(path: str | Path) -> dict:
    p = Path(path)
    data = json.loads(p.read_text())
    if not isinstance(data, dict) or data.get("kind") != "agentcheck.eval-report":
        raise ValueError(f"{p}: not an agentcheck eval report")
    return data


def apply_gate(report: dict, thresholds: dict[str, float] | None = None,
               baseline: dict | None = None) -> dict:
    """Threshold checks plus regression checks against a baseline.

    Failures fail the gate. Warnings do not — circular labeling, thin cells, and
    config drift between the baseline and this report are all warnings, because
    a gate that fails on a warning can never learn.
    """
    th = dict(report.get("gate") or {})
    if thresholds:
        th.update(thresholds)

    failures: list[dict] = []
    warnings: list[dict] = []
    base_index = {}
    if baseline:
        for c in baseline.get("cells", []):
            base_index[(c.get("dataset"), c.get("rubric"), c.get("judge"))] = c

    # Config drift: the baseline declared cells this report does not contain.
    if baseline:
        current = {(c.get("dataset"), c.get("rubric"), c.get("judge"))
                   for c in report.get("cells", [])}
        for key, prev in base_index.items():
            if key not in current:
                warnings.append({
                    "cell": f"{key[0]} x {key[1]} x {key[2]}",
                    "reason": "present in the baseline but missing from this "
                              "report: its regression check is silently dropped",
                })
        bh = baseline.get("config_hash"); ch = report.get("config_hash")
        if bh and ch and bh != ch:
            warnings.append({
                "cell": "-",
                "reason": "baseline and report come from different configs "
                          "(hashes differ); compare with that in mind",
            })

    for c in report.get("cells", []):
        where = f"{c.get('dataset')} x {c.get('rubric')} x {c.get('judge')}"
        if c.get("error"):
            failures.append({"cell": where, "reason": c["error"]})
            continue
        if c.get("invalid_rows"):
            warnings.append({"cell": where,
                             "reason": f"{c['invalid_rows']} — these numbers may be "
                                       "understated, not wrong"})
        if c.get("circular"):
            warnings.append({
                "cell": where,
                "reason": "judge is also the labeler: these numbers are "
                          "self-agreement, not accuracy",
            })
        if c.get("n", 0) < 30:
            warnings.append({"cell": where,
                             "reason": f"only {c.get('n')} items: too thin to gate on"})
        for key, limit, kind in (
            ("accuracy", th.get("min_accuracy"), "min"),
            ("coverage", th.get("min_coverage"), "min"),
            ("f1", th.get("min_f1"), "min"),
            ("ece", th.get("max_ece"), "max"),
        ):
            got = c.get(key)
            if got is None or limit is None:
                continue
            if kind == "min" and got < limit:
                failures.append({"cell": where, "metric": key, "got": got,
                                 "want": f">= {limit}"})
            if kind == "max" and got > limit:
                failures.append({"cell": where, "metric": key, "got": got,
                                 "want": f"<= {limit}"})

        prev = base_index.get((c.get("dataset"), c.get("rubric"), c.get("judge")))
        if prev and not prev.get("error"):
            drop = (prev.get("f1") or 0) - (c.get("f1") or 0)
            if drop > th.get("max_f1_drop", 1.0):
                failures.append({"cell": where, "metric": "f1_regression",
                                 "got": c.get("f1"), "baseline": prev.get("f1"),
                                 "want": f"drop <= {th.get('max_f1_drop')}"})
            rise = (c.get("ece") or 0) - (prev.get("ece") or 0)
            if rise > th.get("max_ece_increase", 1.0):
                failures.append({"cell": where, "metric": "ece_regression",
                                 "got": c.get("ece"), "baseline": prev.get("ece"),
                                 "want": f"rise <= {th.get('max_ece_increase')}"})
            acc_drop = (prev.get("accuracy") or 0) - (c.get("accuracy") or 0)
            if acc_drop > th.get("max_f1_drop", 1.0):
                failures.append({"cell": where, "metric": "accuracy_regression",
                                 "got": c.get("accuracy"),
                                 "baseline": prev.get("accuracy"),
                                 "want": f"drop <= {th.get('max_f1_drop')}"})

    if not report.get("cells"):
        failures.append({"cell": "-", "reason": "report has no cells"})
    return {"ok": not failures, "failures": failures, "warnings": warnings}
