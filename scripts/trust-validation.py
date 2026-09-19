#!/usr/bin/env python3
"""Trust validation: does the consistency tier predict actual accuracy?

Method: judge every labeled trace once per judge (stub + typesafe). Then
bootstrap random subsets; for each, compute the consistency-tier score
(no labels) vs the labeled accuracy. Report Pearson + Spearman across all
points. A tier that cannot predict accuracy is decoration — this script
exists to find that out, not to confirm the opposite.

Usage: TYPESAFE_API_KEY=... python scripts/trust-validation.py [--n 25] [--size 24]
"""

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcheck import calibration as cal
from agentcheck import checks as check_lib
from agentcheck import labels as L
from agentcheck import trust as trust_mod
from agentcheck.judges import get_judge


def judge_once(judge_name, dataset, checkset):
    j = get_judge(judge_name)
    cs = check_lib.get(checkset)
    out = []
    for i, rec in enumerate(dataset):
        trace = rec.get("trace") or rec
        try:
            judgment = j.ask(trace, list(cs.checks))
        except Exception as e:
            print(f"  [{judge_name}] item {i} judge failed: {e}")
            continue
        by_id = {a.question_id: a for a in judgment.answers}
        v = by_id.get(cs.verdict)
        gold = rec.get("label_verdict")
        if gold is None:
            gold = "fail" if rec.get("should_fail") else "pass"
        out.append({
            "ts": i,
            "trace_verdict": v.value if v else None,
            "confidence": (v.confidence if v and isinstance(
                v.confidence, (int, float)) and math.isfinite(v.confidence)
                else None),
            "correct": bool(v and v.value == gold),
        })
    return out


def pearson(xs, ys):
    n = len(xs)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return float("nan")
    return cov / math.sqrt(vx * vy)


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0] * len(v)
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    return pearson(ranks(xs), ranks(ys))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--size", type=int, default=24)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    sets = []
    for name in ("agent-demo", "rag-demo", "seed"):
        try:
            sets.append((name, L.load_dataset(name)))
        except FileNotFoundError:
            pass
    print(f"datasets: {[(n, len(d)) for n, d in sets]}")

    points = []  # (judge, score, accuracy, n)
    for judge_name in ("stub", "typesafe"):
        try:
            pool = []
            for name, data in sets:
                cs = (data[0].get("checkset") if data else None) or "safety"
                for row in judge_once(judge_name, data, cs):
                    pool.append(row)
        except Exception as e:
            print(f"[{judge_name}] skipped: {e}")
            continue
        print(f"[{judge_name}] judged {len(pool)} traces")
        for k in range(args.n):
            sub = rng.sample(pool, min(args.size, len(pool)))
            ts = trust_mod.trust_score(sub)
            acc = sum(1 for r in sub if r["correct"]) / len(sub)
            points.append((judge_name, ts["score"], acc, len(sub)))

    scores = [p[1] for p in points]
    accs = [p[2] for p in points]
    r_p = pearson(scores, accs)
    r_s = spearman(scores, accs)
    print(f"\npoints: {len(points)} "
          f"({sum(1 for p in points if p[0]=='stub')} stub, "
          f"{sum(1 for p in points if p[0]=='typesafe')} typesafe)")
    print(f"consistency score vs accuracy: Pearson r = {r_p:.3f}, "
          f"Spearman rho = {r_s:.3f}")
    for judge_name in ("stub", "typesafe"):
        ss = [p[1] for p in points if p[0] == judge_name]
        aa = [p[2] for p in points if p[0] == judge_name]
        if ss:
            print(f"  {judge_name}: mean score {statistics.fmean(ss):.1f}, "
                  f"mean acc {statistics.fmean(aa):.3f}")
    verdict = ("PREDICTIVE" if r_s >= 0.5 else
               "WEAK" if r_s >= 0.3 else "NOT PREDICTIVE")
    print(f"verdict: {verdict} (Spearman >= 0.5 required)")
    out = {"pearson": r_p, "spearman": r_s, "verdict": verdict,
           "points": [{"judge": p[0], "score": p[1], "accuracy": round(p[2], 4),
                       "n": p[3]} for p in points]}
    Path("trust-validation.json").write_text(json.dumps(out, indent=2))
    print("wrote trust-validation.json")
    return 0 if verdict == "PREDICTIVE" else 1


if __name__ == "__main__":
    sys.exit(main())
