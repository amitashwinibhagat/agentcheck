// Queue chrome: the live stream, the usage pill, and the load loop.

import { api } from "./api.js";
import { $, esc, banner } from "./util.js";
import { state } from "./state.js";
import { renderCounts, renderLedger } from "./ledger.js";
import { renderDock } from "./dock.js";
import { gateForKey, KEY_STORE } from "./keygate.js";

export async function loadUsage() {
  try {
    const u = await api("/v1/usage");
    const a = u.allowance || {};
    const pill = $("usage-pill");
    if (a.monthly_allowance != null) {
      // Thousands separators: "138 / 40000" reads as a serial number, and this
      // is the one label that tells someone how much budget is left.
      const n = (v) => Number(v).toLocaleString();
      pill.textContent =
        `${n(a.used_this_month ?? 0)} / ${n(a.monthly_allowance)} this month`;
      pill.title = `Plan: ${u.plan || "free"} · resets ${a.resets_at ? new Date(a.resets_at * 1000).toLocaleDateString() : "monthly"}`;
      pill.classList.toggle("low", (a.remaining ?? 1) <= Math.max(a.monthly_allowance * 0.1, 5));
    }
    // Honesty about the judge. A keyless self-host runs the offline stub, and
    // someone who does not know that would judge the product by keyword
    // matching and leave convinced it is dumb. Say it plainly, once, with the
    // fix — a warning that names the problem and the command.
    if (u.judge === "stub") {
      banner("Judging with the offline stub — keyword-based, no model. " +
             "Set OPENAI_API_KEY (or TYPESAFE_API_KEY) and restart for real " +
             "judgments.", "info");
    }
  } catch { /* usage is informational; the queue still loads */ }
}
export function setLive(on) {
  state.liveOn = on;
  const btn = $("live-toggle");
  if (btn) {
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-pressed", String(on));
    btn.textContent = on ? "Live" : "Live off";
  }
  if (on && !state.liveSource) {
    state.liveSource = new EventSource(`/v1/stream?key=${encodeURIComponent(state.key)}`);
    state.liveSource.addEventListener("result", (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (!m.id || state.results.some((r) => r.id === m.id)) return;
        state.results.unshift({ id: m.id, ts: m.ts * 1000, request: m.request,
          tool: m.tool, args: {}, trace_verdict: m.verdict,
          confidence: m.confidence, severity: m.severity,
          decision: m.decision, checkset: m.checkset,
          assessment: null, duplicate: m.duplicate });
        state.counts.total += 1;
        if (m.verdict in state.counts) state.counts[m.verdict] += 1;
        renderCounts(); renderLedger();
      } catch { /* a malformed event must not break the stream */ }
    });
    state.liveSource.onerror = () => { /* EventSource auto-reconnects */ };
  } else if (!on && state.liveSource) {
    state.liveSource.close();
    state.liveSource = null;
  }
}
export async function load() {
  try {
    const data = await api("/v1/results");
    state.results = data.results || [];
    state.counts = data.counts || { pass: 0, review: 0, fail: 0, total: 0 };
    banner("");
  } catch (e) {
    if (e.status === 401) {
      // The key this tab holds is no longer valid: drop it and re-gate rather
      // than leaving a half-loaded queue on screen.
      sessionStorage.removeItem(KEY_STORE);
      state.key = "";
      gateForKey("That key was not recognised. Paste a valid one.");
      return;
    }
    banner("Could not reach the workspace: " + e.message);
    return;
  }
  renderCounts(); renderLedger(); loadUsage();
  // Always redraw the dock. It used to be skipped when nothing was selected,
  // which left its "select a call" prompt unreachable and the 424px panel
  // rendering blank on first load — reading as a broken page rather than an
  // empty one.
  state.selected = state.selected ? (state.results.find((r) => r.id === state.selected.id) || null) : null;
  renderDock();
}
