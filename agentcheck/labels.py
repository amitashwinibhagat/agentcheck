"""Label generation, agreement, and evaluation.

The moat. A verification tool's pitch is an accuracy number; you only get one by
generating ground truth independently of the judge being graded, then measuring
that judge against it.

The circularity trap this module exists to prevent: label with the same model
you are about to grade, and you have measured agreement with yourself. A dataset
records *who* labeled it and, where several labelers ran, every label. `evaluate`
compares the graded judge's model against the labeler models and says so.

Label policy: an item is POSITIVE when the gold verdict is anything but "pass".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentcheck import checks as check_lib
from agentcheck.judges import get_judge
from agentcheck.judges.base import Answer

SEED_DIR = Path(__file__).resolve().parent / "seeds"


# ── labeling ────────────────────────────────────────────────────────────────

def label_trace(trace: dict, labeler_judge: str = "typesafe",
                checkset: str = "safety") -> dict:
    """Ask one labeler for the gold verdict on one trace."""
    judge = get_judge(labeler_judge)
    cs = check_lib.get(checkset)
    j = judge.ask(trace, list(cs.checks))
    by_id = {a.question_id: a for a in j.answers}
    verdict = by_id.get(cs.verdict)
    return {
        "trace": trace,
        "label_verdict": verdict.value if verdict else None,
        "label_confidence": verdict.confidence if verdict else None,
        "should_fail": bool(verdict and verdict.value != "pass"),
        "labeler": judge.name,
        "labeler_model": getattr(judge, "_model", None) or judge.name,
        "checkset": cs.name,
    }


def label_dataset(traces: list[dict], labeler_judge: str = "typesafe",
                  checkset: str = "safety", verbose: bool = True,
                  existing: list[dict] | None = None) -> list[dict]:
    """Label every trace.

    Pass ``existing`` (a previously labeled dataset) to *accumulate* a second
    labeler rather than overwrite: each record keeps a ``labels`` list of every
    independent label, which is what makes agreement measurable.
    """
    prior = {id(r): r for r in (existing or [])}
    out: list[dict] = []
    for i, t in enumerate(traces):
        rec = label_trace(t, labeler_judge, checkset)
        prev = next((r for r in (existing or [])
                     if r.get("trace") == t), None)
        if prev is not None:
            history = list(prev.get("labels") or [])
            if not history:
                history = [{"labeler": prev.get("labeler"),
                            "model": prev.get("labeler_model"),
                            "verdict": prev.get("label_verdict")}]
            history.append({"labeler": rec["labeler"], "model": rec["labeler_model"],
                            "verdict": rec["label_verdict"]})
            rec["labels"] = history
        out.append(rec)
        if verbose and (i + 1) % 10 == 0:
            print(f"  labeled {i + 1}/{len(traces)}")
    return out


def dataset_labelers(dataset: list[dict]) -> list[dict]:
    """Who labeled this dataset: [{labeler, model, n}]."""
    counts: dict[tuple, int] = {}
    for rec in dataset:
        entries = rec.get("labels") or [{
            "labeler": rec.get("labeler"),
            "model": rec.get("labeler_model"),
            "verdict": rec.get("label_verdict"),
        }]
        for e in entries:
            key = (e.get("labeler") or "unknown", e.get("model") or "unknown")
            counts[key] = counts.get(key, 0) + 1
    return [{"labeler": k[0], "model": k[1], "n": v}
            for k, v in sorted(counts.items(), key=lambda x: -x[1])]


def independence(judge_name: str, dataset: list[dict], judge: Any | None = None) -> dict:
    """Is the graded judge also the labeler? That would be circular."""
    judge = judge or get_judge(judge_name)
    graded_model = getattr(judge, "_model", None) or judge.name
    labelers = dataset_labelers(dataset)
    same = [l for l in labelers
            if l["model"] == graded_model or l["labeler"] == judge.name]
    return {
        "graded_judge": judge.name,
        "graded_model": graded_model,
        "labelers": labelers,
        "circular": bool(same) and len(labelers) == len(same),
        "overlapping": [l["model"] for l in same],
    }


# ── agreement between labelers ──────────────────────────────────────────────

def cohens_kappa(a: list[str], b: list[str]) -> float:
    """Agreement corrected for chance, for two labelers over shared items."""
    n = len(a)
    if n == 0:
        return 0.0
    labels = sorted(set(a) | set(b))
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    expected = sum((a.count(l) / n) * (b.count(l) / n) for l in labels)
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1 - expected)


def agreement(dataset: list[dict]) -> dict:
    """Pairwise agreement wherever an item carries two or more labels."""
    multi = [r for r in dataset if len(r.get("labels") or []) >= 2]
    pairs: dict[tuple, dict] = {}
    for rec in multi:
        entries = rec["labels"]
        for i in range(len(entries)):
            for k in range(i + 1, len(entries)):
                x, y = entries[i], entries[k]
                key = tuple(sorted([x.get("model") or x.get("labeler"),
                                    y.get("model") or y.get("labeler")]))
                slot = pairs.setdefault(key, {"a": [], "b": []})
                slot["a"].append(str(x.get("verdict")))
                slot["b"].append(str(y.get("verdict")))
    out = []
    for (m1, m2), slot in pairs.items():
        n = len(slot["a"])
        observed = sum(1 for x, y in zip(slot["a"], slot["b"]) if x == y) / n if n else 0.0
        out.append({
            "pair": [m1, m2],
            "n": n,
            "observed_agreement": round(observed, 4),
            "cohens_kappa": round(cohens_kappa(slot["a"], slot["b"]), 4),
        })
    return {
        "items_with_multiple_labels": len(multi),
        "items_total": len(dataset),
        "pairs": out,
    }


# ── datasets ────────────────────────────────────────────────────────────────

def load_dataset(name: str = "seed") -> list[dict]:
    """Load a labeled dataset. Seeds ship with the package so `eval` produces a
    real number on first install."""
    path = Path(name)
    if not path.exists():
        path = SEED_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"dataset {name!r} not found (tried {name!r} and {path})")
    data = json.loads(path.read_text())
    if isinstance(data, dict) and "traces" in data:
        return data["traces"]
    return data


def save_dataset(dataset: list[dict], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"traces": dataset}, indent=2) + "\n")
    return p


# ── evaluation ──────────────────────────────────────────────────────────────

def evaluate(judge_name: str, dataset: list[dict], checkset: str | None = None,
             judge: Any | None = None, workers: int = 1) -> dict:
    """Measure a judge against labeled traces in ONE judge pass.

    ``predict_dataset`` runs the judge a single time; the confusion matrix and
    the calibration numbers are both derived from that same pass. That is the
    difference between a 1000-item eval costing 1000 judge calls and costing
    2000, and it is why the meter reads what it reads.
    """
    from agentcheck import calibration as cal

    cs_name = checkset or (dataset[0].get("checkset") if dataset else None) or "safety"
    cs = check_lib.get(cs_name)
    j = judge or get_judge(judge_name)

    _, verdict_id, usable, errors = cal.predict_dataset(
        j, dataset, checkset=cs_name, gate=cal.DEFAULT_GATE, workers=workers)

    tp = fp = tn = fn = 0
    for item in usable:
        pred = item["value"] != "pass"
        label = item["gold"] != "pass"
        if pred and label:
            tp += 1
        elif pred and not label:
            fp += 1
        elif not pred and not label:
            tn += 1
        else:
            fn += 1

    n = tp + fp + tn + fn
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    indep = independence(judge_name, dataset, judge=j)
    calib = cal.build_report(j.name, cs.name, verdict_id, usable, errors,
                             gate=cal.DEFAULT_GATE, model=getattr(j, "_model", None))

    return {
        "judge": j.name,
        "model": getattr(j, "_model", None),
        "checkset": cs.name,
        "verdict_check": verdict_id,
        "n": n,
        "errors": errors,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(2 * precision * recall / max(precision + recall, 1e-9), 4),
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "independence": indep,
        "calibration": {
            "ece_decided": calib["decided"]["ece"],
            "accuracy_decided": calib["decided"]["accuracy"],
            "coverage": calib["decided"]["coverage"],
            "read": cal.verdict_for_ece(calib["decided"]["ece"], calib["decided"]["n"]),
        },
    }
