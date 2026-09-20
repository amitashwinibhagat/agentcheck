# Signup: from stranger to first judged call

**Status:** design. Nothing here is built yet.

## The gap, verified

Sign-in works (WorkOS/OIDC → session cookie, verified with a real login). But
a stranger who signs in hits three dead ends, all confirmed in code:

1. **No workspace.** `auth_callback` upserts the user and claims invites, but
   nothing creates a workspace for a fresh user. Only `store.create_key`
   spawns one (via `_attach_workspace`).
2. **No key, and no way to get one.** `/v1/keys` requires a Bearer key. A
   session cannot mint. The funnel dies at signup → activated.
3. **The UI never touches auth.** It is key-gate only; after a WorkOS redirect
   there is no sign-in button, no session detection, no path to a key.
4. **No `email_verified`.** Profiles are `{provider_user_id, email, name}`.
   The app cannot distinguish a verified address from a typed one.
5. **No key→workspace join.** `create_key` always spawns a *new* workspace.
   There is no API to attach a key to an existing one — so a naive
   "mint on signup" would leave every user with two workspaces.

## Design principles

- **Compose, don't invent.** Every step below reuses a tested primitive
  (`create_workspace`, `link_member`, `create_key`, session auth).
- **Idempotent first login.** OAuth callbacks can be retried; running the
  "first login" logic twice must not create two workspaces or two keys.
- **Abuse is part of the design, not a follow-up.** A signup that mints judge
  budget must be rate-limited from day one.
- **The key is shown once.** Same rule as CLI issuance and invites: raw token
  returned once, hash at rest, never listed.

## The flow

```
stranger → WorkOS sign-in → callback → [new] first-login setup
         → UI shows the key once → paste/use → first judged call
```

**Step 1 — first-login setup, in `auth_callback`, after `claim_invites`:**

```python
if not store.workspaces_for_user(user["id"]):
    ws = store.create_workspace(f"{name or email}'s workspace",
                                owner_email=email)   # owner row, user_id NULL
    store.link_member(ws["id"], email, user["id"])   # bind this user to it
```

Both primitives exist and are tested. The `if` makes it idempotent: a user
with any workspace (claimed invite, previous login) skips it. Name from the
profile, falling back to the email local-part — never the email as identity
(the existing invariant: identity is `(provider, provider_user_id)`).

**Step 2 — first key, auto-minted and displayed once.**

On the same first login, mint one key bound to that workspace and return it
to the UI to display once (the GitHub-token / Stripe-key pattern). Rationale
for auto-mint over an explicit button: the funnel stage after signup is
`activated` (≥1 judged call), and every extra click loses people. Abuse is
controlled, not by friction, but by the controls in §Abuse below.

This requires one small store extension, because `create_key` always spawns a
workspace today:

```python
store.create_key(name, ..., workspace_id=wid)  # NEW optional param
# when given: INSERT the key with workspace_id set, and add the row to
# workspace_members only if the caller isn't one; do NOT call _attach_workspace
```

Without it, every signup would own two workspaces. The param defaults to
today's behavior, so CLI issuance and migrations are untouched.

**Step 3 — the UI learns about sessions.**

Today the UI only knows keys (`sessionStorage`, the key gate). Add:

- A **Sign in** button (visible when `/v1/auth/me` reports
  `authenticated: false`) linking to `/v1/auth/login?next=/`.
- On load with an authenticated session and no stored key: show the
  first-key display (from Step 2) or a "create key" affordance, then store it
  in `sessionStorage` through the existing key-gate mechanism.
- Sign-out already exists server-side (`POST /v1/auth/logout`); wire a button
  that also clears the tab key.

No new auth protocol: sessions, cookies and hashing are built and tested.

## The API contract

```
POST /v1/me/keys
Auth: session cookie (NOT Bearer — this is the point: a session with no key
      must be able to get its first one)
Body: {"name": "my laptop"} (optional; defaults sensibly)
```

Response: `{"key": "ac_…", "kid": "…", "workspace_id": "…", "plan": "free"}`
— the raw token **once**, never again.

Errors: `401` (no session), `429` (too many keys — see Abuse), `403` (user
has no workspace, which Step 1 should have made impossible; kept as a guard).

`POST /v1/keys` (Bearer) is unchanged for programmatic use.

## Abuse controls (day one, not later)

A signup mints judge budget, so signup is the abuse surface. All cheap:

- **WorkOS bot protection on** (available; do not disable it to make a test
  pass — that failure mode is already documented).
- **Require verified email in the WorkOS dashboard config.** The app cannot
  check what the IdP does not return; until profiles carry `email_verified`,
  the dashboard setting is the enforcement point.
- **Rate-limit key minting**: max 5 keys per user on free (enforced in
  `POST /v1/me/keys`, counted from the store, not from memory).
- **Free-tier caps already meter** (500/mo, 600 qpm). A spam signup costs a DB
  row; it costs judge budget only when used.
- **No email-as-identity**, ever (existing invariant — emails get reassigned).

## Plan and trials

New signups start on **free** (500/mo, 1 seat). The `trial_days` parameter
already exists on `create_key` for later; trials are a pricing decision, not
part of this build.

## WorkOS checklist (human actions, already in AGENTS.md)

Production redirect URI for the callback, Email + Password sign-in method
enabled, bot protection on. Staging redirects do not carry to production, and
a fresh environment offers only enterprise SSO until the method is enabled —
both already paid for in debugging time.

## Tests to write (before merging, per project standard)

- First login with no invite creates exactly one workspace with the user as
  owner; second login creates none (idempotency).
- `POST /v1/me/keys` with a session returns a working key bound to that
  workspace; without a session it 401s; the 6th key 429s.
- The raw token authenticates once-minted calls; listing keys never returns it.
- A Bearer key still cannot mint via the session endpoint and vice versa
  (the two auth modes stay separate).

## Sequencing

Signup unblocks **billing** (checkout needs a key; today only hand-issued keys
exist) and the **funnel** (`signed_up` becomes a real stage instead of
"someone ran the CLI"). It does not unblock enterprise — SSO/SAML, audit
export and SOC 2 remain behind it, in that order.

## Non-goals

- No new identity provider (WorkOS + OIDC cover it).
- No email verification UI (delegated to WorkOS config).
- No plan selection at signup (free; upgrade is a billing flow).
- No username/password in this codebase, ever.
