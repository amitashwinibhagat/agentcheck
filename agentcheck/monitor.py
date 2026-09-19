"""Monitoring rollups over stored judgments.

Arize Phoenix, Langfuse and W&B Weave all pair evaluation with a time series so
you can see a regression land. AgentCheck already stores every judgment with its
verdict, confidence, model, and request id, so the rollup is a query rather than
a new pipeline.

Two views:
  * ``timeline``  — per bucket: volume, verdict mix, mean confidence, flagged rate
  * ``drift``     — first half vs second half of the window, to catch a judge or
                    an agent changing behaviour without a deploy
"""

from __future__ import annotations

import datetime
import time
from typing import Any

BUCKETS = {"hour": 3600, "day": 86400, "week": 604800}


def _bucket_start(ts: float, size: int) -> float:
    return ts - (ts % size)


def _label(ts: float, size: int) -> str:
    dt = datetime.datetime.fromtimestamp(_bucket_start(ts, size))
    if size == BUCKETS["hour"]:
        return dt.strftime("%Y-%m-%d %H:00")
    if size == BUCKETS["week"]:
        return "week of " + dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


def timeline(store: Any, user_key: str | None = None, bucket: str = "day",
             since_days: int = 30, buckets: int | None = None) -> dict:
    """Verdict, confidence and volume per time bucket from stored results."""
    if bucket not in BUCKETS:
        raise ValueError(f"bucket must be one of {sorted(BUCKETS)}")
    size = BUCKETS[bucket]
    since = time.time() - since_days * 86400
    rows = _rows(store, user_key, since)

    agg: dict[float, dict] = {}
    for r in rows:
        key = _bucket_start(float(r["ts"]), size)
        slot = agg.setdefault(key, {"n": 0, "pass": 0, "review": 0, "fail": 0,
                                    "conf_sum": 0.0, "conf_n": 0,
                                    "assessed": 0, "models": set()})
        slot["n"] += 1
        v = r.get("verdict")
        if v in ("pass", "review", "fail"):
            slot[v] += 1
        if r.get("confidence") is not None:
            slot["conf_sum"] += float(r["confidence"])
            slot["conf_n"] += 1
        if r.get("assessment"):
            slot["assessed"] += 1
        if r.get("model"):
            slot["models"].add(r["model"])

    out = []
    for key in sorted(agg):
        s = agg[key]
        out.append({
            "bucket": _label(key + 1, size),
            "ts": key,
            "n": s["n"],
            "pass": s["pass"],
            "review": s["review"],
            "fail": s["fail"],
            "flagged_rate": round(s["fail"] / s["n"], 4) if s["n"] else 0.0,
            "review_rate": round((s["fail"] + s["review"]) / s["n"], 4) if s["n"] else 0.0,
            "mean_confidence": round(s["conf_sum"] / s["conf_n"], 4) if s["conf_n"] else None,
            "signed_out": s["assessed"],
            "models": sorted(s["models"]),
        })
    if buckets:
        out = out[-buckets:]
    return {
        "bucket": bucket,
        "since_days": since_days,
        "total": sum(b["n"] for b in out),
        "buckets": out,
    }


def drift(store: Any, user_key: str | None = None, bucket: str = "day",
          since_days: int = 30) -> dict:
    """Compare the newer half of the window against the older half."""
    tl = timeline(store, user_key, bucket, since_days)
    rows = tl["buckets"]
    if len(rows) < 2:
        return {"ok": False, "reason": "need at least two buckets to compare"}
    mid = len(rows) // 2
    early, late = rows[:mid], rows[mid:]

    def mix(part: list[dict]) -> dict:
        n = sum(b["n"] for b in part) or 1
        conf_n = sum(b["n"] for b in part if b["mean_confidence"] is not None) or 1
        return {
            "n": sum(b["n"] for b in part),
            "flagged_rate": round(sum(b["fail"] for b in part) / n, 4),
            "review_rate": round(sum(b["fail"] + b["review"] for b in part) / n, 4),
            "mean_confidence": round(
                sum(b["mean_confidence"] * b["n"] for b in part
                    if b["mean_confidence"] is not None) / conf_n, 4),
        }

    e, l = mix(early), mix(late)
    changes = {
        "flagged_rate": round(l["flagged_rate"] - e["flagged_rate"], 4),
        "review_rate": round(l["review_rate"] - e["review_rate"], 4),
        "mean_confidence": round(l["mean_confidence"] - e["mean_confidence"], 4),
    }
    notes = []
    if abs(changes["flagged_rate"]) >= 0.15:
        notes.append("flagged rate moved by 15pp or more")
    if abs(changes["mean_confidence"]) >= 0.1:
        notes.append("mean confidence moved by 0.10 or more")
    return {"ok": True, "early": e, "late": l, "change": changes, "notes": notes}


def _rows(store: Any, user_key: str | None, since: float) -> list[dict]:
    sql = ("SELECT ts, verdict, confidence, model, assessment FROM results "
           "WHERE ts >= ?")
    params: list[Any] = [since]
    if user_key:
        sql += " AND user_key = ?"
        params.append(user_key)
    with store._conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]
