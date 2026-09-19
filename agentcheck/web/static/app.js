/* AgentCheck — the queue.
   One screen: ledger of checked calls, dock holding the selected strip. */

import { $, banner, esc, when, checkValue, verdictReason, checkRows,
         chipArea, collisionMessage, VERDICT, ASSESS, CHECK_COPY, GATE,
         TRUST_COLORS, DECISION_COLORS } from "./util.js";
import { state } from "./state.js";
import { api } from "./api.js";


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

async function loadChecksets() {
  try {
    const d = await api("/v1/checksets");
    state.checksets = d.checksets || [];
    for (const sel of [$("upload-checkset"), $("rt-checkset")]) {
      const keep = sel.value;
      sel.innerHTML = state.checksets.map((c) =>
        `<option value="${esc(c.name)}">${esc(c.name)}` +
        `${c.builtin ? " (built-in)" : ""} · ${(c.checks || []).length} checks</option>`)
        .join("");
      if (keep && state.checksets.some((c) => c.name === keep)) sel.value = keep;
    }
    // Trust reads across all rubrics by default; a specific rubric narrows it.
    const tsel = $("trust-checkset");
    const tkeep = tsel.value;
    tsel.innerHTML = `<option value="">all rubrics</option>` + state.checksets.map((c) =>
      `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join("");
    if (tkeep && (!tkeep || state.checksets.some((c) => c.name === tkeep))) tsel.value = tkeep;
    state.rtCheckset = $("rt-checkset").value || "safety";
    await loadRedteamFamilies();
    updateRubricHint();
    updateRedteamCost();
  } catch {
    state.checksets = [];
  }
}

const currentRubric = (name) => state.checksets.find((c) => c.name === name) || null;

function updateRubricHint() {
  const c = currentRubric($("upload-checkset").value);
  $("upload-rubric-hint").textContent = c
    ? `${c.description || "No description."} — asks ${(c.checks || []).length} ` +
      `questions; verdict from "${c.verdict_check}".`
    : "";
}

async function loadRubrics() {
  if (!state.checksets.length) await loadChecksets();
  const box = $("rubric-list");
  if (!state.checksets.length) {
    box.innerHTML = `<p class="hint" style="padding:0 var(--gut)">No rubrics available.</p>`;
    return;
  }
  box.innerHTML = `<div style="padding:14px var(--gut) 40px">` + state.checksets.map((c) => {
    const crit = (q) => !q.criteria ? ""
      : Array.isArray(q.criteria) ? q.criteria.join(" < ")
      : Object.entries(q.criteria).map(([k, v]) => `${k}: ${v}`).join(" · ");
    return `
    <div class="rubric">
      <button type="button" data-rubric="${esc(c.name)}" aria-expanded="false">
        <strong>${esc(c.name)}</strong>
        <span class="pill">${c.builtin ? "built-in" : "yaml"}</span>
        <span class="meta">${(c.checks || []).length} checks · verdict ${esc(c.verdict_check || "-")}` +
        `${c.severity_check ? " · severity " + esc(c.severity_check) : ""}</span>
      </button>
      <div class="body" data-body="${esc(c.name)}" hidden>
        <p style="margin:0 0 8px">${esc(c.description || "No description.")}</p>
        ${(c.checks || []).map((q) => `
          <div class="q">
            <code>${esc(q.id)}${q.id === c.verdict_check ? " *" : ""}</code>
            <em>${esc(q.type)}</em>
            <span>${esc(q.instructions)}
              ${crit(q) ? `<br><span class="crit">${esc(crit(q))}</span>` : ""}</span>
          </div>`).join("")}
      </div>
    </div>`;
  }).join("") + `</div>`;
  box.querySelectorAll("[data-rubric]").forEach((b) => b.addEventListener("click", () => {
    const body = box.querySelector(`[data-body="${b.dataset.rubric}"]`);
    const open = !body.hidden;
    body.hidden = open;
    b.setAttribute("aria-expanded", String(!open));
  }));
}

/* ── trust ────────────────────────────────────────────────────────── */


async function loadTrust() {
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

/* ── policies ─────────────────────────────────────────────────────── */


async function loadPolicies() {
  const box = $("policies-out");
  box.innerHTML = `<p class="hint" style="padding:0 var(--gut)">Loading…</p>`;
  let d;
  try {
    d = await api("/v1/policies");
  } catch (e) {
    box.innerHTML = `<p class="hint danger" style="padding:0 var(--gut)">${esc(e.message)}</p>`;
    return;
  }
  const list = d.policies || [];
  if (!list.length) {
    box.innerHTML = `<p class="hint" style="padding:0 var(--gut)">No policies yet.
      Drop a YAML file in <code>policies/</code> — see the README for the schema.</p>`;
    return;
  }
  box.innerHTML = list.map((p) => {
    if (!p.ok) {
      return `<div class="card" data-v="fail" style="margin:0 var(--gut) 10px">
        <em>policy</em><h3>${esc(p.name)}</h3>
        <p class="hint">${esc(p.error || "invalid")}</p></div>`;
    }
    const rules = (p.rules || []).map((r) =>
      `<div class="bar-row" style="grid-template-columns:1fr 90px">
         <span><code>${esc(r.if)}</code></span>
         <span style="color:${DECISION_COLORS[r.then] || "inherit"}">${esc(r.then)}</span>
       </div>`).join("");
    return `<div class="card" data-v="pass" style="margin:0 var(--gut) 10px">
      <em>policy${p.rubric ? " · " + esc(p.rubric) : ""}</em>
      <h3>${esc(p.name)}</h3>
      <div class="bars">${rules}
        <div class="bar-row" style="grid-template-columns:1fr 90px">
          <span><code>default</code></span>
          <span style="color:${DECISION_COLORS[p.default] || "inherit"}">${esc(p.default)}</span>
        </div>
      </div>
      <p class="hint" style="margin-top:8px">Observe-and-recommend: send
        <code>"policy": "${esc(p.name)}"</code> with a check to get a
        <code>decision</code> back. Nothing is actioned automatically.</p>
    </div>`;
  }).join("");
}

/* ── monitor ──────────────────────────────────────────────────────── */

async function loadMonitor() {
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

async function loadDrift() {
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

/* ── runs (trace explorer) ────────────────────────────────────────── */

async function loadRuns() {
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

function renderRuns(d) {
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

async function openRun(traceId) {
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

function renderRunDetail(d) {
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

/* ── red team ─────────────────────────────────────────────────────── */

function renderRedteamIdle() {
  const box = $("redteam-out");
  if (!box.dataset.done) {
    box.innerHTML = `<p class="hint" style="padding:0 var(--gut)">Not run yet in this
      session.</p>`;
  }
  updateRedteamCost();
}

let rtCorpus = { total: 0, counts: {}, families: [], mechanics: [], industries: [] };

async function loadRedteamFamilies() {
  const sel = $("rt-families");
  try {
    rtCorpus = await api("/v1/redteam/families");
  } catch (e) { return; }
  const opt = (v, label) => `<option value="${v}">${esc(label)}</option>`;
  const groups = [
    ["", `Every family (${rtCorpus.total} attacks)`],
    [rtCorpus.mechanics.join(","), `Mechanics only (${rtCorpus.mechanics.reduce((n, f) => n + (rtCorpus.counts[f] || 0), 0)})`],
    [rtCorpus.industries.join(","), `Industries only (${rtCorpus.industries.reduce((n, f) => n + (rtCorpus.counts[f] || 0), 0)})`],
  ];
  const per = rtCorpus.families.map((f) => opt(f, `${f} (${rtCorpus.counts[f] || 0})`));
  sel.innerHTML = groups.map(([v, l]) => opt(v, l)).join("") +
    `<optgroup label="One family">${per.join("")}</optgroup>`;
  updateRedteamCost();
}

function updateRedteamCost() {
  const c = currentRubric($("rt-checkset").value) || currentRubric(state.rtCheckset);
  if (!c) { $("rt-cost").textContent = ""; return; }
  const sel = $("rt-families");
  const picked = sel && sel.value ? sel.value.split(",") : rtCorpus.families;
  const n = picked.reduce((sum, f) => sum + (rtCorpus.counts[f] || 0), 0) || rtCorpus.total;
  $("rt-cost").textContent =
    `${n} attacks x ${(c.checks || []).length} questions = ~${n * (c.checks || []).length} ` +
    `questions, charged against the monthly allowance.`;
}

async function runRedteam() {
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

function renderRedteam(d) {
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
