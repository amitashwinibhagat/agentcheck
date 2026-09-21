/* The one piece of shared mutable state.

   app.js was a single script where every function could see every variable by
   closing over the module scope. Split across files that stops working: ES
   module imports are read-only bindings, so a module cannot reassign another
   module's `let`. One exported object keeps that possible without accessor
   ceremony — mutate `state.x`, never rebind `state` itself.

   Kept deliberately flat and small. If this grows a method, the thing it
   describes wants to be its own module instead.
*/

export const state = {
  // identity for this tab (see the key gate in app.js)
  key: "",
  // the verdict log
  results: [],
  counts: { pass: 0, review: 0, fail: 0, total: 0 },
  selected: null,
  filter: "all",
  search: "",
  // guided labeling: a stratified batch of unlabeled calls (see labeling.py)
  labelBatch: null,   // {ids, coverage, items}
  labelAt: -1,        // index within labelBatch.ids
  labelRun: null,     // the running calibration from the last sign-out
  // rubric catalogue and the example door
  examples: [],
  checksets: [],
  // per-view selections, so switching away and back keeps your place
  bucket: "day",
  rtCheckset: "safety",
  rtCorpus: { total: 0, counts: {}, families: [], mechanics: [], industries: [] },
  trustTab: "score",
  runFilter: "all",
  selectedRun: null,
  lastRuns: { runs: [] },
  // live stream
  liveSource: null,
  liveOn: false,
  // per-trace step lists, so the dock does not refetch on every selection
  runCache: {},
};
