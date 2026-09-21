"""Platform account surface: bootstrap key, usage meter, key minting."""

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, Field

from agentcheck import billing
from agentcheck.limits import WINDOW_SECONDS
from agentcheck.routes import shared
from agentcheck.routes.auth import ME_KEY_LIMIT
from agentcheck.store import Store


class CreateKeyRequest(BaseModel):
    """A minted key's name. Nothing else is accepted from the caller.

    This model used to carry `qpm`, `allowance`, `plan` and `trial_days`, and
    `/v1/keys` passed them straight to the store. Any key — a free one, or the
    public demo key — could therefore mint itself a key with plan="pro" and an
    allowance bounded only by the pydantic limit (10,000,000). That is the
    catalogue invariant broken from the outside: plan numbers come from PLANS,
    never from a payload. The extra fields are gone rather than clamped, so a
    caller cannot even ask.
    """
    name: str = Field(min_length=1, max_length=80)


def register(app, store, guard, demo_mode, demo_key, ui_key):
    @app.get("/v1/bootstrap")
    def bootstrap(request: Request):
        # Hosted demo: hand out the capped demo key from anywhere. Local:
        # hand out the full browser key, localhost only, as before.
        if demo_mode:
            return {"key": demo_key, "demo": True}
        if not shared.is_local(request):
            raise HTTPException(403, "browser auto-login is only for localhost")
        return {"key": ui_key}

    @app.get("/v1/usage")
    async def usage(request: Request,
                   authorization: str | None = Header(None)):
        key = shared.authorize(store, authorization)
        meta = store.key_meta(key) or {}
        allowance = meta.get("monthly_allowance", 500)
        used_month = store.used_this_month(key)
        _, resets_at = Store._month_start()
        return {
            "window_seconds": WINDOW_SECONDS,
            "used_in_window": guard.used(key),
            "qpm_limit": meta.get("qpm_limit"),
            "key_name": meta.get("name"),
            # Which judge actually answers, resolved at startup. The UI shows
            # an honest warning when this is the offline stub, instead of
            # letting someone judge the product by keyword matching.
            "judge": getattr(request.app.state, "judge", None),
            "plan": meta.get("plan", "free"),
            "trial_active": meta.get("trial_active", False),
            "trial_ends_at": meta.get("trial_ends_at"),
            "allowance": {
                "monthly_allowance": allowance,
                "used_this_month": used_month,
                "remaining": max(allowance - used_month, 0),
                "resets_at": resets_at,
            },
            "totals": store.totals(key),
        }

    @app.post("/v1/keys")
    async def create_key(req: CreateKeyRequest,
                         authorization: str | None = Header(None)):
        """Mint a key into the caller's workspace, on the workspace's plan.

        Three deliberate limits:
          * the demo key is refused. It is handed to anyone who asks, and a
            mint carries a FRESH monthly allowance, so one public key would
            otherwise become unlimited judge budget — and since a mint used to
            spawn a NEW workspace, a per-workspace cap would never have bitten.
          * the new key joins the caller's workspace instead of spawning one,
            so mints are visible and revocable where the caller already looks.
          * allowance and qpm come from the workspace's catalogue plan, never
            from the request (see CreateKeyRequest).
        """
        key = shared.authorize(store, authorization)
        meta = store.key_meta(key) or {}
        if meta.get("name") == "demo":
            raise HTTPException(403, "the demo key cannot mint keys; "
                                     "self-host for free, or sign in")
        _, ws, caller_role = shared.acting(store, authorization)
        if len(store.workspace_keys(ws["id"])) >= ME_KEY_LIMIT:
            raise HTTPException(429, f"key limit reached for this workspace "
                                     f"({ME_KEY_LIMIT}); use an existing key")
        plan = ws.get("plan") or "free"
        spec = billing.PLANS.get(plan, billing.PLANS["free"])
        # The new credential carries the CALLER's role: a member cannot mint
        # themselves an owner, however they call this.
        minted = store.create_key(req.name, qpm_limit=int(spec["qpm"]),
                                  monthly_allowance=int(spec["allowance"]),
                                  plan=plan, workspace_id=ws["id"],
                                  role=caller_role)
        return {"key": minted, "name": req.name, "qpm_limit": spec["qpm"],
                "monthly_allowance": spec["allowance"], "plan": plan,
                "role": caller_role}
