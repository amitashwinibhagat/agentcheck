// The dock: the selected strip as paper, with its checks and sign-out.

import { api } from "./api.js";
import { $, esc, when, banner, checkValue, verdictReason, checkRows,
         ASSESS, CHECK_COPY, GATE, VERDICT } from "./util.js";
import { state } from "./state.js";
import { renderLedger, signoffStats } from "./ledger.js";

export function renderDock() {
  const box = $("dock-in");
  if (!state.selected) {
    box.innerHTML = `<div class="empty-dock">
      <p class="kicker" style="margin-bottom:10px">Nothing in hand</p>
      <p>Select a call from the ledger to read its checks and sign it out.<br><br>
      Nothing is executed and nothing is blocked.</p>
    </div>`;
    return;
  }
  const r = state.selected;
  const v = r.trace_verdict || "review";
  const meta = VERDICT[v] || VERDICT.review;
  const conf = r.confidence == null ? null : Number(r.confidence);
  const low = conf != null && conf < GATE;
  const sev = checkValue(r, "severity");
  const severity = sev ? `<div class="chk"><span>${CHECK_COPY.severity}</span><b>${sev.v.toFixed(1)} / 3</b></div>`
    : `<div class="ghost-row"><span>Severity — not scored</span><span>—</span></div>`;
  box.innerHTML = `
    <p class="kicker">In hand · ${esc(r.run_id ? "upload " + r.run_id.replace("run_", "#").slice(0, 7) : "single check")}${r.model ? " · " + esc(r.model) : ""}</p>
    <div class="paper">
      <span class="verdict ${v}">${esc(meta.stamp)}</span>
      <h3>${esc(meta.title)}</h3>
      <p class="reason">${esc(verdictReason(r))}</p>
      <dl>
        <dt>User asked</dt><dd>${esc(r.request)}</dd>
        <dt>Agent chose</dt><dd>${esc(r.tool)}</dd>
        <dt>When</dt><dd>${esc(when(r.ts))}</dd>
      </dl>
      <div class="mono">${esc(JSON.stringify(r.args ?? {}, null, 2))}</div>
      <div class="checks">
        ${checkRows(r)}
        ${severity}
      </div>
    </div>
    ${low ? `<div class="ghost-row"><span>Low confidence — treat as needs a look, not a decision</span><span>${conf.toFixed(2)}</span></div>` : ""}
    <div id="runstrip"></div>
    <p class="note" id="assess-note">${(() => {
      const s = signoffStats();
      const tally = s.decided
        ? ` ${s.assessed} signed out; the judge agreed on ${Math.round((s.agreed / s.decided) * 100)}%.`
        : (s.assessed ? ` ${s.assessed} signed out.` : "");
      return r.assessment
        ? `Saved: ${ASSESS[r.assessment]}. The machine verdict is unchanged.${tally}`
        : `Your assessment is stored separately from the machine verdict.`;
    })()}</p>
    <div class="signoff" role="group" aria-label="Your assessment">
      <button type="button" class="btn" data-assess="looks_correct" aria-pressed="${r.assessment === "looks_correct"}">Judgment is right</button>
      <button type="button" class="btn ghost" data-assess="actual_issue" aria-pressed="${r.assessment === "actual_issue"}">Missed something</button>
      <button type="button" class="btn ghost" data-assess="insufficient_context" aria-pressed="${r.assessment === "insufficient_context"}">Not enough context</button>
    </div>
    <button type="button" class="btn ghost back" id="dock-back">← Back to queue</button>`;
  box.querySelectorAll("[data-assess]").forEach((b) =>
    b.addEventListener("click", () => assess(r.id, b.dataset.assess)));
  $("dock-back")?.addEventListener("click", () => $("dock").classList.remove("open"));
  if (r.trace_id) loadRunStrip(r.trace_id, r.id);
}
export async function loadRunStrip(traceId, currentId) {
  const slot = document.getElementById("runstrip");
  if (!slot) return;
  try {
    if (!state.runCache[traceId]) {
      const d = await api(`/v1/traces/${encodeURIComponent(traceId)}`);
      state.runCache[traceId] = d.steps || [];
    }
    const steps = state.runCache[traceId];
    if (!steps.length) { slot.innerHTML = ""; return; }
    slot.innerHTML = `<p class="kicker">Run · ${steps.length} step${steps.length === 1 ? "" : "s"} oldest first</p>
      <div class="runstrip">${steps.map((s, i) =>
        `<button type="button" class="rdot v-${s.trace_verdict || "review"}${s.id === currentId ? " here" : ""}" data-step="${s.id}" title="${i + 1} · ${esc(s.tool)} · ${s.trace_verdict || "review"}">${i + 1}</button>`).join("")}</div>`;
    slot.querySelectorAll("[data-step]").forEach((b) => b.addEventListener("click", () => {
      const found = state.results.find((x) => x.id === b.dataset.step);
      if (found) { state.selected = found; renderLedger(); renderDock(); }
    }));
  } catch (e) { /* unknown trace or offline: the dock still stands */ slot.innerHTML = ""; }
}
export async function assess(id, assessment) {
  try {
    await api(`/v1/results/${id}`, { method: "PATCH", body: JSON.stringify({ assessment }) });
    const row = state.results.find((x) => x.id === id);
    if (row) row.assessment = assessment;
    if (state.selected?.id === id) state.selected.assessment = assessment;
    // Advance to the next call nobody has signed out. Labeling 30 items one at
    // a time is the whole path to a measured tier, and stopping after each one
    // made it a chore; the next unlabeled row is the only useful place to be.
    advanceToUnassessed(id);
    renderDock(); renderLedger();
  } catch (e) { banner("Could not save your assessment: " + e.message); }
}

//: Which row to move to after a sign-out: the next one without an assessment,
//: preferring rows below the current one so the eye keeps its place.
function advanceToUnassessed(fromId) {
  const rows = state.results;
  const i = rows.findIndex((x) => x.id === fromId);
  if (i < 0) return;
  const after = rows.slice(i + 1).find((x) => !x.assessment);
  const any = rows.find((x) => !x.assessment);
  const next = after || any;
  if (next) state.selected = next;
}

//: Labeling without the mouse: 1/2/3 sign out the selection and advance.
//: The dock's three buttons are the same three answers, so a person can label
//: a queue at reading speed instead of aiming at 30 targets.
export function bindAssessKeys() {
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = (document.activeElement?.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (document.querySelector(".sheet:not([hidden])")) return;
    const map = { "1": "looks_correct", "2": "actual_issue", "3": "insufficient_context" };
    const assessment = map[e.key];
    if (!assessment || !state.selected) return;
    e.preventDefault();
    assess(state.selected.id, assessment);
  });
}
