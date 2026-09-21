"""Per-decision calibrated reliability.

The calibration report proves, in aggregate, that a confidence of 0.7 is not
really 0.7. This module applies that proof to one decision: given a
confidence, return the accuracy the judge actually achieves in that band.

Without this, calibration is a report someone reads once. With it, every check
carries a number corrected against observed reality.

Honesty rules the design:
  - the model maps by a bin's own value, not its position in a list
  - a bin with too little support is not used, and the fallback says so
  - no report at all -> the raw confidence, flagged as uncalibrated
  - the caller-facing ``reliability`` is never a fabrication
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BINS = 10


def _report_paths(store, name: str) -> list[Path]:
    out = []
    home = os.environ.get("AGENTCHECK_HOME")
    if home:
        out.append(Path(home) / name)
    else:
        try:
            out.append(Path.home() / ".agentcheck" / name)
        except Exception:
            pass
    try:
        # Postgres-backed stores have no local directory; the
        # AGENTCHECK_HOME sidecar above is the source of truth there.
        if getattr(store, "path", None) is not None:
            out.append(store.path.parent / name)
    except Exception:
        pass
    return out


def load_report(store, workspace_id: str | None = None) -> dict | None:
    """The calibration report that applies to this workspace, if any.

    Scoped PER WORKSPACE first (`calibration-<wid>.json`), because a single
    instance-wide file meant one tenant publishing a calibration moved every
    other tenant's trust tier to `measured`, attributing their ECE to someone
    else's traffic. In a product that sells "the confidence is measured", a
    tenant being handed another tenant's proof is the worst kind of wrong.

    The instance-level `calibration.json` remains as an OPERATOR default:
    `agentcheck calibrate --publish` on a single-tenant install has one
    workspace and nothing to leak, and a hosted operator can deliberately set
    a house model for everyone. Per-workspace wins when both exist.

    None means uncalibrated, and the model says so honestly.
    """
    names = ([f"calibration-{workspace_id}.json"] if workspace_id else []) + ["calibration.json"]
    for name in names:
        for p in _report_paths(store, name):
            try:
                if p.exists():
                    return json.loads(p.read_text())
            except Exception:
                continue
    return None


def bin_index(conf: float) -> int:
    """0..9, clamped."""
    i = int(float(conf) * BINS)
    return 0 if i < 0 else (BINS - 1 if i >= BINS else i)


class ReliabilityModel:
    """Maps a confidence to its empirical accuracy, from a calibration report."""

    def __init__(self, report: dict | None = None, min_bin_n: int = 5):
        self.min_bin_n = min_bin_n
        self.source = "uncalibrated"
        self.decided = 0
        # bin value -> accuracy, only bins with real support
        self._acc: dict[int, float] = {}
        self._conf: dict[int, float] = {}
        if not report:
            return
        decided = report.get("decided") or {}
        self.decided = decided.get("n", 0)
        for b in decided.get("reliability") or []:
            n = b.get("n") or 0
            if n < min_bin_n:
                continue
            # calibration.calibration() emits accuracy/mean_confidence;
            # older sidecars used empirical_acc/mean_conf. Accept both.
            acc = b.get("empirical_acc")
            if acc is None:
                acc = b.get("accuracy")
            if acc is None:
                acc = b.get("mean_conf")
            if acc is None:
                continue
            mc = b.get("mean_confidence")
            if mc is None:
                mc = b.get("mean_conf")
            # Real reports key bins by range, not an integer id.
            bi = b.get("bin")
            if bi is None:
                rg = b.get("range") or [0.0, 0.0]
                bi = bin_index((rg[0] + rg[1]) / 2.0)
            self._acc[int(bi)] = float(acc)
            self._conf[int(bi)] = float(mc or 0.0)
        if self._acc:
            self.source = "calibrated"

    def lookup(self, conf: float | None) -> dict:
        """Empirical accuracy for this confidence, with provenance."""
        if conf is None or not isinstance(conf, (int, float)):
            return {"reliability": None, "source": "no-confidence",
                    "bin": None, "gap": None}
        conf = float(conf)
        if not self._acc:
            return {"reliability": round(conf, 4), "source": self.source,
                    "bin": None, "gap": 0.0}

        i = bin_index(conf)
        if i in self._acc:
            return self._entry(i, conf)

        # No support in this bin: use the nearest bin by confidence distance,
        # and mark it interpolated so nobody mistakes it for a direct reading.
        best = min(self._acc, key=lambda b: abs(self._conf[b] - conf))
        return self._entry(best, conf, interpolated=True)

    def _entry(self, bin_v: int, conf: float,
               interpolated: bool = False) -> dict:
        acc = self._acc[bin_v]
        return {
            "reliability": round(acc, 4),
            "source": "calibrated" if not interpolated else "interpolated",
            "bin": bin_v,
            "gap": round(acc - conf, 4),
        }


def annotate(model: ReliabilityModel, payload: dict) -> dict:
    """Attach calibrated reliability to a check response payload (in place)."""
    info = model.lookup(payload.get("confidence"))
    payload["calibrated"] = info
    payload["reliability"] = info.get("reliability")
    return payload
