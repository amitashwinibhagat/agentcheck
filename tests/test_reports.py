"""Phase A deliverables: demo datasets load & validate, calibration HTML
renders with the honest guards, report-card wiring, report-calibration roundtrip."""

import json
import tempfile
from pathlib import Path

from agentcheck import reports as rpt  # noqa: E402
from agentcheck.evals import datasets as ds  # noqa: E402
from agentcheck.judges.base import Answer, Judgment  # noqa: E402


def _rep(n=40, errors=None, sqrt=0.6, gate=0.6):
    """A plausible calibration dict with a spread across bins."""
    dec_n = n - len(errors or [])
    bins = []
    # conf bins that produce a clean-ish ECE
    for lo, hi, cn, acc in ((0.3, 0.4, dec_n // 4, 0.4),
                            (0.6, 0.7, dec_n // 2, 0.65),
                            (0.8, 0.9, dec_n - (dec_n // 4) - (dec_n // 2), 0.8)):
        if cn <= 0:
            continue
        bins.append({"range": [lo, hi], "n": cn,
                     "mean_confidence": round((lo + hi) / 2, 2),
                     "accuracy": acc, "gap": round(acc - (lo + hi) / 2, 2)})
    decided = {"n": dec_n, "coverage": round(dec_n / n, 3),
               "accuracy": 0.65, "mean_confidence": 0.62,
               "ece": 0.04, "mce": 0.09, "brier": 0.10,
               "reliability": bins}
    return {
        "judge": "t", "model": "t-m", "checkset": "safety",
        "verdict_check": "verdict", "gate": gate, "bins": 10,
        "n": n, "undecided_or_error": len(errors or []),
        "decided": decided,
        "abstained": {"n": 0, "accuracy_if_forced": None,
                      "mean_confidence": None},
        "all_items": {"accuracy": 0.65, "ece": 0.05, "mce": 0.1,
                      "brier": 0.1, "reliability": bins},
        "read": "well-calibrated",
        "risk_coverage": [{"gate": g, "coverage": round(1 - g * 0.4, 2),
                           "n_decided": int(n * (1 - g * 0.4)),
                           "accuracy": round(0.55 + g * 0.3, 2)}
                          for g in (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)],
        "errors": [],
    }


def test_demo_datasets_exist_and_validate():
    for name in ("agent-demo", "rag-demo"):
        rows = ds.load(name)
        assert rows, name
        assert ds.validate(rows) == [], f"{name}: {ds.validate(rows)}"


def test_agent_demo_is_independently_labeled():
    rows = ds.load("agent-demo")
    labelers = {r.get("labeler_model") for r in rows}
    assert labelers == {"human"}, labelers
    # every row carries a legible gold
    assert all(r["label_verdict"] in ("pass", "review", "fail") for r in rows)
    # enough decided items to clear the calibration guard
    assert len(rows) >= 30, len(rows)


def test_rag_demo_has_ground_truth_for_hallucination_metrics():
    rows = ds.load("rag-demo")
    from agentcheck import metrics as M
    for r in rows:
        assert M.is_rag_trace(r["trace"]), r
        assert r["trace"].get("ground_truth"), "needs ground_truth for correctness metrics"


def test_calibration_html_renders_headline_and_guard():
    rep = _rep()
    html = rpt.render_calibration_html(rep, dataset="d", checkset="safety")
    assert "Calibration" in html and "t / safety" in html
    assert "reliability" in html.lower() or "Reliability" in html
    assert '<svg' in html and 'aria-label="Reliability curve"' in html
    assert 'aria-label="Risk-coverage curve"' in html
    assert "0.04" in html                       # ECE present
    assert "WELL CALIBRATED" in html            # badge for low ECE


def test_calibration_html_refuses_to_claim_on_thin_data():
    rep = _rep(n=12)   # decided < 30 -> read insufficient-data
    rep["decided"]["n"] = 12
    rep["decided"]["ece"] = 0.30
    html = rpt.render_calibration_html(rep)
    assert "INSUFFICIENT DATA" in html or "insufficient-data" in html.lower()
    assert "under 30 decided items" in html, "the guard reason must be visible"


def test_calibration_html_shows_abstention_separately():
    rep = _rep()
    rep["abstained"] = {"n": 8, "accuracy_if_forced": 0.35, "mean_confidence": 0.4}
    html = rpt.render_calibration_html(rep)
    assert "8" in html and "0.35" in html       # abstained is not hidden


def test_write_roundtrip_and_render():
    rep = _rep()
    with tempfile.TemporaryDirectory() as tmp:
        p = rpt.write_calibration_report(rep, Path(tmp) / "r", dataset="d", checkset="safety")
        assert p.endswith(".html") and Path(p).exists()
        # re-render from the raw dict (what report-calibration does)
        html = rpt.render_calibration_html(rep, dataset="d", checkset="safety")
        assert len(html) > 2000


def test_calibration_html_escapes_inputs():
    rep = _rep()
    rep["judge"] = '<script>alert(1)</script>'
    html = rpt.render_calibration_html(rep, dataset='"><img onerror=x>', checkset="safety")
    assert "<script>alert(1)</script>" not in html   # escaped, not injected
    assert '&lt;script&gt;' in html


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("\nall phase-A tests passed")
