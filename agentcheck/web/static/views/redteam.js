// Trust Under Attack: run the adversarial suite, price it, show evasions.

import { api } from "../api.js";
import { $, esc, collisionMessage } from "../util.js";
import { state } from "../state.js";
import { currentRubric } from "./rubrics.js";
import { loadUsage } from "../queue.js";

export function renderRedteamIdle() {
  // The picker is priced from the corpus, so make sure it is loaded even if
  // this view opens before the queue finished booting.
  if (!state.rtCorpus.total) loadRedteamFamilies();
  const box = $("redteam-out");
  if (!box.dataset.done) {
    box.innerHTML = `<p class="hint" style="padding:0 var(--gut)">Not run yet in this
      session.</p>`;
  }
  updateRedteamCost();
}
export async function loadRedteamFamilies() {
  const sel = $("rt-families");
  try {
    state.rtCorpus = await api("/v1/redteam/families");
  } catch (e) { return; }
  const opt = (v, label) => `<option value="${v}">${esc(label)}</option>`;
  const groups = [
    ["", `Every family (${state.rtCorpus.total} attacks)`],
    [state.rtCorpus.mechanics.join(","), `Mechanics only (${state.rtCorpus.mechanics.reduce((n, f) => n + (state.rtCorpus.counts[f] || 0), 0)})`],
    [state.rtCorpus.industries.join(","), `Industries only (${state.rtCorpus.industries.reduce((n, f) => n + (state.rtCorpus.counts[f] || 0), 0)})`],
  ];
  const per = state.rtCorpus.families.map((f) => opt(f, `${f} (${state.rtCorpus.counts[f] || 0})`));
  sel.innerHTML = groups.map(([v, l]) => opt(v, l)).join("") +
    `<optgroup label="One family">${per.join("")}</optgroup>`;
  updateRedteamCost();
}
export function updateRedteamCost() {
  const c = currentRubric($("rt-checkset").value) || currentRubric(state.rtCheckset);
  if (!c) { $("rt-cost").textContent = ""; return; }
  const sel = $("rt-families");
  const picked = sel && sel.value ? sel.value.split(",") : state.rtCorpus.families;
  const n = picked.reduce((sum, f) => sum + (state.rtCorpus.counts[f] || 0), 0) || state.rtCorpus.total;
  $("rt-cost").textContent =
    `${n} attacks x ${(c.checks || []).length} questions = ~${n * (c.checks || []).length} ` +
    `questions, charged against the monthly allowance.`;
}
export async function runRedteam() {
  const sel = $("rt-checkset").value;
  const fams = ($("rt-families") || {}).value || "";
  const btn = $("run-redteam");
  btn.disabled = true;
  $("rt-status").textContent = "Running attacks…";
  $("redteam-out").innerHTML =
    `<p class="hint" style="padding:0 var(--gut)">Running…</p>`;
  try {
    const q = `/v1/redteam?checkset=${encodeURIComponent(sel)}` +
      (fams ? `&families=${encodeURIComponent(fams)}` : "");
    const d = await api(q, { method: "POST" });
    $("rt-status").textContent = "";
    renderRedteam(d);
  } catch (e) {
    $("rt-status").textContent = "";
    const msg = collisionMessage(e) || e.message;
    $("redteam-out").innerHTML =
      `<p class="hint danger" style="padding:0 var(--gut)">${esc(msg)}</p>`;
  } finally {
    btn.disabled = false;
    loadUsage();
  }
}
export function renderRedteam(d) {
  const box = $("redteam-out");
  box.dataset.done = "1";
  const w = (v) => (v * 100).toFixed(0);
  const tone = d.asr === 0 ? "good" : d.asr > 0.25 ? "bad" : "warn";
  const fams = Object.entries(d.by_family)
    .sort((a, b) => (b[1].evaded / b[1].n) - (a[1].evaded / a[1].n));
  box.innerHTML = `<div style="padding:18px var(--gut) 40px">
    <div class="callout ${tone}">
      <strong>Attack success rate ${w(d.asr)}%</strong> — ${d.evaded.length} of ${d.n}
      attacks came back <em>pass</em> under the "${esc(d.checkset)}" rubric,
      judge <code>${esc(d.judge)}</code>.
      ${d.asr === 0
        ? "Nothing slipped through."
        : "Each one below is a call a good rubric should have flagged."}
    </div>
    <div class="metric-row head"><span>family</span><span>asr</span><span>evaded</span><span>attacks</span></div>
    ${fams.map(([f, s]) => {
      const a = s.evaded / s.n;
      return `<div class="metric-row">
        <span>${esc(f)}</span>
        <span class="spark"><i style="width:${w(a)}%;background:${
          a === 0 ? "#4a7a5c" : a > 0.5 ? "var(--flag)" : "var(--review)"}"></i></span>
        <span>${s.evaded}</span>
        <span>${s.n}</span>
      </div>`;
    }).join("")}
    ${d.evaded.length ? `<h3 style="margin:24px 0 8px;font-size:13px">Evaded attacks</h3>
      ${d.evaded.map((a) => `<div class="callout bad">
        <code>${esc(a.id)}</code> — verdict <strong>${esc(a.verdict)}</strong> at confidence
        ${a.confidence == null ? "—" : a.confidence.toFixed(2)}<br>
        <span class="hint">${esc(a.note)}</span><br>
        <span class="crit">request: ${esc(a.trace.request)}<br>tool: ${esc(a.trace.tool)}</span>
      </div>`).join("")}` : ""}
  </div>`;
}
