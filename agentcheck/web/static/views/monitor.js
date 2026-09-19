// Trust Report: verdict mix per bucket, volume, drift.

import { api } from "../api.js";
import { $, esc } from "../util.js";
import { state } from "../state.js";

export async function loadMonitor() {
  const box = $("monitor-out");
  box.innerHTML = `<p class="hint" style="padding:0 var(--gut)">Loading…</p>`;
  let tl;
  try {
    tl = await api(`/v1/monitor?bucket=${state.bucket}&days=90`);
  } catch (e) {
    box.innerHTML = `<p class="hint danger" style="padding:0 var(--gut)">${esc(e.message)}</p>`;
    return;
  }
  if (!tl.total) {
    box.innerHTML = `<p class="hint" style="padding:0 var(--gut)">No judgments in the window.
      Score some traces first.</p>`;
    return;
  }
  const rows = tl.buckets.slice(-40).reverse().map((b) => {
    const pct = (v) => (b.n ? (v / b.n) * 100 : 0);
    return `<div class="bar-row">
      <span>${esc(b.bucket)}</span>
      <span class="stack" title="${b.fail} flagged · ${b.review} review · ${b.pass} pass">
        <i class="s-fail" style="width:${pct(b.fail)}%"></i>
        <i class="s-rev" style="width:${pct(b.review)}%"></i>
        <i class="s-pass" style="width:${pct(b.pass)}%"></i>
      </span>
      <span>${b.n}</span>
      <span>${(b.flagged_rate * 100).toFixed(0)}%</span>
      <span>${b.mean_confidence == null ? "—" : b.mean_confidence.toFixed(2)}</span>
      <span>${b.signed_out}</span>
    </div>`;
  }).join("");
  box.innerHTML = `<div style="padding:18px var(--gut) 40px">
    <p class="hint">${tl.total} judgments across ${tl.buckets.length} ${esc(state.bucket)} buckets</p>
    <div class="bars">
      <div class="bar-row head"><span>bucket</span><span>mix</span><span>n</span>
        <span>flagged</span><span>conf</span><span>signed out</span></div>
      ${rows}
    </div>
    <div id="drift-out"></div>
    <p class="hint" style="margin-top:18px">Each bar is that bucket's verdict mix:
    red flagged, amber needs a look, grey fine.</p>
  </div>`;
}
export async function loadDrift() {
  const box = $("drift-out");
  if (!box) return;
  box.innerHTML = `<p class="hint">Comparing…</p>`;
  let d;
  try {
    d = await api(`/v1/monitor/drift?bucket=${state.bucket}&days=90`);
  } catch (e) {
    box.innerHTML = `<p class="hint danger">${esc(e.message)}</p>`;
    return;
  }
  if (!d.ok) {
    box.innerHTML = `<div class="callout">${esc(d.reason)}</div>`;
    return;
  }
  const ch = d.change;
  const tone = Math.abs(ch.flagged_rate) >= 0.15 ? "warn" : "good";
  const sign = (v) => (v >= 0 ? "+" : "");
  box.innerHTML = `<div class="callout ${tone}">
    <strong>Drift</strong> — earlier ${d.early.n} judgments vs later ${d.late.n}.
    Flagged ${sign(ch.flagged_rate)}${(ch.flagged_rate * 100).toFixed(1)}pp,
    reviewed ${sign(ch.review_rate)}${(ch.review_rate * 100).toFixed(1)}pp,
    mean confidence ${sign(ch.mean_confidence)}${ch.mean_confidence.toFixed(3)}.
    ${d.notes.map((n) => `<br>! ${esc(n)}`).join("")}
  </div>`;
}
