"""Dataset registry: named, splittable, versioned labeled corpora.

Confident AI and LangSmith both treat the dataset as a first-class object with
versions and splits. This keeps that idea without a server: datasets live under
``$AGENTCHECK_HOME/datasets/<name>/`` with an immutable ``v<N>.json`` plus a
``current`` pointer, so a number quoted in a report can always be traced back to
the exact corpus that produced it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from agentcheck.labels import SEED_DIR


def root() -> Path:
    home = os.environ.get("AGENTCHECK_HOME") or (Path.home() / ".agentcheck")
    return Path(home).expanduser() / "datasets"


def _slug(name: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in name.strip().lower()]
    slug = "".join(keep).strip("-")
    if not slug:
        raise ValueError(f"dataset name {name!r} has no usable characters")
    return slug


def dir_for(name: str) -> Path:
    return root() / _slug(name)


def versions(name: str) -> list[Path]:
    d = dir_for(name)
    if not d.is_dir():
        return []
    def key(p: Path) -> int:
        try:
            return int(p.stem.lstrip("v"))
        except ValueError:
            return 0
    return sorted(d.glob("v*.json"), key=key)


def current_path(name: str) -> Path | None:
    d = dir_for(name)
    ptr = d / "current"
    if ptr.is_file():
        target = d / ptr.read_text().strip()
        return target if target.is_file() else None
    vs = versions(name)
    return vs[-1] if vs else None


def load(name: str, version: int | None = None,
         split: str | None = None) -> list[dict]:
    """Load a dataset: registry first, then a bundled seed, then a path."""
    if version is not None:
        p = dir_for(name) / f"v{version}.json"
        if not p.is_file():
            raise FileNotFoundError(f"{name} has no version {version}")
    else:
        p = current_path(name)
    if p is None:
        candidate = Path(name)
        if candidate.is_file():
            p = candidate
        else:
            seed = SEED_DIR / f"{name}.json"
            if seed.is_file():
                p = seed
            else:
                raise FileNotFoundError(
                    f"dataset {name!r} not found in {root()}, as a path, "
                    f"or as a bundled seed")
    data = json.loads(p.read_text())
    rows = data["traces"] if isinstance(data, dict) and "traces" in data else data
    if split:
        rows = apply_split(rows, split)
    return rows


def save(name: str, rows: list[dict], note: str = "") -> Path:
    """Write a new immutable version and point ``current`` at it."""
    d = dir_for(name)
    d.mkdir(parents=True, exist_ok=True)
    n = len(versions(name)) + 1
    p = d / f"v{n}.json"
    p.write_text(json.dumps({
        "name": _slug(name),
        "version": n,
        "saved_at": time.time(),
        "note": note,
        "traces": rows,
    }, indent=2) + "\n")
    (d / "current").write_text(f"v{n}.json\n")
    return p


def split_of(row: dict, seed: str = "agentcheck") -> str:
    """Deterministic 80/20 split by trace content, so it never reshuffles."""
    trace = row.get("trace") or row
    blob = json.dumps({"s": trace, "seed": seed}, sort_keys=True, default=str)
    h = int(hashlib.sha256(blob.encode()).hexdigest()[:8], 16)
    return "test" if h % 5 == 0 else "dev"


def apply_split(rows: list[dict], split: str, seed: str = "agentcheck") -> list[dict]:
    if split == "all":
        return list(rows)
    if split not in ("dev", "test"):
        raise ValueError(f"split must be dev, test or all; got {split!r}")
    return [r for r in rows if split_of(r, seed) == split]


def _issue(rows: list[dict], i: int, msg: str) -> str:
    return f"row {i}: {msg}"


def validate(rows: list[dict], require_labels: bool = True) -> list[str]:
    """Structural checks on a dataset before any number from it is trusted.

    Flags: records that are not dicts, traces with neither request+tool nor the
    RAG shape (question+answer+contexts), and — when ``require_labels`` — items
    that a *labeled* dataset needs to score against. Importing raw traces to
    label passes ``require_labels=False``, because they are not labeled yet.
    """
    problems: list[str] = []
    for i, rec in enumerate(rows):
        if not isinstance(rec, dict):
            problems.append(_issue(rows, i, f"record is {type(rec).__name__}, not an object"))
            continue
        t = rec.get("trace") or rec
        if not isinstance(t, dict):
            problems.append(_issue(rows, i, f"trace is {type(t).__name__}"))
            continue
        has_tool = bool(t.get("request") and t.get("tool"))
        has_rag = bool(t.get("question") and t.get("answer")
                       and isinstance(t.get("contexts"), list))
        if not has_tool and not has_rag:
            problems.append(_issue(
                rows, i,
                "trace has neither request+tool nor question+answer+contexts"))
        if not require_labels:
            continue
        three = rec.get("label_verdict")
        positive = rec.get("should_fail")
        if (three is None and positive is None
                and "labels" not in rec and "labeler" not in rec
                and "labeler_model" not in rec):
            problems.append(_issue(rows, i, "unlabeled item in a labeled dataset"))
        elif three is not None and not isinstance(three, str):
            problems.append(_issue(rows, i, "label_verdict must be a string"))
        elif positive is not None and not isinstance(positive, bool):
            problems.append(_issue(rows, i, "should_fail must be a boolean"))
    return problems


def inventory() -> list[dict]:
    """Every registered dataset with its version count and split sizes."""
    out = []
    r = root()
    if not r.is_dir():
        return out
    for d in sorted(p for p in r.iterdir() if p.is_dir()):
        vs = versions(d.name)
        if not vs:
            continue
        rows = json.loads(vs[-1].read_text()).get("traces", [])
        n_dev = sum(1 for x in rows if split_of(x) == "dev")
        out.append({
            "name": d.name,
            "versions": len(vs),
            "current": vs[-1].stem,
            "n": len(rows),
            "dev": n_dev,
            "test": len(rows) - n_dev,
            "labelers": sorted({(x.get("labeler_model") or x.get("labeler") or "?")
                                for x in rows}),
        })
    return out
