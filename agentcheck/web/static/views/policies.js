// Policies view: each policy's ordered rules and what each returns.

import { api } from "../api.js";
import { $, esc, DECISION_COLORS } from "../util.js";

export async function loadPolicies() {
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
