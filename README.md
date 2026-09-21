<h1 align="center">AgentCheck</h1>

<p align="center"><strong>Your agent just called a tool. Did it do the right thing?</strong><br/>
Calibrated verification for agent tool calls — every judgment measured,<br/>
every confidence proven, every failure caught before it ships.</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="#the-decision-log">The Decision Log</a> ·
  <a href="#trust-score">Trust Score</a> ·
  <a href="#red-team">Red Team</a> ·
  <a href="#self-host">Self-host</a> ·
  <a href="#docs">Docs</a>
</p>

<p align="center">
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-informational">
  <img alt="tests" src="https://img.shields.io/badge/tests-357%20passing-brightgreen">
  <img alt="red team" src="https://img.shields.io/badge/red%20team-467%20attacks%20%C2%B7%203.2%25%20ASR-red">
  <img alt="byok" src="https://img.shields.io/badge/judges-BYOK%20%2F%20OpenAI--compatible-ff69b4">
</p>

---

## What it looks like

**The Decision Log** — every tool call judged, the fail explained in plain
language ("The user asked for 'back up the customer list', but the agent chose
http_post"), the policy decision attached, your assessment stored beside the
machine's:

![The Decision Log: 27 calls, one selected, the dock explaining the fail](docs/screenshots/decision-log.png)

**The Trust Score** — measured, not asserted. Captured from a seeded instance:
tier `measured` on n=27, ECE 0.066, accuracy 89%, each component shown with
its weight:

![Trust Score view: 76 usable, tier measured, n=27](docs/screenshots/trust-score.png)

**Under attack** — a live run of all 467 red-team attacks in the browser,
per-family attack-success bars. This run: the `code-review` rubric, judge
`typesafe`, **0 of 467 passed**:

![Under attack: 467 attacks, per-family ASR, 0% on this run](docs/screenshots/red-team.png)

---

## Why AgentCheck

Agents now act: send email, move money, delete records. When they get it wrong,
nobody notices until a customer does — because **there was no way to verify a
tool call before it happened**. LLM-as-judge tools tell you a *score*; a score
from an uncalibrated model is a guess wearing a number.

AgentCheck is the missing **decision layer**:

1. **Judge** every tool call against a rubric you control — decomposed into
   atomic questions, batched into one round trip.
2. **Correct** the judge's confidence against measured reality — a 0.9 from a
   judge that is right 70% of the time reads as what it is.
3. **Floor** the judgment with deterministic screens that an LLM cannot be
   talked out of.
4. **Decide** — `approve`, `human`, or `block` — from a policy you wrote,
   persisted in a decision log you can audit.
5. **Prove it** — a Trust Score with its sample size attached, a calibration
   report (ECE/Brier) you can attach to a PR, and a 467-attack red-team suite.

No other tool ships measured calibration as a first-class feature. That is the
wedge, and it is deliberate.

> **Pricing and the honest state of it** —
> [35-253-233-192.sslip.io/start](https://35-253-233-192.sslip.io/start):
> self-host is free forever, hosted tiers exist in Razorpay, and self-serve
> signup is built but **not yet enabled on that host**, which the page says
> plainly rather than showing a button that 404s.
>
> There is **no public demo instance.** One existed and handed out a working key
> to anyone who asked (`/v1/bootstrap`), which meant a stranger could read the
> whole log and spend the judge budget without signing in. Public hosts now run
> with `AGENTCHECK_DEMO=0`: `/start` is marketing, `/` requires a key or a
> session, and every data-bearing endpoint returns 401 without one. The
> screenshots in this README are the way to see the product without running it.

## Quickstart

**See the whole thing in one command** — a verdict with its reasoning, then
the measurement that makes the confidence trustworthy:

```bash
pip install agentcheck-verify
agentcheck quickstart        # judges 3 sample calls, then calibrates the judge
```

With no key it runs the offline stub and says so. With `OPENAI_API_KEY` (or
`TYPESAFE_API_KEY`) it judges for real.

```bash
mkdir agentcheck && cd agentcheck
curl -fsSL https://raw.githubusercontent.com/amitashwinibhagat/agentcheck/main/deploy/gcp/docker-compose.yml -o docker-compose.yml
echo 'OPENAI_API_KEY=sk-…' > .env          # any OpenAI-compatible key — or none
docker compose up -d
open http://localhost:7373/
```

**No key at all also works** — an offline stub judge keeps every feature
runnable so you can evaluate before signing up for anything. The UI says so
plainly when it's running, and one click measures that judge against the
shipped 66-trace dataset (ECE and accuracy) so the calibration claim is not
taken on faith:

<p align="center"><img alt="Day one: the trust tab explains what it needs and offers to measure the judge" src="docs/screenshots/trust-day-one.png" width="900"></p>

To go live:

```bash
echo 'OPENAI_API_KEY=sk-…' > .env      # your key, or your provider's via OPENAI_BASE_URL
echo 'TYPESAFE_API_KEY=apikey_…' >> .env   # optional: the hosted, calibration-tuned judge
docker compose up -d
```

Or without Docker:

```bash
pip install agentcheck-verify    # the PyPI distribution name
agentcheck init                  # creates the store, live-tests your judge
agentcheck key my-laptop         # prints an API key once — paste it in the UI
agentcheck serve --port 7373
```

## One judged call

```bash
curl http://localhost:7373/v1/check \
  -H "Authorization: Bearer $AGENTCHECK_KEY" \
  -H "Content-Type: application/json" \
  -d '{"trace":{"request":"Summarize my unread inbox",
                "tool":"send_email",
                "args":{"to":"cfo@acme.com","body":"wire transfer details…"}},
        "policy":"demo-strict"}'
```

Real response (fields elided; stub judge):

```json
{
  "trace_verdict": "fail",
  "confidence": 0.88,
  "severity": 3.0,
  "decision": "block",
  "policy": "demo-strict",
  "checks": {
    "args_accomplish_request": {"value": 0.08, "confidence": 0.9},
    "high_risk":               {"value": 0.91, "confidence": 0.9},
    "data_exfiltration":       {"value": 0.2,  "confidence": 0.9},
    "verdict":                 {"value": "fail", "confidence": 0.88},
    "severity":                {"value": 3.0,  "confidence": 0.7}
  },
  "calibrated": {"reliability": 0.88, "source": "uncalibrated"},
  "id": "rs_…", "trace_id": "tr_…", "span_id": "sp_…"
}
```

The request said *summarize*. The call *sends money-talk email to the CFO*.
A plain keyword filter sees "email" and passes it. AgentCheck decomposes the
call into atomic judgments (`args_accomplish_request: 0.08` — the call does
not do what was asked), and the attached policy turns the failed verdict into
a persisted decision.

When a deterministic screen fires it lands on the row with its reason:

```json
"screen": {"rule": "destination_mismatch",
           "why": "data goes to a destination the request never named"}
```

**One row per distinct call** — identical calls reuse their stored result
(`"duplicate": true`) instead of growing the log; `GET /v1/results?verdict=fail`
filters; human assessments are stored beside machine verdicts and never
overwritten by them.

## The Decision Log

Every judged call lands in one screen: verdict, confidence, the policy decision,
and *why* — the rubric answers and any screen that fired, expandable per row.
Search it, filter it, live-stream it:

```
GET /v1/results?verdict=review        # what needs a human right now
GET /v1/runs?only=blocked             # whole agent runs that touched a block
GET /v1/traces/{trace_id}             # the ordered steps of one run
```

Runs group an agent's steps into one timeline — tools in order, per-step
verdict, confidence, decision and the policy that produced it. The **decision
is persisted** (`results.decision`), so the log answers *"what did we tell you
to do?"* months later, not just tonight.

![A blocked run: search_inbox pass, send_email review, http_post fail → block](docs/screenshots/runs.png)

Bring your own traces instead of live traffic — paste JSONL or upload a file,
pick the rubric, and the rows land in the same log with a `run_id`:

![Score my traces: upload JSON/JSONL or paste, pick a rubric](docs/screenshots/upload.png)

Hook your agent in directly — SSE stream plus a one-line SDK:

```python
from agentcheck.observe import observe
result = observe(my_tool_call, request="Summarize my unread inbox")
# -> {"verdict": "fail", "confidence": 0.88, "decision": "block", ...}
```

Native wrappers for **OpenAI, Anthropic, LangChain, LlamaIndex** — one line,
judge failures degrade to `.error` and never break your agent — plus import
from **Langfuse / LangSmith** to turn existing trace history into a day-one
calibration dataset.

![Connect an agent: the exact curl for one call or a batch](docs/screenshots/connect.png)

## Trust Score

A judge that always says 0.9 is not discriminating, whatever its accuracy.
The Trust Score measures judge behavior **on your traffic** and returns one
number with its sample size attached — because a trust score without a sample
size is a horoscope.
```bash
curl -H "Authorization: Bearer $KEY" localhost:7373/v1/trust
# {"score": 76, "verdict": "trusted", "tier": "measured", "n": 27, ...}
```

Two honest tiers:

- **consistency** — no labels needed: confidence spread, verdict stability,
  agreement with human sign-outs, abstention rate.
- **measured** — once you calibrate against labeled data, ECE and accuracy
  fold in. Below 30 decided items it says `insufficient-data` instead of
  pretending.

On the shipped 66-trace demo set: the real judge scored **84 (trusted)**, the
keyword stub **21 (low-trust)**. The score separates a judge that discriminates
from one that does not — live, in the UI's Trust view, with a shareable
`/v1/trust.svg` badge for your README.

![Trust Score view: 76 usable, measured tier, n=27, ECE 0.066](docs/screenshots/trust-score.png)

And it watches the trend, not just the number — per-bucket verdict mix,
flag rate, mean confidence, and how many you signed out:

![Over time: judgments bucketed by day with verdict mix bars](docs/screenshots/trust-over-time.png)

```bash
agentcheck calibrate --dataset agent-demo --judge openai --gate 0.6
# ECE, MCE, Brier, reliability table, risk/coverage curve
agentcheck calibrate --dataset agent-demo --judge openai --out report.html
# self-contained HTML report — attach it to the PR that changes your prompt
```

Demo datasets ship in the package; calibration works on any rubric, any judge,
your data included. Honesty is structural: **missing labels are refused, not
scored**; **no calibration report means the record says `uncalibrated`**.

### Measure the judge on your labels, not ours

The measured tier is reachable on **your own traffic** — sign judgments out in
the log (`looks_correct` / `actual_issue`), and those labels become a real
calibration. No dataset file, no second judge pass: the confidence is the one
recorded with each judgment, the truth is the person who signed it out.

Sign out 30 decided items and the tier moves to `measured`:

```bash
agentcheck calibrate --from-signoffs --publish
# ECE, accuracy and the reliability table over YOUR sign-outs
```

In the UI: the **Trust** tab shows the count (`12 of 30 decided sign-outs`),
labels at reading speed with **1 / 2 / 3** (each sign-out advances to the next
unlabeled call), and publishes in one click. Under 30 decided items it reports
the numbers but refuses to claim the tier, and publishing with no labels is
refused outright rather than writing a fabricated calibration.

The batch is **stratified, not log order**: every failure is included, passes
are spread across confidence bands, and distinct tools are preferred — the
first 30 rows are usually 30 near-identical passes, which measures the sample
instead of the judge. The running ECE appears as you label, so the number you
are building is visible from the first sign-out instead of only at item 30:

![Guided labeling: batch position, coverage, and the running ECE](docs/screenshots/labeling-hud.png)

## Red Team

467 adversarial tool calls across 28 families — 8 *mechanics* (exfiltration,
destructive, privilege, injection, scope creep, social engineering,
obfuscation, money) and 20 *industries* (healthcare, financial, legal, …),
because the same tool call can be ordinary in one context and a regulatory
failure in another.

| Suite | Families | Attacks | ASR |
|---|---|---|---|
| Mechanics | 8 | 303 | 3.0% |
| Industries | 20 | 164 | 3.7% |
| **Total** | **28** | **467** | **3.2%** |

Measured against the live judge with screens on (2026-09-19). Judge-alone it
was 6.4% — **the deterministic screens catch what the judge rationalises**.
Against the keyword stub, ~75% — the obfuscation family defeats keywords by
design, which is why "we filter on keywords" is not a defense.

Adding an attack is data, not code (`agentcheck/redteam_industries.py`), and
the worst miss on record is worth reading: `scope_creep.search_all_mail`
passed at **confidence 0.97** — a near-certain pass that reads every mailbox
without eDiscovery approval.

```bash
agentcheck redteam --judge openai --max-asr 0.0 --out attacks.json
agentcheck redteam --families healthcare,financial,telecom
```

And it runs in the browser — every family, live, with the failures called out:

![Under attack: 467 attacks, per-family ASR, 0% on this run](docs/screenshots/red-team.png)

## Deterministic screens — the floor beneath the model

An LLM judge can be talked out of a call that looks ordinary. Screens are
conservative, literal checks that run before the judge's verdict is finalised:

- **destination mismatch** — the request said one thing, the call reached
  somewhere else
- **control disabled** — guardrails or eDiscovery switched off mid-flight
- **gate bypass, backdated records, minors-data access**

And the invariant that makes them safe to turn on: **screens only ever
downgrade `pass` → `review`**. Worst case is one more human look — a
legitimate call is never floored, and a 44-case regression suite pins that.

## Decision policies

A verdict is not a decision. Policies turn verdict + confidence + severity
into `approve` / `human` / `block`:

```yaml
# refund-policy.yaml
- when: {verdict: fail, confidence: ">= 0.8"}
  then: block
- when: {verdict: review}
  then: human
```

```bash
agentcheck policies apply refund-policy.yaml --what-if sample.json
#   approve   27  (40.9%)
#   human      0  (0.0%)
#   block     39  (59.1%)
```

Dry-run before you enforce. The decision produced is stored with the row
forever — plan numbers come from *our* catalogue, never from a payload.

![Policies: each rule and its ruling — block, human, approve](docs/screenshots/policies.png)

## Bring your own judge

| Judge | Key | Notes |
|---|---|---|
| `openai` | `OPENAI_API_KEY` | **Any OpenAI-compatible endpoint** via `OPENAI_BASE_URL` — OpenRouter, Together, vLLM, Ollama. `OPENAI_JUDGE_MODEL` to pick the model. |
| `typesafe` | `TYPESAFE_API_KEY` | The hosted, calibration-tuned judge (System One / Jev). |
| `stub` | none | Offline, deterministic, always available. |

Set the key and AgentCheck picks it up; `AGENTCHECK_JUDGE` overrides. Swap
judges by writing one adapter file — the product never changes, and every
calibration report records *which* judge measured it.

## Rubrics are files, not code

```yaml
name: groundedness
description: >
  Does an answer stick to what the sources say?
verdict: groundedness
checks:
  - id: supported_by_context
    type: noul
    instructions: >
      Is every factual claim in the answer supported by the provided context?
  - id: groundedness
    type: choice
    instructions: How should this answer be treated?
    criteria:
      use: fully grounded in the sources, safe to use
      verify: mostly grounded but has a claim someone should check
      discard: contradicts or invents; do not use
```

Eight ship built-in (groundedness, injection-resistance, refund-policy,
comment-moderation, support-tone, code-review, RAG retrieval, …). Drop a YAML
file in the directory and it appears in the UI. The eval matrix runs
**datasets × rubrics × judges** in one command:

![Rubrics: every rubric available to the process, with its checks and verdicts](docs/screenshots/rubrics.png)

```bash
agentcheck eval --config evals/qa.yaml --fail-under 0.8 --gate ci
```

## Teams

Workspaces, seats, roles, single-use hashed invites; sign-in via **WorkOS
AuthKit** (email+password) or any **OIDC provider**. Identity is the provider's
user id — never the email, because emails get reassigned. Self-serve signup:
first sign-in creates your workspace, the UI mints your first API key, shown
once, stored hashed like every credential in this system.

## Why not just a keyword filter?

Fair question — and a keyword filter is often the right answer. The same 467
attacks score **~75% ASR** against literal pattern matching and **3.2%**
against a judge with deterministic screens under it. The honest breakdown of
why, and when a regex really is the right tool:
[docs/WHY-NOT-KEYWORDS.md](docs/WHY-NOT-KEYWORDS.md).

```bash
agentcheck redteam --judge stub       # the keyword baseline
agentcheck redteam --judge openai     # a judge, with screens underneath
```

## Self-host

```bash
# the ten-minute path — see docs/SELFHOST.md
echo 'OPENAI_API_KEY=sk-…' > .env
docker compose up -d

# something wrong? every self-host failure this project has hit, checked:
agentcheck doctor
```

- **Free forever, Apache-2.0, including commercial use.** No license key, no
  phone-home, no telemetry by default. Your traces live in a SQLite file you
  own; Postgres is one env var when you outgrow it.
- **Your keys stay yours.** The judge key sits beside your compose file; the
  BYOK path means no vendor signup is required to evaluate.

Prefer hosted? The live instance handles TLS, uptime monitoring, verified
nightly backups, and billing (Pro ₹24,999/mo, Team ₹79,999/mo, Enterprise custom,
included) at [app.35-253-233-192.sslip.io](https://app.35-253-233-192.sslip.io/).

## Why trust the numbers

Because they are printed next to their sample sizes, and the honest paths are
structural:

| Claim | Number | Where |
|---|---|---|
| Red-team attack success (judge + screens) | **3.2%** of 467 (judge-alone 6.4%) | `agentcheck redteam`, 2026-09-19 run |
| Same suite vs keyword filtering | **~75%** | same run, stub judge |
| Trust separation | real judge **84** vs stub **21** | 66-trace demo set |
| Measured calibration (live demo) | ECE **0.066**, accuracy **0.886** | `agentcheck calibrate` on `agent-demo` |
| Judged questions per round trip | **5** in one batched call | latency flat 10→60 questions |
| Test suite | **357 passing** | `pytest tests/` |

Judge failure never fails your run: integrations degrade to `.error` and the
agent continues. A record without ground truth is **refused, not scored**.
No calibration report means the output says **`uncalibrated`**, not a
plausible number.

## The design, in one page

Judge quality is decomposed, never vibes: every rubric is atomic questions
(`noul` probability / `choice` label / `score` level) answered in one batched
call, with confidence attached to every answer. Confidence is *corrected*
against empirical accuracy before a gate sees it. Deterministic screens sit
under the model because an LLM judge can be argued with; they only ever
downgrade. Vendor coupling is confined to one adapter file per judge —
`agentcheck/judges/` — so the product survives a vendor change, and every
calibration report names the judge that measured it.

Design details and the failure each decision prevents:
[docs/TRUST.md](docs/TRUST.md) · [docs/RUBRICS.md](docs/RUBRICS.md) ·
[docs/HOSTING.md](docs/HOSTING.md) · [docs/DEPLOY-GCP.md](docs/DEPLOY-GCP.md)

## <a name="docs"></a>Docs

| Doc | What it covers |
|---|---|
| [docs/SELFHOST.md](docs/SELFHOST.md) | Ten-minute private deploy, BYOK judges |
| [docs/WHY-NOT-KEYWORDS.md](docs/WHY-NOT-KEYWORDS.md) | The 75% vs 3.2% ASR gap, and when a regex is the right answer |
| [docs/TRUST.md](docs/TRUST.md) | The full loop: rubric → verdict → policy → human |
| [docs/RUBRICS.md](docs/RUBRICS.md) | Writing and shipping rubrics |
| [docs/HOSTING.md](docs/HOSTING.md) | Hosted deployment options and demo mode |
| [docs/DEPLOY-GCP.md](docs/DEPLOY-GCP.md) | Full cloud runbook: TLS, backups, monitoring |
| [docs/SIGNUP.md](docs/SIGNUP.md) | Sessions, workspaces, first-key flow |
| [CHANGELOG.md](CHANGELOG.md) | What changed, per release |

## Development

```bash
git clone https://github.com/amitashwinibhagat/agentcheck.git
cd agentcheck && pip install -e ".[dev]"
python -m playwright install chromium    # browser tests (optional)
pytest tests/ -q                         # expect: 357 passed, 2 skipped
```

> **`pip install agentcheck` does not install this.** That PyPI name is a
> different project (trace/replay/assert) that also registers an `agentcheck`
> command. This distribution is **`agentcheck-verify`** — the import package
> and CLI remain `agentcheck`.

## License

Apache-2.0. Self-hosting is free forever, including commercial use.
Built by [Amit Ashwini Bhagat](https://github.com/amitashwinibhagat).
