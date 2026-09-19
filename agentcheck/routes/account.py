"""Platform account surface: bootstrap key, usage meter, key minting."""

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, Field

from agentcheck.limits import WINDOW_SECONDS
from agentcheck.routes import shared
from agentcheck.store import Store


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    qpm: int = Field(default=600, ge=1, le=10_000)
    allowance: int = Field(default=500, ge=0, le=10_000_000)
    plan: str = "free"
    trial_days: int | None = Field(default=None, ge=1, le=365)


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
    async def usage(authorization: str | None = Header(None)):
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
    async def create_key(req: CreateKeyRequest, authorization: str | None = Header(None)):
        # Local workspace: any valid key can mint a sibling key for the same store.
        shared.authorize(store, authorization)
        plan = req.plan if req.plan in ("free", "pro", "trial") else "free"
        minted = store.create_key(req.name, qpm_limit=req.qpm,
                                  monthly_allowance=req.allowance, plan=plan,
                                  trial_days=req.trial_days)
        return {"key": minted, "name": req.name, "qpm_limit": req.qpm,
                "monthly_allowance": req.allowance, "plan": plan}
