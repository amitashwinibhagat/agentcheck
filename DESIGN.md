# Design

<!-- impeccable:design-schema 1 -->

Written from the built queue at `agentcheck/web/static`. The surface brief is
`.impeccable/surfaces/agentcheck-web.md`; the approved comp is
`.impeccable/mocks/comp-b.png`.

## World

**Flight Strip Board.** Every checked call is a paper strip laid in one ledger;
the selected strip is pulled and held in a dock. The world is a physical
records board, not a dashboard: warm charcoal ground, cream card stock, hairline
rules, and machine-printed data.

It refuses the category default for this product — navy glass cards, a violet
accent, a gradient headline — which the brief was written against.

## Palette

Hue is spent on exactly one thing: verdict. Nothing decorative is coloured.

| Token | Value | Use |
|---|---|---|
| `--board` | `#14120f` | page ground, warm near-black |
| `--board2` | `#1d1a16` | dock, sheets |
| `--card` | `#f0eade` | strip and paper stock |
| `--cardline` | `#cfc5ae` | rules on stock |
| `--ink` / `--ink2` | `#16140f` / `#6f6a5c` | type on stock |
| `--text` / `--text-dim` | `#e8e2d4` / `#9b9483` | type on board |
| `--rule` / `--rule2` | `#2e2a23` / `#3a352c` | hairlines on board |
| `--flag` | `#a63a24` | flagged band, bar, pill |
| `--review` | `#8a5a12` | needs-a-look band, bar, pill |
| `--pass` | `#4a4741` | neutral graphite — a pass is the absence of a warning |
| `--focus` | `#e8b45a` | focus ring |

Contrast: the checked pairs (card kicker 5.80, "run this check" 4.75, white on
blue 5.80, review pill 5.96) clear WCAG AA for normal text, verified in-browser
against composited backgrounds rather than against token values alone.

## Type

- **Archivo** — interface, headings, labels. Weights 400/500/600/700.
- **Spline Sans Mono** — tool signs, arguments, timestamps, numerals. Tabular.

Numerals are the loudest element on the surface: queue counts at 52px/.82 with
`-.055em` tracking. The measured cap height from the comp is 39.7px, which is
where that scale came from.

`font-match.mjs` recorded a catalog nearest-face (Aleo, a slab) for the numeral
region because no browser was resolvable from the CLI to fingerprint the crop.
**Stated deviation:** the build keeps the comp's face (Spline Sans Mono) rather
than the fallback, because the approved comp is the artifact being audited. The
measured cap height was taken.

Legibility floor (from a detector pass over the static UI, 2026-09-17):
functional and interactive text is at least **11px**, body text at least
**12px**. The queue count labels, sheet field labels, kickers, row review
states, and status pills all sit at or above the floor. Density is the genre,
not an excuse to drop below legibility.

## Structure

```
shell: grid 1fr 424px / auto 1fr
  head    mark + counts (4 buttons, doubling as the verdict filter) | search + 3 actions
  ledger  rows: band(4px) sign(108) what(1fr) track(128) numeral(62) state(74) kill(26)
  dock    paper card: verdict pill, reason, dl, mono args, checks, sign-off
sheets: upload · examples · connect
```

**The strip** is the unit: a 4px verdict band on the left edge, the tool sign in
uppercase mono with its area beneath, the request with a mono metadata line, a
confidence track in the verdict hue, the confidence as a tabular numeral, and a
review state on the right. `data-v` on the row drives the band and bar hue, so
verdict is one attribute, not three conditionals.

## Motion

One gesture: selecting a strip pulls it into the dock. On narrow viewports the
dock slides in over the ledger (`translateX`, 220ms
`cubic-bezier(.25,1,.5,1)`) and the ledger's own scroll position is preserved.
`prefers-reduced-motion` removes the transition.

## States

| State | Treatment |
|---|---|
| Empty | Ledger is a statement of the job plus two doors: score traces, or show an example |
| Loading | `.load` line; the ledger keeps its previous rows rather than blanking |
| Judge failed | Banner: "The check service is unavailable. That is not a pass." |
| Low confidence | Dashed ghost field in the dock and a `Needs a look` row state at `< 0.6` |
| Abstained check | Dashed ghost row rather than a blank or a zero |
| Upload error | Per-row, counted as `error`; it never becomes a pass and never sinks the run |
| Filtered to nothing | "Nothing matches that filter" with a clear action |

Callouts (drift, ASR, evaded attack) carry severity as a **defined edge** — a
full 1px border in the severity hue — not a coloured left rail. The left band is
the paper strip's status device on the ledger; borrowing it onto dark callout
panels read as the generic AI accent rail, so the treatments are kept separate.

## Responsive

Below 1080px the grid collapses to one column and the dock becomes a pushed
detail view with a back control. The confidence track and review state drop from
the row first; the band, sign, request and numeral survive. Verified at 390px
with no horizontal overflow.

## Open

The hero gate for the approved comp stands at **75%** (`gate.ok: false`) with four
region contradictions. Three are **stated decisions**, recorded in the surface
brief rather than hidden, because closing them would mean removing real
function to satisfy a pixel diff:

| Region | Score | Why it stands |
|---|---|---|
| `filter` | 54% | The count buttons *are* the verdict filter; the comp's separate segmented control was dropped as redundant under the triage-first spine |
| `signout` | 52% | Three assessments, not two — the product needs an explicit abstention and `/v1/results` accepts `insufficient_context` |
| `dock-args` | 54% | Geometry now within ~20px (build 420–508 vs comp 400–470); the residue is the extra definition-list row |
| `mark` | 48% | Not reachable: live verdict counts put an amber numeral in a box the comp filled white. Closing it means falsifying data |

The final diff was re-run at the comp's own 1600×1000. An earlier run compared a
1440×950 capture against the 1600×1000 comp — mismatched aspect ratios, so its
numbers were unreadable and were discarded.

`font-match.mjs` recorded a catalog nearest-face (Aleo, a slab) for the numeral
region because no browser was resolvable from the CLI to fingerprint the crop.
**Stated deviation:** the build keeps the comp's face (Spline Sans Mono) rather
than the fallback, because the approved comp is the artifact being audited. The
measured cap height (39.7px) was taken.

The mechanical detector ran **degraded** (regex fallback, no `htmlparser2`), so
its empty result is an undercount and not a clean bill of health.
