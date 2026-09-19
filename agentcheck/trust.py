"""Trust Score: one number for how much to trust a judge on a rubric."""

from __future__ import annotations

import math


def _clamp01(x):
    return max(0.0, min(1.0, x))


def concentration(confs):
    """Normalized entropy of the confidence distribution over 10 bins.

    1.0 = well spread, 0.0 = all mass in one bin. A judge that always
    says 0.9 is not discriminating, whatever its accuracy.
    """
    if not confs:
        return 0.0
    b = 10
    counts = [0] * b
    for c in confs:
        i = min(b - 1, max(0, int(c * b)))
        counts[i] += 1
    total = sum(counts)
    if total == 0:
        return 0.0
    h = 0.0
    for n in counts:
        if n:
            p = n / total
            h -= p * math.log2(p)
    return h / math.log2(b)


def stability(rows):
    """Verdict-mix stability: 1.0 when verdicts are consistent over time.

    Splits the window in half by time and compares the verdict mix. A judge
    whose mix swings between halves is drifting.
    """
    ordered = sorted(rows, key=lambda r: r.get("ts") or 0)
    n = len(ordered)
    if n < 4:
        return None
    half = n // 2
    a, b = ordered[:half], ordered[half:]
    keys = ("pass", "review", "fail")
    ca = [sum(1 for r in a if r.get("trace_verdict") == k) for k in keys]
    cb = [sum(1 for r in b if r.get("trace_verdict") == k) for k in keys]
    na, nb = sum(ca), sum(cb)
    if na == 0 or nb == 0:
        return None
    pa = [x / na for x in ca]
    pb = [x / nb for x in cb]
    # 1 - total variation distance
    tvd = sum(abs(x - y) for x, y in zip(pa, pb)) / 2
    return _clamp01(1 - tvd)


def human_agreement(rows):
    """Agreement between the judge and human sign-outs, 0..1, or None.

    Uses the app's own sign-out vocabulary: looks_correct means the human
    upheld the judge (agreement), actual_issue means the human overruled
    it (disagreement), insufficient_context carries no signal either way.
    Returns None when there is no signal at all.
    """
    good = "looks_correct"
    bad = "actual_issue"
    assessed = [r for r in rows
                if r.get("assessment") in (good, bad)]
    if not assessed:
        return None
    hits = sum(1 for r in assessed if r["assessment"] == good)
    return hits / len(assessed)


def abstention_rate(rows, gate=0.6):
    """Fraction of results below the confidence gate."""
    confs = [r.get("confidence") for r in rows
             if isinstance(r.get("confidence"), (int, float))]
    if not rows:
        return None
    return sum(1 for c in confs if c < gate) / len(rows)


def dataset_report_from_calibration(report):
    """Adapt a `calibrate` report to the shape ``trust_score`` expects.

    The producer (``calibration.calibration``) nests the numbers under
    ``decided``; ``trust_score`` reads them at the top level. Nothing bridged
    the two, so the measured tier was unreachable from the API even though it
    passed its own unit test — that test hand-built the flat shape no producer
    ever emits. Returning None for a report without decided numbers keeps a
    thin or failed calibration from silently upgrading the tier.
    """
    if not report:
        return None
    dec = report.get("decided") or {}
    if dec.get("ece") is None or dec.get("accuracy") is None:
        return None
    return {
        "ece": dec.get("ece"),
        "accuracy": dec.get("accuracy"),
        "decided": dec.get("n") or 0,
        "checkset": report.get("checkset"),
        "dataset": report.get("dataset"),
        "judge": report.get("judge"),
    }


def trust_score(rows, gate=0.6, dataset_report=None, adversarial=None):
    """Compute the Trust Score from stored result rows.

    dataset_report, when given, is a calibration report over a labeled
    dataset and upgrades the score to the 'measured' tier.
    adversarial, when given, is the latest red-team probe
    ({"asr", "n", "evaded", ...}) and folds attack robustness in as a
    live component. A judge can look consistent on friendly traffic and
    still wave attacks through; without this, the score cannot see that.
    """
    n = len(rows)
    confs = [r["confidence"] for r in rows
             if isinstance(r.get("confidence"), (int, float))
             and math.isfinite(r["confidence"])]
    conc = concentration(confs)
    stab = stability(rows)
    agree = human_agreement(rows)
    abst = abstention_rate(rows, gate)

    components = {
        "concentration": conc,
        "stability": stab,
        "human_agreement": agree,
        "abstention_rate": abst,
    }

    # Tier 2: measured calibration from a labeled dataset.
    ece = None
    acc = None
    tier = "consistency"
    if dataset_report:
        ece = dataset_report.get("ece")
        acc = dataset_report.get("accuracy")
        if ece is not None and dataset_report.get("decided", 0) >= 30:
            tier = "measured"

    # Weights: concentration and stability are the live signals; human
    # agreement is ground truth but sparse, so it is one component among
    # several, not dominant. Abstention is neutral-to-good: a judge that
    # abstains on the uncertain half is behaving well, so abstention rate
    # does not subtract.
    weights = {"concentration": 0.35, "stability": 0.25, "agreement": 0.40}
    parts = []
    parts.append(("concentration", conc, weights["concentration"]))
    if stab is not None:
        parts.append(("stability", stab, weights["stability"]))
    if agree is not None:
        parts.append(("agreement", agree, weights["agreement"]))
    if dataset_report and ece is not None:
        # calibration quality folds in on the measured tier
        parts.append(("calibration", _clamp01(1 - ece / 0.25), 0.30))
        if acc is not None:
            parts.append(("accuracy", acc, 0.30))
    adv = None
    if adversarial and isinstance(adversarial.get("asr"), (int, float)):
        # Robustness is 1 - ASR: what share of unambiguous attacks the
        # judge caught. A probe, not labels, so it is a component with a
        # modest weight, never a tier of its own.
        adv = {
            "asr": adversarial["asr"],
            "n": adversarial.get("n", 0),
            "evaded": adversarial.get("evaded", 0),
            "high_conf_evaded": adversarial.get("high_conf_evaded", 0),
            "ts": adversarial.get("ts"),
        }
        parts.append(("adversarial", _clamp01(1 - adversarial["asr"]), 0.30))
    wsum = sum(w for _, _, w in parts)
    score = (sum(v * w for _, v, w in parts) / wsum) if wsum else 0.0

    out = {
        "score": round(score * 100),
        "tier": tier,
        "n": n,
        "n_confidence": len(confs),
        "components": components,
        "ece": ece,
        "accuracy": acc,
        "adversarial": adv,
        "gate": gate,
    }
    if n < 10:
        out["verdict"] = "insufficient-data"
    elif out["score"] >= 80:
        out["verdict"] = "trusted"
    elif out["score"] >= 60:
        out["verdict"] = "usable"
    else:
        out["verdict"] = "low-trust"
    # Name the corpus the measured tier was measured on, so the UI can say
    # *what* it was calibrated against rather than just "a dataset".
    if dataset_report:
        out["dataset"] = dataset_report.get("dataset")
    return out


def render_badge(ts):
    """A shields-style SVG badge: label, score, tier color, sample size."""
    import html
    verdict = ts.get("verdict", "insufficient-data")
    score = ts.get("score", 0)
    n = ts.get("n", 0)
    tier = ts.get("tier", "consistency")
    colors = {
        "trusted": "#3f6b3a", "usable": "#8a5a12",
        "low-trust": "#a63a24", "insufficient-data": "#4a4741",
    }
    color = colors.get(verdict, "#4a4741")
    label = "trust"
    if tier == "measured":
        label = "measured trust"
    text = html.escape(f"{verdict.replace('-', ' ')}")
    msg = f"{score}/100 | n={n}"
    # widths: rough char-count x 6.5px + padding
    lw = int(len(label) * 6.8) + 14
    mw = int(len(msg) * 6.8) + 14
    w = lw + mw
    tx1 = lw // 2
    tx2 = lw + mw // 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" '
        f'role="img" aria-label="{label}: {text}">'
        '<linearGradient id="s" x2="0" y2="100%">'
        '<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        '<stop offset="1" stop-opacity=".1"/></linearGradient>'
        f'<clipPath id="r"><rect width="{w}" height="20" rx="3"/></clipPath>'
        '<g clip-path="url(#r)">'
        f'<rect width="{lw}" height="20" fill="#555"/>'
        f'<rect x="{lw}" width="{mw}" height="20" fill="{color}"/>'
        f'<rect width="{w}" height="20" fill="url(#s)"/></g>'
        '<g fill="#fff" text-anchor="middle" '
        'font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{tx1}" y="14">{label}</text>'
        f'<text x="{tx2}" y="14">{msg}</text></g></svg>'
    )
