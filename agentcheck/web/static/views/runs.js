// Runs: the trace explorer -- runs list, and one run's steps in order.

import { api } from "../api.js";
import { $, esc, when } from "../util.js";
import { state } from "../state.js";

export async function loadRuns() {
  const box = $("run-list");
  if (!box) return;
  try {
    const d = await api("/v1/runs" + (state.runFilter === "all" ? "" : `?only=${state.runFilter}`));
    state.lastRuns = d;
    renderRuns(d);
    if (!state.selectedRun || !(d.runs || []).some((r) => r.trace_id === state.selectedRun)) {
      state.selectedRun = null;
      $("run-detail").innerHTML =
        `<div class="empty-dock"><p class="kicker" style="margin-bottom:10px">Steps</p>
         <p>Select a run to see its steps, in order, with the verdict, confidence,
         decision and policy for each.</p></div>`;
    }
  } catch (e) {
    box.innerHTML = `<p class="hint danger" style="padding:0 var(--gut)">${esc(e.message)}</p>`;
  }
}
export function renderRuns(d) {
  const box = $("run-list");
  const runs = d.runs || [];
  $("runs-status").textContent = runs.length
    ? `${runs.length} run${runs.length === 1 ? "" : "s"}`
      + (d.blocked ? ` · ${d.blocked} with a block` : "")
    : "";
  if (!runs.length) {
    box.innerHTML = `<div class="empty"><h2>No runs yet</h2>
      <p>A run appears once an agent makes several checked calls under one trace id —
      pass <code>trace_id</code> on <code>/v1/check</code>, or let the OpenAI or LangChain
      wrapper stamp it for you.</p></div>`;
    $("run-detail").innerHTML = "";
    return;
  }
  box.innerHTML = runs.map((r) => {
    const tools = r.tools || [];
    const uniq = [...new Set(tools)];
    const path = uniq.slice(0, 3).join(" → ") + (uniq.length > 3 ? ` +${uniq.length - 3}` : "");
    const state = r.blocked ? "blocked" : (r.review ? "review" : "clear");
    return `<button type="button" class="row runrow" data-run="${esc(r.trace_id)}"
        aria-selected="${state.selectedRun === r.trace_id}">
      <span class="band ${r.blocked ? "flag" : (r.review ? "rev" : "")}" aria-hidden="true"></span>
      <span class="sign">${r.step_count}<i>step${r.step_count === 1 ? "" : "s"}</i></span>
      <span class="what"><b>${esc(path)}</b>
        <small>${esc(when(r.started))} · ${Number(r.duration_ms).toFixed(0)} ms</small></span>
      <span class="state ${r.blocked || r.review ? "out" : ""}">${esc(state)}</span>
    </button>`;
  }).join("");
}
export async function openRun(traceId) {
  state.selectedRun = traceId;
  renderRuns(state.lastRuns);
  const det = $("run-detail");
  det.innerHTML = `<p class="hint" style="padding:12px">Loading…</p>`;
  try {
    renderRunDetail(await api(`/v1/traces/${encodeURIComponent(traceId)}`));
  } catch (e) {
    det.innerHTML = `<p class="hint danger" style="padding:12px">${esc(e.message)}</p>`;
  }
}
export function renderRunDetail(d) {
  const steps = d.steps || [];
  $("run-detail").innerHTML = `
    <p class="kicker">Run ${esc(String(d.trace_id).replace(/^tr_/, ""))} ·
      ${steps.length} step${steps.length === 1 ? "" : "s"}${d.blocked ? " · blocked" : ""}</p>
    <div class="paper">
      ${steps.map((s, i) => {
        const v = s.trace_verdict || "review";
        const conf = s.confidence == null ? "—" : Number(s.confidence).toFixed(2);
        return `<div class="steprow">
          <span class="stepno v-${esc(v)}">${i + 1}</span>
          <div class="stepbody">
            <div class="stephead">
              <b>${esc(s.tool)}</b>
              <span class="verdict ${esc(v)}">${esc(v)}</span>
              <span class="pill">conf ${conf}</span>
              ${s.decision ? `<span class="pill dec-${esc(s.decision)}">${esc(s.decision)}</span>` : ""}
              ${s.policy ? `<span class="pill">${esc(s.policy)}</span>` : ""}
              ${(s.screen && s.screen.rule) ? `<span class="pill">screen: ${esc(s.screen.rule)}</span>` : ""}
            </div>
            <p class="reason">${esc(s.request || "")}</p>
            <div class="mono">${esc(JSON.stringify(s.args ?? {}, null, 2))}</div>
          </div>
        </div>`;
      }).join("")}
    </div>`;
}
