// The ledger: one row per checked call, and the counts above it.

import { $, esc, when, GATE, ASSESS, openSheet, chipArea } from "./util.js";
import { state } from "./state.js";

export function visible() {
  const q = state.search.trim().toLowerCase();
  return state.results.filter((r) => {
    if (state.filter !== "all" && r.trace_verdict !== state.filter) return false;
    if (!q) return true;
    return `${r.request} ${r.tool} ${JSON.stringify(r.args)}`.toLowerCase().includes(q);
  });
}
export function renderCounts() {
  $("c-total").textContent = state.counts.total ?? 0;
  $("c-fail").textContent = state.counts.fail ?? 0;
  $("c-review").textContent = state.counts.review ?? 0;
  $("c-pass").textContent = state.counts.pass ?? 0;
}
export function signoffStats() {
  const assessed = state.results.filter((r) => r.assessment);
  const agreed = assessed.filter((r) => r.assessment === "looks_correct").length;
  const missed = assessed.filter((r) => r.assessment === "actual_issue").length;
  return { assessed: assessed.length, agreed, missed, decided: agreed + missed };
}
export function renderLedger() {
  const box = $("rows");
  if (!state.counts.total) {
    $("hintline").textContent = "";
    const signed = (state.counts.assessed ?? 0) > 0;
    const batched = (state.counts.batches ?? 0) > 0;
    box.innerHTML = `
      <div class="empty">
        <h2>No calls checked yet</h2>
        <p>Three steps to value. Nothing is blocked automatically — this is a review log.</p>
        <ol class="checklist">
          <li><button type="button" class="btn ghost" data-door="examples">1 · See a check</button>
            <span>Run one example and read its verdict.</span></li>
          <li><button type="button" class="btn" data-door="upload">2 · Score your traces</button>
            <span>Paste JSON or upload a file${batched ? " — done" : ""}.</span></li>
          <li><span class="step">3 · Sign one out</span>
            <span>Open a result and mark it right, a miss, or unclear${signed ? " — done" : ""}.</span></li>
        </ol>
      </div>`;
    box.querySelectorAll("[data-door]").forEach((b) => b.addEventListener("click", () => openSheet(b.dataset.door)));
    return;
  }
  const rows = visible();
  const s = signoffStats();
  const tail = s.assessed
    ? ` · ${s.assessed} signed out`
      + (s.decided ? `, judge agreed on ${Math.round((s.agreed / s.decided) * 100)}%` : "")
    : "";
  $("hintline").textContent = (rows.length === state.results.length
    ? `${state.results.length} checked call${state.results.length === 1 ? "" : "s"} · newest first`
    : `${rows.length} of ${state.results.length} shown`) + tail;
  if (!rows.length) {
    box.innerHTML = `<div class="empty"><p>Nothing matches that filter. <button type="button" class="btn ghost" data-clear>Clear filters</button></p></div>`;
    box.querySelector("[data-clear]").addEventListener("click", () => {
      state.filter = "all"; state.search = ""; $("search").value = "";
      document.querySelectorAll(".count").forEach((c) => c.classList.toggle("on", c.dataset.filter === "all"));
      renderLedger();
    });
    return;
  }
  box.innerHTML = rows.map((r) => {
    const conf = r.confidence == null ? null : Number(r.confidence);
    const state = r.assessment ? ASSESS[r.assessment] : (conf != null && conf < GATE ? "Needs a look" : "Unread");
    const out = r.assessment === "looks_correct" || r.assessment === "insufficient_context";
    const confTxt = conf == null ? "—" : conf.toFixed(2);
    const bar = conf == null ? 0 : Math.round(conf * 100);
    const meta = [
      when(r.ts),
      r.run_id ? "upload " + r.run_id.replace("run_", "#").slice(0, 6) : null,
      r.trace_id ? "run " + String(r.trace_id).replace(/^tr_/, "").slice(0, 6) : null,
      typeof r.args === "object" ? JSON.stringify(r.args).slice(0, 90) : null,
    ].filter(Boolean).join(" · ");
    return `
      <button type="button" class="row" data-id="${r.id}" data-v="${r.trace_verdict || "review"}"
              aria-selected="${state.selected?.id === r.id}">
        <span class="band" aria-hidden="true"></span>
        <span class="sign">${esc(r.tool)}<i>${esc(chipArea(r))}</i></span>
        <span class="what"><b>${esc(r.request)}</b><small>${esc(meta)}</small></span>
        <span class="track" title="Confidence ${confTxt}"><i style="width:${bar}%"></i></span>
        <span class="trust">${confTxt}</span>
        <span class="state ${out ? "out" : ""}">${esc(state)}</span>
        <span class="kill" data-kill="${r.id}" role="button" tabindex="-1" title="Delete">✕</span>
      </button>`;
  }).join("");
}
