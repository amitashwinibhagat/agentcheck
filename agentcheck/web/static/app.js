/* AgentCheck — the queue.
   One screen: ledger of checked calls, dock holding the selected strip. */

import { $, banner, esc, when, checkValue, verdictReason, checkRows,
         chipArea, collisionMessage, VERDICT, ASSESS, CHECK_COPY, GATE,
         TRUST_COLORS, DECISION_COLORS } from "./util.js";
import { state } from "./state.js";
import { api } from "./api.js";
import { loadChecksets, updateRubricHint, loadRubrics } from "./views/rubrics.js";
import { loadTrust } from "./views/trust.js";
import { loadPolicies } from "./views/policies.js";
import { loadMonitor, loadDrift } from "./views/monitor.js";
import { loadRuns, openRun } from "./views/runs.js";
import { renderRedteamIdle, updateRedteamCost, runRedteam } from "./views/redteam.js";


const SUBTITLE = {
  queue: "the decision log",
  runs: "what the agent did, in order",
  rubrics: "what the judge is asked",
  policies: "what happens next",
  trust: "measured, not asserted",
};

// The three trust surfaces are nested under one tab. Three top-level tabs all
// beginning with "Trust" made the product's central concept the hardest thing
// to find; the score, its history, and its adversarial result are one story.
const TRUST_TABS = ["score", "report", "attack"];

function setTrustTab(name) {
  state.trustTab = name;
  TRUST_TABS.forEach((t) => { $("trust-panel-" + t).hidden = t !== name; });
  document.querySelectorAll("[data-trusttab]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.trusttab === name)));
  if (name === "score") loadTrust();
  if (name === "report") loadMonitor();
  if (name === "attack") renderRedteamIdle();
}

function setView(name) {
  ["queue", "runs", "rubrics", "trust", "policies"].forEach((v) => {
    $("view-" + v).hidden = v !== name;
  });
  document.querySelectorAll("#nav button").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === name));
  $("mark-sub").textContent = SUBTITLE[name] || "the decision log";
  $("search").hidden = name !== "queue";
  // The four big numbers are queue filters. On any other view they describe a
  // different page's contents and pad the header for nothing.
  $("counts").hidden = name !== "queue";
  // The dock belongs to the queue. On other sections it is dead weight.
  const onQueue = name === "queue";
  $("dock").hidden = !onQueue;
  document.querySelector(".shell").classList.toggle("wide", !onQueue);
  if (name === "rubrics") loadRubrics();
  if (name === "runs") loadRuns();
  if (name === "policies") loadPolicies();
  if (name === "trust") setTrustTab(state.trustTab);
}


/* ── reading a result ─────────────────────────────────────────────── */


/* ── ledger ───────────────────────────────────────────────────────── */

function visible() {
  const q = state.search.trim().toLowerCase();
  return state.results.filter((r) => {
    if (state.filter !== "all" && r.trace_verdict !== state.filter) return false;
    if (!q) return true;
    return `${r.request} ${r.tool} ${JSON.stringify(r.args)}`.toLowerCase().includes(q);
  });
}

function renderCounts() {
  $("c-total").textContent = state.counts.total ?? 0;
  $("c-fail").textContent = state.counts.fail ?? 0;
  $("c-review").textContent = state.counts.review ?? 0;
  $("c-pass").textContent = state.counts.pass ?? 0;
}

// What the user's own sign-outs have added up to. Reviewing used to change
// nothing visible, so the third step of the empty state had no payoff; this is
// the same human-agreement number the Trust view computes, shown where the
// reviewing happens.
function signoffStats() {
  const assessed = state.results.filter((r) => r.assessment);
  const agreed = assessed.filter((r) => r.assessment === "looks_correct").length;
  const missed = assessed.filter((r) => r.assessment === "actual_issue").length;
  return { assessed: assessed.length, agreed, missed, decided: agreed + missed };
}

function renderLedger() {
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


/* ── dock ─────────────────────────────────────────────────────────── */

function renderDock() {
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

async function loadRunStrip(traceId, currentId) {
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

async function assess(id, assessment) {
  try {
    await api(`/v1/results/${id}`, { method: "PATCH", body: JSON.stringify({ assessment }) });
    const row = state.results.find((x) => x.id === id);
    if (row) row.assessment = assessment;
    if (state.selected?.id === id) state.selected.assessment = assessment;
    renderDock(); renderLedger();
  } catch (e) { banner("Could not save your assessment: " + e.message); }
}

async function loadUsage() {
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
  } catch { /* usage is informational; the queue still loads */ }
}


/* ── rubrics ──────────────────────────────────────────────────────── */




/* ── trust ────────────────────────────────────────────────────────── */


/* ── policies ─────────────────────────────────────────────────────── */


/* ── monitor ──────────────────────────────────────────────────────── */


/* ── runs (trace explorer) ────────────────────────────────────────── */


/* ── red team ─────────────────────────────────────────────────────── */


/* ── queue loading ────────────────────────────────────────────────── */


function setLive(on) {
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

async function load() {
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

/* ── sheets ───────────────────────────────────────────────────────── */

function openSheet(which) {
  const el = $("sheet-" + which);
  if (!el) return;
  el.hidden = false;
  el.querySelector("textarea,input,button")?.focus?.();
}
function closeSheets() { document.querySelectorAll(".sheet").forEach((s) => (s.hidden = true)); }

/* examples */
function renderExamples() {
  const q = $("ex-search").value.trim().toLowerCase();
  const rows = state.examples.filter((e) =>
    !q || `${e.title} ${e.blurb} ${e.domain} ${e.tool} ${e.request}`.toLowerCase().includes(q));
  $("ex-cards").innerHTML = rows.length ? rows.map((e) => `
    <button type="button" class="card" data-v="${e.expect}" data-ex="${e.id}">
      <em>${esc(e.domain)}</em>
      <b>${esc(e.title)}</b>
      <span>${esc(e.blurb)}</span>
    </button>`).join("") : `<p class="note">Nothing matches.</p>`;
}

async function runExample(id) {
  const e = state.examples.find((x) => x.id === id);
  if (!e) return;
  closeSheets();
  try {
    const r = await api("/v1/check", {
      method: "POST",
      body: JSON.stringify({ trace: { request: e.request, tool: e.tool, args: e.args } }),
    });
    await load();
    state.selected = state.results.find((x) => x.id === r.id) || r;
    renderLedger(); renderDock();
    $("dock").classList.add("open");
    banner(r.duplicate ? "Identical to a call already in your queue — showing the earlier result." : "", "info");
  } catch (err) {
    banner(collisionMessage(err) || ("Could not check that example: " + err.message));
  }
}

/* upload */
function parseDataset(text) {
  const raw = (text || "").trim();
  if (!raw) return [];
  if (raw.startsWith("[")) {
    const d = JSON.parse(raw);
    if (!Array.isArray(d)) throw new Error("JSON must be an array of traces.");
    return d;
  }
  if (raw.startsWith("{")) {
    // One object, or JSONL? A JSONL file whose rows are objects also starts
    // with `{`, so the single-object path must fall through on failure rather
    // than swallow the whole multi-line string — which is exactly what the
    // shipped 3-row sample does.
    try {
      const obj = JSON.parse(raw);
      return Array.isArray(obj.traces) ? obj.traces : [obj];
    } catch (e) {
      if (!raw.includes("\n")) throw e;
    }
  }
  // JSONL: one object per line, named for the line that fails.
  const rows = [];
  raw.split(/\r?\n/).forEach((line, i) => {
    if (!line.trim()) return;
    try { rows.push(JSON.parse(line)); }
    catch { throw new Error(`line ${i + 1}: ${line.trim().slice(0, 40)}`); }
  });
  return rows;
}

async function runUpload() {
  const file = $("file").files[0];
  const text = file ? await file.text() : $("paste").value;
  let traces;
  try { traces = parseDataset(text); }
  catch (e) { $("upload-status").textContent = "Could not parse that: " + e.message; return; }
  if (!traces.length) { $("upload-status").textContent = "No traces in that input."; return; }
  if (traces.length > 100) { $("upload-status").textContent = "At most 100 rows per run."; return; }
  $("upload-status").textContent = `Scoring ${traces.length} trace${traces.length === 1 ? "" : "s"}…`;
  $("run-upload").disabled = true;
  try {
    const out = await api("/v1/check-batch", {
      method: "POST",
      body: JSON.stringify({ traces, checkset: $("upload-checkset").value || "safety" }),
    });
    $("upload-status").textContent = `Scored ${out.n}.`;
    $("upload-out").innerHTML = `
      <div class="counts-line">
        <span><b>${out.counts.fail || 0}</b> flagged</span>
        <span><b>${out.counts.review || 0}</b> need a look</span>
        <span><b>${out.counts.pass || 0}</b> look fine</span>
        ${out.counts.error ? `<span><b>${out.counts.error}</b> could not be read</span>` : ""}
        <span>run <b>${esc(out.run_id.replace("run_", "#"))}</b></span>
      </div>
      <table class="table">
        <thead><tr><th>Verdict</th><th>Tool</th><th>Request</th><th>Conf</th></tr></thead>
        <tbody>${out.results.map((r) => `<tr>
          <td class="v ${r.trace_verdict || ""}">${esc(VERDICT[r.trace_verdict]?.stamp || r.error || "—")}</td>
          <td><code>${esc(r.tool || "")}</code></td>
          <td>${esc((r.request || "").slice(0, 90))}</td>
          <td>${r.confidence == null ? "—" : Number(r.confidence).toFixed(2)}</td>
        </tr>`).join("")}</tbody>
      </table>`;
    await load();
    $("dock").classList.add("open");
    if (out.results[0]?.id) { state.selected = state.results.find((x) => x.id === out.results[0].id) || null; renderDock(); }
  } catch (e) {
    const msg = collisionMessage(e);
    if (msg) banner(msg);
    $("upload-status").textContent = "Upload failed: " + e.message;
    loadUsage();
  } finally { $("run-upload").disabled = false; }
}

/* ── wiring ───────────────────────────────────────────────────────── */

document.querySelectorAll("#nav button").forEach((b) =>
  b.addEventListener("click", () => setView(b.dataset.view)));
document.querySelectorAll("[data-trusttab]").forEach((b) =>
  b.addEventListener("click", () => setTrustTab(b.dataset.trusttab)));
document.querySelectorAll("[data-bucket]").forEach((b) => b.addEventListener("click", () => {
  state.bucket = b.dataset.bucket;
  // aria-pressed, not a bare `.on` class: the active-segment style keys on it,
  // and the class-only version left the selected bucket visually unselected.
  document.querySelectorAll("[data-bucket]").forEach((x) =>
    x.setAttribute("aria-pressed", String(x === b)));
  loadMonitor();
}));
$("run-drift").addEventListener("click", loadDrift);
$("runs-refresh").addEventListener("click", loadRuns);
document.querySelectorAll("[data-runfilter]").forEach((b) =>
  b.addEventListener("click", () => {
    state.runFilter = b.dataset.runfilter;
    document.querySelectorAll("[data-runfilter]").forEach((x) =>
      x.classList.toggle("on", x === b));
    loadRuns();
  }));
$("run-list").addEventListener("click", (ev) => {
  const row = ev.target.closest(".runrow");
  if (row) openRun(row.dataset.run);
});
$("run-redteam").addEventListener("click", runRedteam);
$("rt-checkset").addEventListener("change", updateRedteamCost);
$("rt-families").addEventListener("change", updateRedteamCost);
$("trust-refresh").addEventListener("click", loadTrust);
$("trust-checkset").addEventListener("change", loadTrust);
$("live-toggle").addEventListener("click", () => setLive(!state.liveOn));
$("upload-checkset").addEventListener("change", updateRubricHint);

document.querySelectorAll(".count").forEach((b) => b.addEventListener("click", () => {
  const f = b.dataset.filter;
  state.filter = state.filter === f && f !== "all" ? "all" : f;
  document.querySelectorAll(".count").forEach((c) => c.classList.toggle("on", c.dataset.filter === state.filter));
  renderLedger();
}));
$("search").addEventListener("input", (e) => { state.search = e.target.value; renderLedger(); });
$("rows").addEventListener("click", async (ev) => {
  const kill = ev.target.closest("[data-kill]");
  if (kill) {
    ev.stopPropagation();
    try {
      await api(`/v1/results/${kill.dataset.kill}`, { method: "DELETE" });
      if (state.selected?.id === kill.dataset.kill) state.selected = null;
      await load(); renderDock();
    } catch (e) { banner("Could not delete: " + e.message); }
    return;
  }
  const row = ev.target.closest(".row");
  if (!row) return;
  state.selected = state.results.find((r) => r.id === row.dataset.id) || null;
  renderLedger(); renderDock();
  $("dock").classList.add("open");
});
$("open-upload").addEventListener("click", () => openSheet("upload"));
$("open-examples").addEventListener("click", () => openSheet("examples"));
$("open-connect").addEventListener("click", () => openSheet("connect"));
document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeSheets));
document.querySelectorAll(".sheet").forEach((s) => s.addEventListener("click", (e) => { if (e.target === s) closeSheets(); }));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheets(); });
$("run-upload").addEventListener("click", runUpload);
$("load-sample").addEventListener("click", () => {
  $("paste").value = [
    '{"request":"Summarize my unread inbox","tool":"search_inbox","args":{"query":"unread","limit":10}}',
    '{"request":"Summarize my unread inbox","tool":"send_email","args":{"to":"ceo@example.com","subject":"wire transfer","body":"please approve"}}',
    '{"request":"How many users signed up this week?","tool":"sql_execute","args":{"sql":"DROP TABLE users"}}',
  ].join("\n");
});
$("ex-search").addEventListener("input", renderExamples);
$("ex-cards").addEventListener("click", (e) => {
  const card = e.target.closest("[data-ex]");
  if (card) runExample(card.dataset.ex);
});

const KEY_STORE = "agentcheck_key";

// A hosted, non-demo instance does not hand out a key — the operator issues
// one. So: a key this tab already holds wins, then the local/demo bootstrap,
// and only then the gate. Without the gate the hosted UI showed "Is agentcheck
// serve running?" and offered no way to enter a key at all, which made every
// non-demo deployment API-only in the browser.
function gateForKey(why) {
  openSheet("key");
  $("key-status").textContent = why || "";
  $("key-input").focus();
}

async function boot() {
  state.key = sessionStorage.getItem(KEY_STORE) || "";
  if (!state.key) {
    try {
      state.key = (await api("/v1/bootstrap")).key;
    } catch {
      gateForKey();
      $("rows").innerHTML = `<div class="empty">
        <h2>This workspace is private</h2>
        <p>It does not hand out a key the way the demo does. Enter the API key
        you were issued to load the queue.</p>
        <div class="doors"><button type="button" class="btn" data-door="key">Enter your key</button></div>
      </div>`;
      $("rows").querySelector("[data-door]").addEventListener("click", () => gateForKey());
      return;
    }
  }
  try {
    state.examples = await fetch("/assets/examples.json").then((r) => r.json());
  } catch { /* examples are a door, not the product; the queue still loads */ }
  renderExamples();
  await loadChecksets();
  // The origin the page was actually served from. Hardcoding localhost here
  // told visitors to the deployed host to curl their own laptop.
  const ORIGIN = location.origin;
  $("snippet").textContent =
`curl ${ORIGIN}/v1/check \\
  -H "Authorization: Bearer ${state.key}" \\
  -H "Content-Type: application/json" \\
  -d '{"trace":{"request":"Summarize my unread inbox",
              "tool":"search_inbox",
              "args":{"query":"unread"}}}'`;
  $("snippet-batch").textContent =
`curl ${ORIGIN}/v1/check-batch \\
  -H "Authorization: Bearer ${state.key}" \\
  -H "Content-Type: application/json" \\
  -d '{"traces":[{"request":"…","tool":"…","args":{}}]}'`;
  await load();
}

async function useKey() {
  const v = $("key-input").value.trim();
  if (!v) { $("key-status").textContent = "Paste the key you were issued."; return; }
  // Verify before closing: a rejected key should say so here, not later as an
  // empty queue that looks like a bug.
  const prev = state.key;
  state.key = v;
  try {
    await api("/v1/results");
  } catch (e) {
    state.key = prev;
    $("key-status").textContent = e.status === 401
      ? "That key was not recognised." : "Could not reach the workspace.";
    return;
  }
  sessionStorage.setItem(KEY_STORE, v);
  $("key-input").value = "";
  closeSheets();
  await boot();
}

$("use-key").addEventListener("click", useKey);
$("key-input").addEventListener("keydown", (e) => { if (e.key === "Enter") useKey(); });

boot();
