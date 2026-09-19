// The rubric catalogue: list, expand, and the picker hints.

import { api } from "../api.js";
import { $, esc } from "../util.js";
import { state } from "../state.js";

export async function loadChecksets() {
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
    updateRubricHint();
  } catch {
    state.checksets = [];
  }
}
export function updateRubricHint() {
  const c = currentRubric($("upload-checkset").value);
  $("upload-rubric-hint").textContent = c
    ? `${c.description || "No description."} — asks ${(c.checks || []).length} ` +
      `questions; verdict from "${c.verdict_check}".`
    : "";
}
export async function loadRubrics() {
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

// Which rubric a name refers to, or null. Used by the trust and redteam
// pickers as well, so it lives with the catalogue rather than either view.
export const currentRubric = (name) =>
  state.checksets.find((c) => c.name === name) || null;
