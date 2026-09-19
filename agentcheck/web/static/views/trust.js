// Trust view: the score, its components, its tier and sample size.

import { api } from "../api.js";
import { $, esc, TRUST_COLORS } from "../util.js";
import { state } from "../state.js";

export async function loadTrust() {
  const box = $("trust-out");
  box.innerHTML = `<p class="hint" style="padding:0 var(--gut)">Computing…</p>`;
  const cs = $("trust-checkset").value || "";
  let d;
  try {
    d = await api(`/v1/trust${cs ? `?checkset=${encodeURIComponent(cs)}` : ""}`);
  } catch (e) {
    box.innerHTML = `<p class="hint danger" style="padding:0 var(--gut)">${esc(e.message)}</p>`;
    return;
  }
  const color = TRUST_COLORS[d.verdict] || TRUST_COLORS["insufficient-data"];
  const comp = d.components || {};
  const fmt = (v) => (v == null ? "—" : (v * 100).toFixed(0) + "%");
  const compRows = [
    ["Confidence spread", comp.concentration,
     "How varied the judge's confidences are. A judge stuck at one value is not discriminating."],
    ["Verdict stability", comp.stability,
     "Does the verdict mix stay steady between the first and second half of the window?"],
    ["Human agreement", comp.human_agreement,
     "Share of signed-out assessments that agreed with the judge. Sparse by design."],
    ["Abstention rate", comp.abstention_rate,
     "Share below the confidence gate. Abstaining is correct behavior, not a penalty."],
  ].map(([label, v, hint]) => `
    <div class="bar-row">
      <span>${label}</span>
      <span class="stack" title="${esc(hint)}">
        <i class="s-pass" style="width:${v == null ? 0 : (v * 100).toFixed(0)}%"></i>
      </span>
      <span>${fmt(v)}</span>
    </div>`).join("");
  const tierNote = d.tier === "measured"
    ? `Measured against ${d.dataset ? "the " + d.dataset + " dataset" : "a labeled dataset"}: ECE ${d.ece?.toFixed(3)}, accuracy ${(d.accuracy * 100).toFixed(0)}%.`
    : `Consistency tier: no labels used. Publish a calibration (agentcheck calibrate --publish) to move to the measured tier.`;
  box.innerHTML = `<div style="padding:18px var(--gut) 40px">
    <div class="trust-hero">
      <div class="trust-score" style="color:${color}">${d.score}</div>
      <div>
        <span class="pill" style="color:${color}">${esc(d.verdict.replace("-", " "))}</span>
        <span class="note">tier: ${esc(d.tier)}</span>
        <span class="note">n=${d.n}</span>
        <span class="note">gate ${d.gate}</span>
      </div>
    </div>
    <p class="hint">${esc(tierNote)}</p>
    <div class="bars">${compRows}</div>
    <p class="hint" style="margin-top:14px">Badge for your README —
      <code>GET /v1/trust.svg</code> with your key renders it live.</p>
  </div>`;
}
