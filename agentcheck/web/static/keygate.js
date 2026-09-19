// The key gate: a hosted, non-demo instance does not hand out a key, so the
// browser asks for the one the operator issued.
//
// Its own module because two callers need it and one of them is queue.js --
// importing it back from app.js would make app.js and queue.js circular.

import { $, openSheet } from "./util.js";

export const KEY_STORE = "agentcheck_key";

export function gateForKey(why) {
  openSheet("key");
  $("key-status").textContent = why || "";
  $("key-input").focus();
}
