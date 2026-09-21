"""Billing: public catalogue, checkout, and the provider webhook."""

from fastapi import Header, HTTPException, Request

from agentcheck import billing
from agentcheck.routes import shared


def register(app, store):
    @app.get("/v1/billing/plans")
    def billing_plans():
        """The catalogue. Public: a pricing page needs it before signup."""
        p = billing.get_provider()
        return {"plans": billing.plans_public(), "provider": p.name,
                "configured": p.configured()}

    @app.post("/v1/billing/checkout")
    async def billing_checkout(payload: dict | None = None,
                               authorization: str | None = Header(None)):
        """Start a subscription for the CALLER's key.

        The plan is chosen here and written into the provider's notes, so the
        webhook that returns can be matched back to this key without any
        client-supplied identity.
        """
        key = shared.authorize(store, authorization)
        plan_name = str((payload or {}).get("plan") or "pro")
        try:
            billing.plan(plan_name)
        except billing.BillingError as e:
            raise HTTPException(422, str(e)) from None
        if billing.display_mismatch(plan_name):
            # Refuse rather than debit a currency the customer was not shown.
            raise HTTPException(
                503,
                f"{plan_name} is priced in {billing.DISPLAY_CURRENCY} but the "
                f"configured payment provider settles "
                f"{billing.charge_currency(plan_name)}; checkout is disabled "
                f"until USD billing is configured. Self-host is free today.")
        provider = billing.get_provider()
        if not provider.configured():
            raise HTTPException(
                503, "billing is not configured on this deployment")
        try:
            sub = provider.create_subscription(key, plan_name)
        except billing.BillingError as e:
            raise HTTPException(502, str(e)) from None
        store.record_event(key, "billing_checkout_started",
                           {"plan": plan_name,
                            "subscription_id": sub.get("id")})
        return {"subscription_id": sub.get("id"), "plan": plan_name,
                "provider": provider.name,
                "key_id": getattr(provider, "key_id", None)}

    @app.post("/v1/billing/webhook")
    async def billing_webhook(request: Request):
        """Provider callback. Unauthenticated BY DESIGN, because the
        signature is the authentication.

        Order matters: verify over the raw bytes first; only then parse. No
        field of an unverified payload is read.
        """
        provider = billing.get_provider()
        body = await request.body()
        signature = request.headers.get("X-Razorpay-Signature")
        if not provider.verify_webhook(body, signature):
            raise HTTPException(401, "invalid webhook signature")
        try:
            event = provider.parse_event(body)
            result = billing.apply_event(store, event)
        except billing.BillingError as e:
            raise HTTPException(400, str(e)) from None
        return {"received": True, **result}
