"""Calibration: does the confidence number mean anything?

The product claim is "calibrated probabilities". Until metrics measure it, the
claim is decoration. Calibration asks: when the judge says 0.9, is it right
about 90% of the time? A judge can have good accuracy and still be badly
calibrated, which is exactly the failure that makes confidence-gating unsafe.

Measured here:
  ECE   expected calibration error — the count-weighted mean gap between
        confidence and observed accuracy across bins. 0 is perfect.
  MCE   worst single bin gap.
  Brier mean squared error of confidence against the outcome.
  risk/coverage  accuracy among decided items as the abstention gate moves.

Definitions used, stated so the numbers are not over-read:
  * An item is DECIDED when the verdict confidence is at or above the gate.
    Items below the gate are abstained and reported separately, never binned.
  * Correctness is 3-way exact match when the dataset carries a gold verdict,
    otherwise binary (gold non-pass vs pass).
  * ECE is computed over decided items only. A judge that abstains on its hard
    cases should not be scored as if it had answered them.

Robustness: ``predict_dataset`` is the single judge pass — eval, calibration and
the RAG layer all build on it, so a dataset is never judged twice and there is
exactly one place where the judge is called.
"""

from __future__ import annotations

from typing import Any, Callable

DEFAULT_GATE = 0.6
DEFAULT_BINS = 10


def _gold(rec: dict) -> tuple[str | None, bool]:
    """(three_way_label, is_positive). Three-way is None when unavailable."""
    three = rec.get("label_verdict")
    positive = rec.get("should_fail")
    if positive is None and three is not None:
        positive = three != "pass"
    return three, bool(positive)


def _finite(x: float | None) -> float | None:
    """A NaN or Inf confidence is corrupt data, not a real answer. Say None."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def predict_dataset(judge: Any, dataset: list[dict], checkset: str = "safety",
                    gate: float = DEFAULT_GATE, workers: int = 1
                    ) -> tuple[str, str, list[dict], list[str]]:
    """ONE judge pass over every trace.

    Returns (checkset, verdict_check_id, usable_items, errors). Used by
    ``evaluate`` (confusion) and ``calibration`` (ECE) alike, so a 1,000 item
    eval costs 1,000 judge calls, not 2,000. ``workers > 1`` threads the judge
    calls with a bounded pool; the judge must be stateless per call, which the
    shipped adapters are.
    """
    from agentcheck import checks as check_lib
    cs = check_lib.get(checkset)
    verdict_id = cs.verdict
    questions = list(cs.checks)

    def one(rec: dict) -> dict:
        trace = rec.get("trace") or rec
        if rec.get("label_verdict") is None and rec.get("should_fail") is None:
            # No ground truth at all. Grading this as "pass" would mint
            # accuracy out of nothing (unlabeled imports used to do exactly
            # that), so refuse it and say why.
            return {"error": "no gold verdict; label this record first",
                    "trace": trace}
        three, positive = _gold(rec)
        try:
            judgment = judge.ask(trace, questions)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "trace": trace}
        answers = {a.question_id: a for a in judgment.answers}
        verdict = answers.get(verdict_id)
        if verdict is None:
            return {"error": f"no answer for {verdict_id!r}", "trace": trace}
        conf = _finite(verdict.confidence)
        if conf is None:
            conf = 0.0
        value = str(verdict.value) if verdict.value is not None else ""
        correct = (value == three) if three else ((value != "pass") == positive)
        return {
            "value": value,
            "gold": three if three else ("fail" if positive else "pass"),
            "correct": bool(correct),
            "confidence": conf,
            "decided": conf >= gate,
            "trace": trace,
            "model": getattr(getattr(judge, "_model", None), "model", None),
        }

    if workers and workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        items = list(ThreadPoolExecutor(max_workers=workers).map(one, dataset))
    else:
        items = [one(r) for r in dataset]

    usable = [i for i in items if "error" not in i]
    errors = [i.get("error") for i in items if "error" in i]
    return cs.name, verdict_id, usable, errors


def _brier(pairs: list[tuple[float, int]]) -> float:
    if not pairs:
        return 0.0
    return sum((c - correct) ** 2 for c, correct in pairs) / len(pairs)


def _ece(pairs: list[tuple[float, int]], bins: int) -> tuple[float, float, list[dict]]:
    """Returns (ECE, MCE, reliability table)."""
    if not pairs:
        return 0.0, 0.0, []
    n = len(pairs)
    table: list[dict] = []
    ece = 0.0
    mce = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        bucket = [(c, y) for c, y in pairs
                  if (lo <= c < hi) or (b == bins - 1 and c >= hi)]
        if not bucket:
            table.append({"range": [round(lo, 2), round(hi, 2)], "n": 0,
                          "mean_confidence": None, "accuracy": None, "gap": None})
            continue
        mean_conf = sum(c for c, _ in bucket) / len(bucket)
        acc = sum(y for _, y in bucket) / len(bucket)
        gap = acc - mean_conf
        ece += (len(bucket) / n) * abs(gap)
        mce = max(mce, abs(gap))
        table.append({
            "range": [round(lo, 2), round(hi, 2)],
            "n": len(bucket),
            "mean_confidence": round(mean_conf, 4),
            "accuracy": round(acc, 4),
            "gap": round(gap, 4),
        })
    return ece, mce, table


def _risk_coverage(pairs: list[tuple[float, int]],
                   thresholds: tuple[float, ...] = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95),
                   ) -> list[dict]:
    """Accuracy among items whose confidence clears each gate."""
    out = []
    for t in thresholds:
        kept = [(c, y) for c, y in pairs if c >= t]
        out.append({
            "gate": t,
            "coverage": round(len(kept) / len(pairs), 4) if pairs else 0.0,
            "n_decided": len(kept),
            "accuracy": round(sum(y for _, y in kept) / len(kept), 4) if kept else None,
        })
    return out


def _acc(rows: list[dict]) -> float | None:
    return round(sum(1 for i in rows if i["correct"]) / len(rows), 4) if rows else None


def build_report(judge_name: str, checkset: str, verdict_id: str,
                 usable: list[dict], errors: list[str],
                 gate: float = DEFAULT_GATE, bins: int = DEFAULT_BINS,
                 model: str | None = None) -> dict:
    """Pure math over a single predict pass; no judge calls."""
    decided = [i for i in usable if i["decided"]]
    abstained = [i for i in usable if not i["decided"]]
    all_pairs = [(i["confidence"], 1 if i["correct"] else 0) for i in usable]
    dec_pairs = [(i["confidence"], 1 if i["correct"] else 0) for i in decided]
    ece_dec, mce_dec, table_dec = _ece(dec_pairs, bins)
    ece_all, mce_all, table_all = _ece(all_pairs, bins)

    return {
        "judge": judge_name,
        "model": model,
        "checkset": checkset,
        "verdict_check": verdict_id,
        "gate": gate,
        "bins": bins,
        "n": len(usable),
        "undecided_or_error": len(errors),
        "decided": {
            "n": len(decided),
            "coverage": round(len(decided) / len(usable), 4) if usable else 0.0,
            "accuracy": round(sum(1 for i in decided if i["correct"]) / len(decided), 4)
            if decided else None,
            "mean_confidence": round(
                sum(c for c, _ in dec_pairs) / len(dec_pairs), 4) if dec_pairs else None,
            "ece": round(ece_dec, 4),
            "mce": round(mce_dec, 4),
            "brier": round(_brier(dec_pairs), 4),
            "reliability": table_dec,
        },
        "abstained": {
            "n": len(abstained),
            "accuracy_if_forced": round(
                sum(1 for i in abstained if i["correct"]) / len(abstained), 4)
            if abstained else None,
            "mean_confidence": round(
                sum(i["confidence"] for i in abstained) / len(abstained), 4)
            if abstained else None,
        },
        "all_items": {
            "accuracy": round(sum(1 for i in usable if i["correct"]) / len(usable), 4)
            if usable else None,
            "ece": round(ece_all, 4),
            "mce": round(mce_all, 4),
            "brier": round(_brier(all_pairs), 4),
            "reliability": table_all,
        },
        "risk_coverage": _risk_coverage(all_pairs),
        "errors": errors,
    }


def calibration(judge_name: str, dataset: list[dict], checkset: str = "safety",
                gate: float = DEFAULT_GATE, bins: int = DEFAULT_BINS,
                judge: Any | None = None, workers: int = 1) -> dict:
    """Run the judge once and report its calibration."""
    from agentcheck.judges import get_judge
    j = judge or get_judge(judge_name)
    cs_name, verdict_id, usable, errors = predict_dataset(
        j, dataset, checkset=checkset, gate=gate, workers=workers)
    rep = build_report(j.name, cs_name, verdict_id, usable, errors,
                       gate=gate, bins=bins, model=getattr(j, "_model", None))
    rep["read"] = verdict_for_ece(rep["decided"]["ece"], rep["decided"]["n"])
    return rep


def verdict_for_ece(ece: float, n: int) -> str:
    """A blunt read, so the number is not left to the reader."""
    if n < 30:
        return "insufficient-data"
    if ece <= 0.05:
        return "well-calibrated"
    if ece <= 0.12:
        return "usable"
    return "miscalibrated"
