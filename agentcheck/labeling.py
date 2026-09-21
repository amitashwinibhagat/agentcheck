"""Choosing which calls to label, and why a good batch is not a prefix.

Thirty sign-outs is the price of a measured tier. Taking them in log order
spends that price on the least informative sample available: a queue is
usually many near-identical passes and a few failures, so the first 30 rows
can easily be 30 variations of the same call. The judge then looks perfectly
calibrated because it was only ever asked easy things, and the ECE that comes
out is a measurement of the sample, not of the judge.

A useful batch is stratified on the two axes that decide what ECE can see:

* **Verdict** — every `fail` and `review` is included first. Those are where
  over-confidence lives; a batch with no failures cannot show any.
* **Confidence band** — passes are drawn round-robin across the bands, so the
  reliability curve gets points across its width instead of a spike at 0.88.

and diversified on the axis that decides whether the number generalises:
**the tool**. Twelve distinct tools teach more than thirty calls to `sql`.

Deliberately pure (rows in, selection out) so the API, the CLI and any test
agree on the same batch, and deterministic so the same queue yields the same
plan — a batch that reshuffles on reload is unusable as a to-do list.
"""

from __future__ import annotations

from typing import Iterable, Sequence

#: Confidence bands used for stratification. Coarse on purpose: six bands over
#: 30 labels puts ~5 in each, which is the smallest number that reads as a
#: shape rather than noise.
BANDS = ((0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01))


def _band(conf: float) -> int:
    for i, (lo, hi) in enumerate(BANDS):
        if lo <= conf < hi:
            return i
    return len(BANDS) - 1


def _verdict(row: dict) -> str | None:
    """The verdict, from either shape of a results row.

    Store rows carry `verdict` (the column) and public/API rows carry
    `trace_verdict`. Reading only one silently made every row "unknown", which
    disabled the stratification without erroring — a batch that looks fine and
    measures nothing.
    """
    v = row.get("trace_verdict") or row.get("verdict")
    return None if v is None else str(v)


def _tool(row: dict) -> str:
    return str(row.get("tool") or "unknown")


def _usable(row: dict) -> bool:
    """Could a sign-out on this row ever count toward a calibration?

    `calibration.report_from_signoffs` needs a finite confidence to place the
    item on the curve. A row whose judgement errored has none, so asking
    someone to label it spends one of their 30 sign-outs on something that can
    never count. Those rows are excluded from the batch rather than silently
    wasted.
    """
    conf = row.get("confidence")
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return False
    return c == c and abs(c) != float("inf")


def select_batch(rows: Sequence[dict], size: int = 30) -> dict:
    """Pick up to `size` unlabeled calls, most informative first.

    Returns {"ids": [...], "coverage": {...}, "considered": n}. Only rows with
    no assessment yet AND a confidence that could be calibrated are eligible —
    labeling something already labeled teaches nothing, and labeling an errored
    judgement cannot count.
    """
    live = [r for r in rows if not r.get("assessment") and _usable(r)]
    fails = [r for r in live if _verdict(r) == "fail"]
    reviews = [r for r in live if _verdict(r) == "review"]
    passes = [r for r in live if _verdict(r) == "pass"]
    others = [r for r in live if _verdict(r) not in ("pass", "review", "fail")]

    # Rarest first: a small, high-signal stratum must not be crowded out by a
    # large easy one. Within a stratum, newest first — the log reads top-down.
    def newest(rs: Iterable[dict]) -> list[dict]:
        return sorted(rs, key=lambda r: r.get("ts") or 0, reverse=True)

    chosen: list[dict] = []
    chosen += newest(others)
    chosen += newest(fails)
    chosen += newest(reviews)

    # Passes: round-robin over confidence bands so the curve gets its width,
    # and within a band prefer a tool not yet in the batch so 30 labels cover
    # more than one code path.
    buckets: dict[int, list[dict]] = {}
    for r in newest(passes):
        try:
            band = _band(float(r.get("confidence")))
        except (TypeError, ValueError):
            band = len(BANDS) - 1  # unparseable confidence is its own signal
        buckets.setdefault(band, []).append(r)

    tools: list[str] = [_tool(r) for r in chosen]
    while len(chosen) < size and any(buckets.values()):
        for band in sorted(buckets):
            if len(chosen) >= size:
                break
            pool = buckets[band]
            if not pool:
                continue
            pick = next((r for r in pool if _tool(r) not in tools), pool[0])
            pool.remove(pick)
            tools.append(_tool(pick))
            chosen.append(pick)

    chosen = chosen[:size]
    by_verdict: dict[str, int] = {}
    by_band: dict[str, int] = {}
    for r in chosen:
        v = _verdict(r) or "unknown"
        by_verdict[v] = by_verdict.get(v, 0) + 1
        try:
            b = BANDS[_band(float(r.get("confidence")))]
            key = f"{b[0]:.1f}-{b[1]:.1f}"
        except (TypeError, ValueError):
            key = "unknown"
        by_band[key] = by_band.get(key, 0) + 1
    return {
        "ids": [r.get("id") for r in chosen],
        "considered": len(rows),
        "unlabeled": len(live),
        "coverage": {
            "verdicts": by_verdict,
            "bands": by_band,
            "tools": sorted({t for t in tools if t != "unknown"}),
        },
    }
