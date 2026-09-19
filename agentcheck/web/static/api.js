/* The one way this client talks to the API.

   Every call carries the tab's key and every failure arrives as an Error with
   a `status`, so callers can branch on 401/402/502 without parsing text. The
   204 and empty-body cases are handled here rather than at each call site.
*/

import { state } from "./state.js";

export async function api(path, opts = {}) {
  const headers = { Authorization: `Bearer ${state.key}`, ...(opts.headers || {}) };
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail || res.statusText;
    const e = new Error(typeof d === "string" ? d : JSON.stringify(d));
    e.status = res.status;
    throw e;
  }
  return data;
}
