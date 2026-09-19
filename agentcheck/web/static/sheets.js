// The three sheets: upload, examples, connect.

import { api } from "./api.js";
import { $, esc, banner, openSheet, closeSheets, VERDICT, collisionMessage } from "./util.js";
import { state } from "./state.js";
import { load, loadUsage } from "./queue.js";
import { renderLedger } from "./ledger.js";
import { renderDock } from "./dock.js";

export function renderExamples() {
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
export async function runExample(id) {
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
export function parseDataset(text) {
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
export async function runUpload() {
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
