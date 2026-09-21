// Trust view: the score, its components, its tier and sample size.

import { api } from "../api.js";
import { $, banner, esc, openSheet, TRUST_COLORS } from "../util.js";
import { state } from "../state.js";
import { renderLedger } from "../ledger.js";
import { renderDock } from "../dock.js";

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
  // "thin" means the sample is too small to say anything: fewer than 10
  // judged calls, which is where the verdict itself becomes available. It has
  // to be computed before the copy that branches on it (a TDZ slip here
  // blanked the whole view).
  const thin = (d.n || 0) < 10;
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
  const tierNote = thin
    ? `No verdict yet — the score needs a sample before it means anything. ` +
      `Measuring the judge below needs no key and takes about a minute.`
    : d.tier === "measured"
      ? `Measured against ${d.dataset ? "the " + d.dataset + " dataset" : "a labeled dataset"}: ECE ${d.ece?.toFixed(3)}, accuracy ${(d.accuracy * 100).toFixed(0)}%.`
      : `Consistency tier: no labels used. "Measure this judge" below runs the shipped dataset and moves this to the measured tier.`;

  // Day one is a zero, and a zero on the differentiating feature reads as
  // failure rather than as "no sample yet". Frame it as progress and put the
  // two things that produce a real number one click away: judge something,
  // then calibrate against the shipped dataset.
  const emptyState = !thin ? "" : `
    <div class="empty" style="margin:6px var(--gut) 0">
      <h2>Not enough judgments yet</h2>
      <p>The score needs a sample — <strong>${d.n || 0} of 10</strong> judged calls
      for a verdict, and 30 decided items to reach the measured tier. A number
      without a sample is a horoscope, so this stays blank rather than guessing.</p>
      <ol class="checklist">
        <li><button type="button" class="btn" id="trust-sample">1 · Judge something</button>
          <span>Loads the shipped 3-row sample; ${(d.n || 0) === 0 ? "this is the fastest first row" : "adds 3 more rows"}.</span></li>
        <li><button type="button" class="btn ghost" id="trust-calibrate">2 · Measure the judge</button>
          <span>Runs the shipped <strong>agent-demo</strong> dataset (66 labeled traces,
          ~330 judgments) and shows real ECE and accuracy — the part no other tool ships.
          ${d.tier === "measured" ? "" : "This dataset is ours, not yours; re-run on your traces to make it yours."}</span></li>
      </ol>
      <div id="trust-cal-out"></div>
    </div>`;
  const measureButton = (thin || d.tier === "measured") ? "" : `
    <div class="rowline" style="padding:0 var(--gut);margin-top:10px">
      <button type="button" class="btn ghost" id="trust-calibrate">Measure this judge</button>
      <span class="note">Runs the shipped agent-demo dataset (~330 judgments) — ECE and accuracy, not a vibe.</span>
      <div id="trust-cal-out"></div>
    </div>`;

  // The labeling loop: the measured tier can be reached on the user's OWN
  // labels, and the only thing standing in the way is a count. Show the count.
  const so = d.signoffs || {};
  const soNeeded = so.needed || 30;
  const soDecided = so.decided || 0;
  const labelBlock = (thin || soDecided >= soNeeded) && d.tier === "measured" ? "" : `
    <div class="ro" style="margin:16px var(--gut)">
      <p class="kicker">Measured tier · your labels</p>
      <p>${soDecided} of ${soNeeded} decided sign-outs
      ${soDecided >= soNeeded
        ? "— enough to publish a calibration measured on your own traffic."
        : `— ${soNeeded - soDecided} more to reach the tier 
           <strong>on your traffic</strong> instead of the shipped dataset.`}</p>
      <div class="rowline">
        <button type="button" class="btn ghost" id="trust-label">Start labeling</button>
        <button type="button" class="btn" id="trust-publish"
                ${soDecided ? "" : "disabled"}>Publish from my ${soDecided} sign-outs</button>
        <span class="note" id="trust-pub-status">
          ${soDecided ? "No judge calls." : "Sign calls out first — a calibration with no labels is not a number."}
        </span>
      </div>
    </div>`;
  box.innerHTML = `<div style="padding:18px var(--gut) 40px">
    <div class="trust-hero">
      <div class="trust-score" style="color:${color}">${thin ? "—" : d.score}</div>
      <div>
        <span class="pill" style="color:${color}">${esc(d.verdict.replace("-", " "))}</span>
        <span class="note">tier: ${esc(d.tier)}</span>
        <span class="note">n=${d.n}</span>
        <span class="note">gate ${d.gate}</span>
      </div>
    </div>
    <p class="hint">${esc(tierNote)}</p>
    ${emptyState}
    ${thin ? "" : `<div class="bars">${compRows}</div>`}
    ${measureButton}
    ${labelBlock}
    <p class="hint" style="margin-top:14px">Badge for your README —
      <code>GET /v1/trust.svg</code> with your key renders it live.</p>
  </div>`;

  const sample = $("trust-sample");
  if (sample) sample.addEventListener("click", () => {
    openSheet("upload");
    const load = $("load-sample");
    if (load) load.click();
  });
  const cal = $("trust-calibrate");
  if (cal) cal.addEventListener("click", () => runDemoCalibration(cs));
  const label = $("trust-label");
  if (label) label.addEventListener("click", () => {
    // Jump to the queue and select the first call nobody has signed out.
    document.querySelector('[data-view="queue"]')?.click();
    const next = state.results.find((r) => !r.assessment);
    if (next) { state.selected = next; renderLedger(); renderDock(); }
    else banner("Every call in this log is signed out — nothing left to label.", "info");
  });
  const pub = $("trust-publish");
  if (pub) pub.addEventListener("click", () => publishSignoffs(pub, cs));
}

// Publish the sign-out calibration as the workspace's reliability model. Writes
// the same file the CLI does, so the tier moves either way — and says plainly
// that the trust number changes now while per-check corrections need a restart.
async function publishSignoffs(btn, checkset) {
  const status = $("trust-pub-status");
  btn.disabled = true;
  const was = btn.textContent;
  btn.textContent = "Publishing…";
  try {
    const r = await api(`/v1/calibration/signoffs/publish` +
                        (checkset ? `?checkset=${encodeURIComponent(checkset)}` : ""),
                        { method: "POST", body: JSON.stringify({}) });
    const rep = r.report || {};
    const dd = rep.decided || {};
    if (status) status.textContent =
      `Published: ECE ${Number(dd.ece ?? 0).toFixed(3)}, accuracy ` +
      `${(Number(dd.accuracy ?? 0) * 100).toFixed(0)}% over ${dd.n ?? 0} of your sign-outs. ` +
      `${r.note || ""}`;
    await loadTrust();
  } catch (e) {
    if (status) status.textContent = e.message;
    btn.disabled = false;
    btn.textContent = was;
  }
}

// Run the calibration the product ships with, from the browser. This is the
// only one-click path to the measured tier, and the endpoint already existed —
// it was CLI-only, so the differentiating feature was invisible on day one.
async function runDemoCalibration(checkset) {
  const out = $("trust-cal-out");
  const btn = $("trust-calibrate");
  if (!out) return;
  if (btn) { btn.disabled = true; btn.textContent = "Calibrating…"; }
  out.innerHTML = `<p class="hint">Running 66 labeled traces through the judge…</p>`;
  let r;
  try {
    r = await api(`/v1/calibration?dataset=agent-demo` +
                  (checkset ? `&checkset=${encodeURIComponent(checkset)}` : ""));
  } catch (e) {
    out.innerHTML = `<p class="hint danger">${esc(e.message)}</p>`;
    if (btn) { btn.disabled = false; btn.textContent = "Measure this judge"; }
    return;
  }
  const dd = r.decided || {};
  out.innerHTML = `
    <div class="ro">
      <p class="kicker">Calibration · ${esc(r.dataset || "agent-demo")} · judge ${esc(r.judge || "")}</p>
      <p><strong>ECE ${Number(dd.ece ?? 0).toFixed(3)}</strong> · accuracy
      ${(Number(dd.accuracy ?? 0) * 100).toFixed(0)}% · n=${dd.n ?? 0} decided ·
      ${esc(r.read || "")}</p>
      <p class="hint">Expected calibration error measures how far confidence sits
      from measured accuracy. ${esc(r.dataset || "agent-demo")} is the shipped
      dataset — run it on your own labeled traces to make the number yours.</p>
    </div>`;
  if (btn) { btn.disabled = false; btn.textContent = "Measure this judge"; }
}
