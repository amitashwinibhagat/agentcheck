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
      <span class="note" id="label-keys">press 1 · 2 · 3</span>
    </div>
    ${labelHud()}
    <button type="button" class="btn ghost back" id="dock-back">← Back to queue</button>`;
  box.querySelectorAll("[data-assess]").forEach((b) =>
    b.addEventListener("click", () => assess(r.id, b.dataset.assess)));
  $("dock-back")?.addEventListener("click", () => $("dock").classList.remove("open"));
  if (r.trace_id) loadRunStrip(r.trace_id, r.id);
}

//: The guided-labeling strip: where you are in the batch, and the number you
//: are building. A measured tier costs 30 sign-outs, and a counter that only
//: appears at the end gives nobody a reason to reach it — so the ECE so far is
//: shown from the first label, and labeled honestly as unreadable until there
//: are enough decided items to mean anything.
function labelHud() {
  const b = state.labelBatch;
  const run = state.labelRun;
  if (!b && !run) return "";
  const parts = [];
  if (b?.ids?.length) {
    const done = b.ids.filter((id) =>
      state.results.find((x) => x.id === id)?.assessment).length;
    parts.push(`Labeling batch <strong>${done} of ${b.ids.length}</strong>`);
    const cov = b.coverage || {};
    const vs = Object.entries(cov.verdicts || {})
      .map(([v, n]) => `${n} ${v}`).join(", ");
    if (vs) parts.push(`covers ${vs}`);
    if ((cov.tools || []).length) parts.push(`${cov.tools.length} tools`);
    const bands = Object.keys(cov.bands || {}).length;
    if (bands) parts.push(`${bands} confidence band${bands === 1 ? "" : "s"}`);
  }
  if (run) {
    if (run.decided >= 5 && run.ece != null) {
      // "total" on purpose: the calibration covers every label in the
      // workspace, which after a first session is more than this batch.
      parts.push(`ECE <strong>${Number(run.ece).toFixed(3)}</strong> over `
        + `${run.decided} total labels`
        + (run.accuracy != null
            ? ` (accuracy ${(run.accuracy * 100).toFixed(0)}% vs confidence `
              + `${((run.mean_confidence ?? 0) * 100).toFixed(0)}%)`
            : "")
        + ` — ${run.read}`);
    } else {
      parts.push(`${run.decided} decided label${run.decided === 1 ? "" : "s"}`
        + ` — too few to read a calibration yet`);
    }
    if (run.remaining > 0) {
      parts.push(`${run.remaining} more to a measured tier`);
    } else {
      parts.push("enough for a measured tier — publish it in Trust");
    }
  }
  return `<p class="note" id="label-hud">${parts.join(" · ")}</p>`;
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
    const r = await api(`/v1/results/${id}`, {
      method: "PATCH", body: JSON.stringify({ assessment }) });
    const row = state.results.find((x) => x.id === id);
    if (row) row.assessment = assessment;
    if (state.selected?.id === id) state.selected.assessment = assessment;
    // The response carries the calibration so far: the running ECE is the
    // progress bar for the 30 labels a measured tier costs.
    if (r && r.calibration) state.labelRun = r.calibration;
    // Advance to the next call in the batch (or the next unlabeled one when
    // there is no batch). Stopping after each label made 30 items a chore.
    advanceToUnassessed(id);
    renderDock(); renderLedger();
  } catch (e) { banner("Could not save your assessment: " + e.message); }
}

//: Which row to move to after a sign-out. In guided mode it is the next
//: unlabeled item of the batch — that plan is stratified, and it is the reason
//: the user is here. Otherwise the next unlabeled row, preferring one below
//: the current so the eye keeps its place.
function advanceToUnassessed(fromId) {
  const batch = state.labelBatch;
  const unlabeled = (rid) => {
    const r = state.results.find((x) => x.id === rid);
    return r && !r.assessment;
  };
  if (batch?.ids?.length) {
    for (let i = state.labelAt + 1; i < batch.ids.length; i++) {
      if (unlabeled(batch.ids[i])) {
        state.labelAt = i;
        state.selected = state.results.find((x) => x.id === batch.ids[i]);
        return;
      }
    }
    for (let i = 0; i <= state.labelAt; i++) {
      if (unlabeled(batch.ids[i])) {
        state.labelAt = i;
        state.selected = state.results.find((x) => x.id === batch.ids[i]);
        return;
      }
    }
    // Batch finished: fall through to the rest of the log.
  }
  const rows = state.results;
  const i = rows.findIndex((x) => x.id === fromId);
  const after = i >= 0 ? rows.slice(i + 1).find((x) => !x.assessment) : null;
  const next = after || rows.find((x) => !x.assessment);
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
