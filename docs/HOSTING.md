# Hosting a prospect demo

The demo requirement that decides everything: **the app must not sleep.**
Several free tiers spin down after inactivity. Sending a link that might be
cold when the prospect clicks it is a different product decision than running
a live call.

> **Fly.io is retired.** The trial is exhausted and deploys are refused
> ("We need your payment information"). The old `agentcheck-demo.fly.dev` may
> still answer until it is reclaimed, but it cannot be updated and its `/data`
> was never a volume. Do not build on it. The live demo is on GCP — see
> [`DEPLOY-GCP.md`](DEPLOY-GCP.md).

## What demo mode changes (already shipped)

`AGENTCHECK_DEMO=1` makes `/v1/bootstrap` hand out a dedicated demo key from
any origin — never the admin key. The cap is sized for one full demo session:
seeding a log plus a complete red-team run (467 attacks × 5 checks ≈ 2,800
questions) has to fit in a single burst.

| | value |
|---|---|
| rate limit | 4,000 questions/min (burst protection, not budget) |
| monthly allowance | 40,000 questions (bounds total abuse across visitors) |

Without the flag, browser auto-login is localhost-only and every hosted URL
shows 401s. Verified: a remote IP gets 403 without the flag, a capped working
key with it.

## Ranked options (facts verified from vendor pages on the day noted)

| Option | Cost | Sleeps? | Notes |
|---|---|---|---|
| **GCP `e2-micro` (Always Free)** | $0 | **No** | What the demo runs on. 1 GB RAM, 30 GB disk, 1 GB/mo egress, US regions only. Needs swap for the image build. See `DEPLOY-GCP.md`. |
| Oracle Always Free | $0 forever | **No** | Ampere A1 ARM confirmed in Always Free. Needs a card; ARM capacity is frequently unavailable. Best *permanent* free tier, worst *this week* option. |
| Hetzner CX22 | ~€4–5/mo | No | Cheapest always-on. Pricing page was JS-blocked when checked — treat the price as unverified. |
| Railway trial | $5 one-time | No | Confirmed "$5 in credits" from their pricing FAQ. |
| ~~Fly.io trial~~ | — | — | **Retired.** Trial exhausted, deploys refused, no persistent volume. |
| Render free | $0 | **Yes** (750 hrs/mo, spins down) | Fine for a "click this link" async demo. Not for a call. |
| HF Spaces CPU basic | $0 | **Yes**; disk not persistent | Same. 2 vCPU/16GB confirmed free. |
| Koyeb free | $0 | **Yes** ("scale to zero") | Same. |

## Recommendation

**GCP `e2-micro`, one VM, Caddy for automatic TLS** — that is what the live
demo uses and the full runbook is in [`DEPLOY-GCP.md`](DEPLOY-GCP.md). For a
throwaway link you paste in an email, HF Spaces or Render are fine.

One measured budget note: 1 GB egress is roughly **11,000 page loads or 7,000
red-team runs** against this app, so the free tier is comfortable for a demo.

---

## Live demo: https://35-253-233-192.sslip.io

One GCP `e2-micro`, app plus Caddy, data on a named volume on the boot disk
that survives rebuilds and reboots. This is the instance to check before a
demo.

| Check | How |
|---|---|
| `/v1/bootstrap` from a public IP | returns `{"key": "ac_…", "demo": true}` |
| Seeded log present | `GET /v1/results` shows the seeded calls, not an empty state |
| Rubrics | `GET /v1/checksets` returns 8 |
| Policies | `GET /v1/policies` returns the shipped set |
| Trust | `GET /v1/trust` returns a score with its `tier` and `n` |
| Badge | `GET /v1/trust.svg` returns `200 image/svg+xml` |
| Browser UI | all five nav sections render, 0 console errors |

Read the live values at demo time rather than trusting a number in this file —
the trust score and sample size move as the demo is used, and a stale figure in
a doc is exactly how the ASR discrepancy happened elsewhere.

### Operational reality (read before demo day)

- **Single machine on purpose.** Two machines sharing one SQLite file returned
  intermittent 401s (each had its own database), so the demo is pinned to one.
  Do not scale out without Postgres (`AGENTCHECK_DB_URL`).
- **The demo key is public.** Anyone who loads the URL gets it, capped as
  above. If the URL leaks widely, wipe the store and restart to rotate.
- **Demo mode is not auth.** It only replaces the localhost-only check on
  `/v1/bootstrap`. Every other endpoint still requires the key, and the key is
  capped.
- **Egress is the free-tier limit**, not CPU or disk. The red-team run is the
  heaviest page; the budget above covers thousands of them.

## Demo-day checklist

1. `curl -s https://35-253-233-192.sslip.io/v1/bootstrap` — a key comes back.
2. Open the URL cold in a private window — the log should already show seeded
   calls, not an empty state.
3. Open **Trust → Score** and read the tier and `n` aloud, whatever they are.
4. Run a live malicious trace in the Connect dialog so the prospect sees a
   `block` decision land.
5. Toggle **Live** and post a trace — the row appears with no refresh.
6. Open **Trust → Under attack**, run the suite, and show the ASR with the
   evaded list. The evasions are the honest part; do not skip them.
7. If the judge feels slow, say so first: the app is deployed in the US near
   the judge API, so most of the perceived latency is the visitor's own
   distance from that region, not inference.
