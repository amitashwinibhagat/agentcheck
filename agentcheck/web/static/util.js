/* Shared formatting, DOM and copy helpers.

   Extracted from app.js, which had grown one 1,100-line script. These are the
   pure ones: no shared app state, so any module can import them and a test can
   reason about them without a document. The verdict copy lives here rather
   than beside the code that renders it, so the wording cannot drift between
   the ledger row and the dock. */

export const $ = (id) => document.getElementById(id);

export const VERDICT = {
  pass:   { stamp: "Looks fine",             title: "This call matches the request." },
  review: { stamp: "Needs a look",           title: "Plausible, but not obviously right." },
  fail:   { stamp: "Should not have happened", title: "This does not match what the user asked." },
};
export const ASSESS = {
  looks_correct: "Signed off",
  actual_issue: "Flagged as a miss",
  insufficient_context: "Held for context",
};
export const CHECK_COPY = {
  args_accomplish_request: "The tool does what was asked",
  high_risk: "High-impact action",
  args_match_schema: "Arguments well-formed",
  severity: "Severity",
};
export const GATE = 0.6;

export const TRUST_COLORS = {
  trusted: "#3f6b3a", usable: "#8a5a12",
  "low-trust": "#a63a24", "insufficient-data": "#4a4741",
};

export const DECISION_COLORS = {approve: "#3f6b3a", human: "#8a5a12", block: "#a63a24"};

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const when = (ts) => ts ? new Date(ts * 1000).toLocaleString([], {
  month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";

export function banner(msg, kind = "error") {
  const b = $("banner");
  if (!msg) { b.hidden = true; return; }
  b.hidden = false;
  b.textContent = msg;
  b.style.borderColor = kind === "error" ? "var(--flag)" : "var(--rule2)";
  b.style.color = kind === "error" ? "#f0b3a5" : "var(--text-dim)";
}

export function checkValue(r, id) {
  const c = r.checks?.[id];
  if (!c) return null;
  return { v: Number(c.value), conf: Number(c.confidence), type: c.type };
}

export function verdictReason(r) {
  const v = r.trace_verdict || "review";
  const match = checkValue(r, "args_accomplish_request");
  const risk = checkValue(r, "high_risk");
  if (v === "fail" && match && match.v < 0.5) {
    return `The user asked for “${r.request}”, but the agent chose ${r.tool}. That is the mismatch.`;
  }
  if (risk && risk.v >= 0.5) {
    return `${r.tool} looks like a high-impact action for “${r.request}”.`;
  }
  if (v === "review") return "The tool is plausible for this request, but not obviously the right one. A person should decide.";
  if (v === "pass") return "The tool is a reasonable way to do what the user asked, and nothing looks high-impact.";
  return "The agent did something other than what the user asked.";
}

export function checkRows(r) {
  return ["args_accomplish_request", "high_risk", "args_match_schema"]
    .map((id) => {
      const c = checkValue(r, id);
      if (!c) return "";
      let label = "", cls = "";
      if (id === "high_risk") { label = c.v >= 0.5 ? "Flagged" : "Clear"; cls = c.v >= 0.5 ? "no" : "ok"; }
      else if (id === "args_accomplish_request") { label = c.v >= 0.5 ? "Yes" : "No"; cls = c.v >= 0.5 ? "ok" : "no"; }
      else { label = c.v >= 0.5 ? "Yes" : "No"; cls = c.v >= 0.5 ? "ok" : "no"; }
      return `<div class="chk"><span>${CHECK_COPY[id]}</span><b class="${cls}">${label} · ${c.v.toFixed(2)}</b></div>`;
    }).join("");
}

export function chipArea(r) {
  const t = String(r.tool || "");
  if (/^sql_|_sql|sql_/.test(t) || /sql/.test(t)) return "Data";
  if (/mail|inbox/.test(t)) return "Email";
  if (/slack|chat|post/.test(t)) return "Chat";
  if (/http|browser|web/.test(t)) return "Web";
  if (/event|calendar/.test(t)) return "Calendar";
  if (/pay|transfer|charge|invoice/.test(t)) return "Money";
  if (/doc|admin|share|grant/.test(t)) return "Access";
  if (/shell|exec/.test(t)) return "Shell";
  if (/file|read|write|copy|move|list/.test(t)) return "Files";
  return "Other";
}

export function collisionMessage(e) {
  if (e.status === 402) return e.message + " Your queue and history are untouched.";
  if (e.status === 502) return "The check service is unavailable. That is not a pass.";
  return null;
}

export function openSheet(which) {
  const el = $("sheet-" + which);
  if (!el) return;
  el.hidden = false;
  el.querySelector("textarea,input,button")?.focus?.();
}

export function closeSheets() { document.querySelectorAll(".sheet").forEach((s) => (s.hidden = true)); }
