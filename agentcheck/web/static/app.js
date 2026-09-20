/* AgentCheck — the queue.
   One screen: ledger of checked calls, dock holding the selected strip. */

import { $, banner, esc, when, checkValue, verdictReason, checkRows,
         chipArea, collisionMessage, VERDICT, ASSESS, CHECK_COPY, GATE,
         TRUST_COLORS, DECISION_COLORS, openSheet, closeSheets } from "./util.js";
import { state } from "./state.js";
import { api } from "./api.js";
import { loadChecksets, updateRubricHint, loadRubrics } from "./views/rubrics.js";
import { loadTrust } from "./views/trust.js";
import { loadPolicies } from "./views/policies.js";
import { loadMonitor, loadDrift } from "./views/monitor.js";
import { loadRuns, openRun } from "./views/runs.js";
import { renderRedteamIdle, updateRedteamCost, runRedteam } from "./views/redteam.js";
import { visible, renderCounts, signoffStats, renderLedger } from "./ledger.js";
import { renderDock } from "./dock.js";
import { setLive, load, loadUsage } from "./queue.js";
import { renderExamples, runExample, runUpload } from "./sheets.js";
import { gateForKey, KEY_STORE } from "./keygate.js";


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


// What the user's own sign-outs have added up to. Reviewing used to change
// nothing visible, so the third step of the empty state had no payoff; this is
// the same human-agreement number the Trust view computes, shown where the
// reviewing happens.


/* ── dock ─────────────────────────────────────────────────────────── */


/* ── rubrics ──────────────────────────────────────────────────────── */


/* ── trust ────────────────────────────────────────────────────────── */


/* ── policies ─────────────────────────────────────────────────────── */


/* ── monitor ──────────────────────────────────────────────────────── */


/* ── runs (trace explorer) ────────────────────────────────────────── */


/* ── red team ─────────────────────────────────────────────────────── */


/* ── queue loading ────────────────────────────────────────────────── */


/* ── sheets ───────────────────────────────────────────────────────── */


/* examples */


/* upload */


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

// A hosted, non-demo instance does not hand out a key — the operator issues
// one. So: a key this tab already holds wins, then the local/demo bootstrap,
// and only then the gate. Without the gate the hosted UI showed "Is agentcheck
// serve running?" and offered no way to enter a key at all, which made every
// non-demo deployment API-only in the browser.
// Sessions: the UI learns sign-in. A signed-in tab with no key mints its
// first one from the session (POST /v1/me/keys), shows it once, and stores
// it like any other key. Raw fetch, not api(): the session endpoint refuses
// Bearer by design, and cookies ride along same-origin by default.
const CLAIMED_STORE = "agentcheck_claimed";

async function sessionMe() {
  try {
    const r = await fetch("/v1/auth/me", { credentials: "same-origin" });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

async function refreshAuthButton() {
  const btn = $("auth-btn");
  const me = await sessionMe();
  if (!me) { btn.hidden = true; return; }
  // Sign out needs only the session (logout is local); sign in needs a
  // configured provider. Where neither holds, there is no button.
  if (me.authenticated) {
    btn.hidden = false;
    btn.textContent = "Sign out";
    btn.onclick = async () => {
      try {
        await fetch("/v1/auth/logout",
                    { method: "POST", credentials: "same-origin" });
      } catch { /* the cookie is gone either way after reload */ }
      sessionStorage.removeItem(KEY_STORE);
      sessionStorage.removeItem(CLAIMED_STORE);
      location.reload();
    };
  } else if (me.login_configured) {
    btn.hidden = false;
    btn.textContent = "Sign in";
    btn.onclick = () => { location.href = "/v1/auth/login?next=/"; };
  } else {
    btn.hidden = true;
  }
}

async function claimFirstKey() {
  // One mint per tab: the endpoint caps at five per workspace, but a reload
  // with no stored key must not burn another on every boot.
  if (sessionStorage.getItem(CLAIMED_STORE)) return null;
  const me = await sessionMe();
  if (!me || !me.authenticated) return null;
  let k;
  try {
    const r = await fetch("/v1/me/keys", { method: "POST",
      credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "browser" }) });
    if (!r.ok) return null;
    k = await r.json();
  } catch { return null; }
  sessionStorage.setItem(KEY_STORE, k.key);
  sessionStorage.setItem(CLAIMED_STORE, "1");
  $("firstkey-value").value = k.key;
  $("firstkey-status").textContent = "";
  openSheet("firstkey");
  return k.key;
}

$("copy-firstkey").addEventListener("click", async () => {
  const v = $("firstkey-value").value;
  try {
    await navigator.clipboard.writeText(v);
    $("firstkey-status").textContent = "Copied.";
  } catch {
    $("firstkey-value").select();
    $("firstkey-status").textContent = "Copy it manually — it will not be shown again.";
  }
});

async function boot() {
  await refreshAuthButton();
  state.key = sessionStorage.getItem(KEY_STORE) || "";
  if (!state.key) {
    try {
      state.key = (await api("/v1/bootstrap")).key;
    } catch {
      state.key = "";
    }
    // No bootstrap key: a signed-in tab mints its first from the session;
    // anyone else gets the gate, as before.
    if (!state.key) state.key = (await claimFirstKey()) || "";
    if (!state.key) {
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
