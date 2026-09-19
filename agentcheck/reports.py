"""Render calibration results as a self-contained, shareable HTML report.

The CLI prints ECE as numbers; this turns them into something an engineer can
open, screenshot, and send. One file, no dependencies: the data is embedded as
JSON and drawn as inline SVG by a tiny script, so it opens anywhere and never
talks to a network.

Honesty rules baked into the render:
  * fewer than 30 decided items prints `insufficient-data`, not a verdict;
  * the abstained row is shown next to the decided row, never hidden;
  * the reliability table and curve are the evidence, the ECE number is the
    headline on top of them.
"""

from __future__ import annotations

import html
import json
import time
from typing import Any


def _fmt(v: Any, digits: int = 3, dash: str = "—") -> str:
    if v is None:
        return dash
    return f"{float(v):.{digits}f}"


def _esc(s: Any) -> str:
    return html.escape(str(s))


def _reliability_svg(rows: list[dict], width: int = 640, height: int = 360) -> str:
    """SVG reliability curve: diagonal reference + a point per non-empty bin."""
    pad_l, pad_r, pad_t, pad_b = 46, 24, 22, 40
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b
    filled = [r for r in rows if r.get("n")]
    max_n = max((r["n"] for r in filled), default=1)

    def x(c: float) -> float:
        return pad_l + inner_w * c

    def y(a: float) -> float:
        return pad_t + inner_h * (1.0 - a)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="Reliability curve" style="max-width:100%;height:auto">',
        # grid + reference diagonal
        f'<line x1="{x(0)}" y1="{y(0)}" x2="{x(1)}" y2="{y(1)}" stroke="#d4cfc5" '
        f'stroke-dasharray="4 4" stroke-width="1"/>',
        f'<text x="{x(1)-4}" y="{y(1)+14}" fill="#6b6b6b" font-size="10" text-anchor="end">'
        "perfect calibration</text>",
    ]
    # axes
    for label, pos in (("0.0", 0), ("0.5", 0.5), ("1.0", 1.0)):
        parts.append(
            f'<text x="{x(pos)}" y="{height-18}" fill="#6b6b6b" font-size="10" '
            f'text-anchor="middle">{label}</text>')
        parts.append(
            f'<text x="{pad_l-8}" y="{y(pos)+3}" fill="#6b6b6b" font-size="10" '
            f'text-anchor="end">{label}</text>')
    parts.append(f'<text x="{pad_l+inner_w/2}" y="{height-2}" fill="#444" '
                 f'font-size="11" text-anchor="middle">stated confidence</text>')
    parts.append(f'<text x="12" y="{pad_t+inner_h/2}" fill="#444" font-size="11" '
                 f'text-anchor="middle" transform="rotate(-90 12 '
                 f'{pad_t+inner_h/2})">observed accuracy</text>')

    # per-bin points, radius scaled by n
    for r in filled:
        c, a, n = r["mean_confidence"], r["accuracy"], r["n"]
        rad = 3 + 7 * (n / max_n)
        color = "#c0392b" if abs(a - c) > 0.12 else ("#b8860b" if abs(a - c) > 0.05 else "#1a6b3c")
        parts.append(
            f'<circle cx="{x(c):.1f}" cy="{y(a):.1f}" r="{rad:.1f}" fill="{color}" '
            f'fill-opacity="0.85" stroke="#fff" stroke-width="1">'
            f'<title>conf {c:.2f} / acc {a:.2f} / n={n}</title></circle>')
    parts.append("</svg>")
    return "\n".join(parts)


def _risk_svg(points: list[dict], width: int = 640, height: int = 300) -> str:
    """Coverage vs accuracy as the abstention gate moves 0.0 -> 0.95."""
    pad_l, pad_r, pad_t, pad_b = 46, 24, 22, 40
    inner_w, inner_h = width - pad_l - pad_r, height - pad_t - pad_b
    ps = [p for p in points if p.get("accuracy") is not None]

    def x(g: float) -> float:
        return pad_l + inner_w * g / 0.95

    def y(v: float) -> float:
        return pad_t + inner_h * (1.0 - v)

    # lines for accuracy (solid) and coverage (dashed)
    acc_pts = " ".join(f"{x(p['gate']):.1f},{y(p['accuracy']):.1f}" for p in ps)
    cov_pts = " ".join(f"{x(p['gate']):.1f},{y(p['coverage']):.1f}" for p in points)
    return "\n".join([
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="Risk-coverage curve" style="max-width:100%;height:auto">',
        f'<polyline points="{acc_pts}" fill="none" stroke="#1a6b3c" stroke-width="2"/>',
        f'<polyline points="{cov_pts}" fill="none" stroke="#2563eb" stroke-width="2" '
        f'stroke-dasharray="4 4"/>',
        '<circle cx="12" cy="14" r="4" fill="#1a6b3c"/><text x="22" y="18" '
        'fill="#444" font-size="11">accuracy at gate</text>',
        '<circle cx="12" cy="34" r="4" fill="#2563eb"/><text x="22" y="38" '
        'fill="#444" font-size="11">coverage at gate</text>',
        f'<text x="{x(0.475):.0f}" y="{height-18}" fill="#6b6b6b" font-size="10" '
        'text-anchor="middle">abstention gate (confidence ≥)</text>',
        "</svg>",
    ])


_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--ink:#0f131c;--paper:#f5f0e8;--muted:#6b6b6b;--rule:#d4cfc5;
--good:#1a6b3c;--warn:#b8860b;--bad:#c0392b;--blue:#2563eb}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--paper);color:#16181c;font:400 14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:0 0 48px}}
header{{background:var(--ink);color:var(--paper);padding:30px 44px 24px}}
header .fi{{font:600 11px/1 ui-monospace,Menlo,monospace;letter-spacing:.15em;text-transform:uppercase;color:var(--warn);margin-bottom:10px}}
header h1{{font-size:24px;font-weight:700;letter-spacing:-.3px}}
header p{{color:rgba(245,240,232,.7);max-width:720px;margin-top:8px}}
.badge{{display:inline-block;font:600 11px/1 ui-monospace,Menlo,monospace;letter-spacing:.08em;
text-transform:uppercase;padding:5px 10px;border-radius:4px;margin-top:12px}}
.badge.good{{background:rgba(26,107,60,.25);color:#7fd4a0}}
.badge.warn{{background:rgba(184,134,11,.25);color:#f0cf86}}
.badge.bad{{background:rgba(192,57,43,.25);color:#f4a49a}}
.badge.na{{background:rgba(255,255,255,.12);color:rgba(245,240,232,.8)}}
main{{max-width:820px;margin:0 auto;padding:0 24px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:22px 0}}
.card{{background:#fff;border:1px solid var(--rule);border-radius:8px;padding:14px 16px}}
.card .k{{font:600 10px/1 ui-monospace,Menlo,monospace;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}}
.card .v{{font-size:26px;font-weight:700;letter-spacing:-.5px;margin-top:6px}}
.card .s{{font-size:11px;color:var(--muted);margin-top:4px}}
.v{{text-align:left;font-size:13px;color:#666;margin:22px 0 8px;font-weight:600}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--rule);font-size:12.5px}}
th,td{{text-align:left;padding:8px 12px;border-bottom:1px solid var(--rule)}}
th{{font:600 10px/1 ui-monospace,Menlo,monospace;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}}
td.r{{font:14px ui-monospace,Menlo,monospace;text-align:right}}
.legend{{font-size:11.5px;color:var(--muted);margin-top:8px}}
.good{{color:var(--good)}}.warn{{color:var(--warn)}}.bad{{color:var(--bad)}}
footer{{text-align:center;color:var(--muted);font:400 11px/1.6 ui-monospace,Menlo,monospace;margin-top:34px}}
</style></head><body>
<header>
  <div class="fi">AgentCheck · Calibration report</div>
  <h1>{judge_line}</h1>
  <p>{dataset_line}</p>
  <span class="badge {read_class}">{read}</span>
</header>
<main>
  <div class="grid">
    <div class="card"><div class="k">Decided items</div><div class="v">{decided_n}</div>
      <div class="s">of {n} total · {coverage} coverage</div></div>
    <div class="card"><div class="k">Accuracy (decided)</div><div class="v">{accuracy}</div>
      <div class="s">over items at/above gate</div></div>
    <div class="card"><div class="k">ECE</div><div class="v {ece_class}">{ece}</div>
      <div class="s">expected calibration error</div></div>
    <div class="card"><div class="k">MCE</div><div class="v {mce_color}">{mce}</div>
      <div class="s">worst single bin gap</div></div>
    <div class="card"><div class="k">Brier</div><div class="v">{brier}</div>
      <div class="s">mean squared confidence error</div></div>
    <div class="card"><div class="k">Abstained</div><div class="v">{abstained_n}</div>
      <div class="s">{abstained_acc} accurate if forced · conf {abstained_conf}</div></div>
  </div>

  <div class="v">Reliability curve — decided items by confidence bin</div>
  <div class="card" style="background:#fff">{reliability_svg}</div>
  <p class="legend">Point = mean confidence vs observed accuracy in that bin.
  Green: |gap|≤0.05. Amber: ≤0.12. Red: overconfident/under-confident beyond 0.12.
  Radius scales with bin size. The dashed diagonal is perfect calibration.</p>

  <div class="v">Risk / coverage — what you gain by abstaining</div>
  <div class="card" style="background:#fff">{risk_svg}</div>
  <p class="legend">As the confidence gate rises, more items are routed to a human
  (lower coverage) but the ones acted on are more likely correct (rising accuracy).</p>

  <div class="v">Per-bin table (decided)</div>
  <table><thead><tr><th>confidence</th><th>n</th><th>mean conf</th><th>accuracy</th>
  <th style="text-align:right">gap</th></tr></thead><tbody>
  {table_rows}
  </tbody></table>

  <p class="legend">{footnote}</p>
</main>
<footer>AgentCheck · generated {generated} · ECE measured over decided items only; abstentions are reported, never scored.</footer>
</body></html>"""


def render_calibration_html(rep: dict, dataset: str = "", checkset: str = "") -> str:
    """Turn a calibration dict (from calibration.build_report) into HTML."""
    def r(cls, v):  # noqa: E731 - tiny local helpers
        return f'<td class="r {cls}">{v}</td>'

    decided = rep.get("decided", {})
    abst = rep.get("abstained", {})
    # The guard is not a stored value: if there are under 30 decided items,
    # the report must refuse to claim on the badge as loudly as in the footnote,
    # whatever the stored read says.
    read = rep.get("read")
    if decided.get("n", 0) < 30:
        read = "insufficient-data"
    read = read or "insufficient-data"
    read_class = {"well-calibrated": "good", "usable": "warn",
                  "miscalibrated": "bad"}.get(read, "na")
    ece = decided.get("ece")
    ece_class = "good" if ece is not None and ece <= 0.05 else (
        "warn" if ece is not None and ece <= 0.12 else "bad")
    mce = decided.get("mce")
    mce_color = "good" if mce is not None and mce <= 0.12 else "bad"

    table_rows = []
    for row in decided.get("reliability", []):
        if not row.get("n"):
            continue
        gap = row["gap"]
        gap_cls = "good" if abs(gap) <= 0.05 else ("warn" if abs(gap) <= 0.12 else "bad")
        table_rows.append(
            f'<tr><td class="r">{row["range"][0]:.2f}–{row["range"][1]:.2f}</td>'
            f'<td class="r">{row["n"]}</td>'
            f'<td class="r">{row["mean_confidence"]:.2f}</td>'
            f'<td class="r">{row["accuracy"]:.2f}</td>'
            f'{r(gap_cls, f"{gap:+.2f}")}</tr>')
    table = "\n".join(table_rows) or ("<tr><td colspan=5>no decided bins</td></tr>")

    n_total = rep.get("n", 0)
    reason = ("under 30 decided items: this number does not yet support a "
              "calibration claim either way." if decided.get("n", 0) < 30
              else "decided items above the abstention gate; abstentions are "
                   "reported separately and never scored against ECE.")
    labels = (f'{_esc(rep.get("judge", ""))} / {_esc(checkset or rep.get("checkset", ""))}'
              if rep.get("judge") else "no judge recorded")
    return _TEMPLATE.format(
        title=f"Calibration — {labels}",
        judge_line=f"{labels}",
        dataset_line=(f"Dataset: <b>{_esc(dataset or rep.get('dataset', '—'))}</b>"
                      if (dataset or rep.get("dataset")) else ""),
        read=_esc(read.replace("_", " ").replace("-", " ").upper()),
        read_class=read_class,
        decided_n=str(decided.get("n", "—")),
        n=str(n_total),
        coverage=_fmt(decided.get("coverage"), 3),
        accuracy=_fmt(decided.get("accuracy")),
        ece=_fmt(ece),
        ece_class=ece_class,
        mce=_fmt(mce),
        mce_color=mce_color,
        brier=_fmt(decided.get("brier")),
        abstained_n=str(abst.get("n", "—")),
        abstained_acc=_fmt(abst.get("accuracy_if_forced")),
        abstained_conf=_fmt(abst.get("mean_confidence")),
        reliability_svg=_reliability_svg(decided.get("reliability", [])),
        risk_svg=_risk_svg(rep.get("risk_coverage", [])),
        table_rows=table,
        footnote=reason,
        generated=time.strftime("%Y-%m-%d %H:%M"),
    )


def write_calibration_report(rep: dict, path: str, dataset: str = "",
                             checkset: str = "") -> str:
    """Render and write. Returns the path written."""
    from pathlib import Path
    p = Path(path)
    if p.suffix != ".html":
        p = p.with_suffix(p.suffix + ".html") if p.suffix else Path(str(p) + ".html")
    html_out = render_calibration_html(rep, dataset=dataset, checkset=checkset)
    p.write_text(html_out)
    return str(p)
