# Hosting a prospect demo

The demo requirement that decides everything: **the app must not sleep.**
Three of the four free tiers spin down after inactivity. Sending a link that
might be cold when the prospect clicks it is a different product decision
than running a live call.

## What demo mode changes (already shipped)

`AGENTCHECK_DEMO=1` makes `/v1/bootstrap` hand out a dedicated demo key from
any origin — 10 requests/min, 300 questions/month, never the admin key.
Without it, browser auto-login is localhost-only and every hosted URL shows
401s. Verified: remote IP gets 403 without the flag, a capped working key
with it.

## Ranked options (facts verified from vendor pages on the day noted)

| Option | Cost | Sleeps? | Notes |
|---|---|---|---|
| **Fly.io trial** | $0 | **No** | `shared-cpu-1x`, pick `iad` region (same as the judge API). Trial page live; adding a card lifts limits. |
| Railway trial | $5 one-time | No | Confirmed "$5 in credits" from their pricing FAQ. Same one-command deploy. |
| Oracle Always Free | $0 forever | **No** | Ampere A1 ARM confirmed in Always Free. Needs a card; ARM capacity is frequently unavailable. Best *permanent* free tier, worst *this week* option. |
| Hetzner CX22 | ~€4–5/mo | No | Cheapest always-on. Pricing page was JS-blocked when checked — treat the price as unverified. |
| Render free | $0 | **Yes** (750 hrs/mo, spins down) | Fine for a "click this link" async demo. Not for a call. |
| HF Spaces CPU basic | $0 | **Yes**; disk not persistent | Same. 2 vCPU/16GB confirmed free. |
| Koyeb free | $0 | **Yes** ("scale to zero") | Same. |

## Recommendation

**Fly.io for the live demo** (never sleeps, zero cost on trial, region
control), **HF Spaces or Render as the throwaway link** you paste in an
email. `fly.toml` is in the repo root:

```bash
fly launch --no-deploy
fly secrets set TYPESAFE_API_KEY=apikey_... AGENTCHECK_DEMO=1
fly deploy
```

Ephemeral disk is deliberate: every deploy is a clean slate. Add a `[mounts]`
block only if you want the Decision Log to survive redeploys.

---

## Live demo: https://agentcheck-demo.fly.dev

Deployed 2026-09-18, single machine `shared-cpu-1x` in `iad`. Verified end to
end from the public internet:

| Check | Result |
|---|---|
| `/v1/bootstrap` from a public IP | `{"key": "ac_…", "demo": true}` |
| Judged check (malicious trace) | `fail`, conf 0.98, `decision: block`, model `jev-1.13.0` |
| `/v1/policies` | `demo-strict`, `refund-safety` |
| `/v1/trust` on seeded log | score 77, usable, n=15 |
| `/v1/trust.svg` | `200 image/svg+xml` |
| `/v1/checksets` | 8 rubrics |
| Browser UI (Playwright) | all 6 sections render, 0 console errors |
| Red team on the public URL | 39 attacks, ASR **2.6%**, 1 evasion at conf **0.1** |

That last row is the demo's closing beat, and it reproduces the product's core
claim live: the judge approved a credential-collection attack while being
0.1 confident, which is exactly why low confidence must never count as a pass.

### Operational reality (read before demo day)

- **Single machine on purpose.** `fly deploy` had created two machines, each
  with its own SQLite file, so the demo key existed on one and not the other —
  requests load-balanced between them returned intermittent 401s. Scaled to
  one with `fly scale count 1`. Do not scale back up without a shared volume.
- **Ephemeral disk.** The seeded log is lost on redeploy. Reseed with:
  `AGENTCHECK_URL=https://agentcheck-demo.fly.dev python scripts/seed-demo.py`
  (~75 questions).
- **The demo key is public.** Anyone who loads the URL gets it, capped at
  300 questions/min and 5000/month. If the URL leaks widely, rotate by wiping
  `/data/agentcheck.db` and restarting (a new key is minted on boot).
- **Demo mode is not auth.** It only replaces the localhost-only check on
  `/v1/bootstrap`. `/v1/checksets`, `/v1/trust` and friends still require the
  key, and the key is capped.

## Demo-day checklist

1. `fly status -a agentcheck-demo` — one machine, started.
2. Open the URL cold in a private window — the log should already show 15
   seeded calls, not an empty state.
3. Trust view reads ~77 usable with n=15.
4. Run a live malicious trace in the Connect dialog so the prospect sees a
   `block` decision land.
5. Toggle **Live** and post a trace — the row appears with no refresh.
6. Run the red team. The 2.6% ASR and the 0.1-confidence evasion are the close.
7. If the judge feels slow, say so first: server-side is ~450ms end to end
   from `iad`; the rest is geography.
