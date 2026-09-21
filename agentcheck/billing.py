"""Billing: plans, and the provider that sells them.

The posture decided earlier: **proxy-metered**. The meter already prices judge
cost (questions/month), so a plan is nothing but an allowance plus a rate
limit. Billing therefore has one job — move a key between plans, safely.

Two rules this module holds to:

  * a webhook is UNTRUSTED input until its signature verifies over the exact
    raw bytes. Nothing in the payload is read before that check passes.
  * the plan applied comes from OUR catalogue, never from the payload. A
    signed event tells us *which kid* and *which plan name*; the allowance is
    looked up locally, so a tampered or stale payload cannot grant 10x.

Razorpay is the first provider. Dodo (merchant-of-record) would implement
``Provider`` and change nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

# Plans are the product, not an implementation detail: allowance is the
# monthly judged-question budget, qpm the burst ceiling.
#
# Priced as a control plane, not a consumer app. ₹2,999/₹9,999 read as a
# hobby SKU to an enterprise buyer — price is a quality signal in that
# motion, and those numbers dismissed the product before the demo started.
# Self-host stays ₹0 forever (Apache-2.0); that is the indie path, not a
# discount on hosted. Enterprise is deliberately unpriced on the public
# grid: annual contract, SSO/DPA/audit, not a fourth monthly card.
PLANS: dict[str, dict] = {
    "free": {"allowance": 500, "qpm": 600, "label": "Free", "seats": 1,
             "price_inr": 0, "blurb": "Try it on one rubric."},
    "pro": {"allowance": 50000, "qpm": 4000, "label": "Pro", "seats": 5,
             "price_inr": 24999, "blurb": "One production agent, in production."},
    "team": {"allowance": 250000, "qpm": 20000, "label": "Team", "seats": 15,
             "price_inr": 79999, "blurb": "Several agents, one audit trail."},
    "enterprise": {"allowance": 0, "qpm": 0, "label": "Enterprise", "seats": 0,
                   "price_inr": None, "blurb": "SSO, DPA, audit export. Annual."},
}

# Razorpay plan ids are per-environment, so they come from config.
_PLAN_ENV = {"pro": "RAZORPAY_PLAN_ID_PRO", "team": "RAZORPAY_PLAN_ID_TEAM"}


class BillingError(RuntimeError):
    pass


def plan(name: str) -> dict:
    if name not in PLANS:
        raise BillingError(f"unknown plan {name!r}; have {sorted(PLANS)}")
    return PLANS[name]


def plans_public() -> list[dict]:
    return [{"name": n, **{k: v for k, v in p.items()}} for n, p in PLANS.items()]


def plan_id_for(name: str) -> str:
    """The provider's plan id, per tier, from an AGENTCHECK-specific variable.

    There is deliberately NO generic RAZORPAY_PLAN_ID fallback. A shared
    Razorpay account may hold plans for several products, and silently
    reusing whichever one is in the environment would charge customers for
    the wrong thing. Each tier names its own plan.
    """
    env = _PLAN_ENV.get(name)
    if not env:
        raise BillingError(f"plan {name!r} is not sellable")
    pid = (os.environ.get(env) or "").strip()
    if not pid:
        raise BillingError(f"no provider plan id for {name!r}; set {env}")
    return pid


# ── provider interface ──────────────────────────────────────────────────────

class Provider:
    """What the app needs from a payment provider. Nothing else."""

    name = "none"

    def configured(self) -> bool:
        return False

    def verify_webhook(self, body: bytes, signature: str | None) -> bool:
        return False

    def create_subscription(self, kid: str, plan_name: str,
                            total_count: int = 12) -> dict:
        raise BillingError("no provider configured")

    def parse_event(self, body: bytes) -> dict:
        """(event_name, kid, plan_name) from a VERIFIED payload."""
        raise BillingError("no provider configured")


class NullProvider(Provider):
    """No billing configured: every call is a clear error, never a silent
    success. A demo deployment should not pretend to take money."""

    def create_subscription(self, kid, plan_name, total_count=12):
        raise BillingError("billing is not configured on this deployment")


class RazorpayProvider(Provider):
    """Razorpay subscriptions (India + international cards).

    Uses httpx rather than the SDK so the dependency list does not grow for a
    single integration. Auth is HTTP Basic key_id:key_secret.
    """

    name = "razorpay"
    API = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str | None = None, key_secret: str | None = None,
                 webhook_secret: str | None = None, transport=None):
        self.key_id = key_id or os.environ.get("RAZORPAY_KEY_ID", "")
        self.key_secret = key_secret or os.environ.get("RAZORPAY_KEY_SECRET", "")
        self.webhook_secret = (webhook_secret
                               or os.environ.get("RAZORPAY_WEBHOOK_SECRET", ""))
        self._transport = transport  # tests inject a fake

    def configured(self) -> bool:
        return bool(self.key_id and self.key_secret and self.webhook_secret)

    def verify_webhook(self, body: bytes, signature: str | None) -> bool:
        """HMAC-SHA256 over the RAW body, constant-time compare.

        Razorpay signs the exact bytes sent; re-serialising the JSON first
        would change them and break (or worse, spoof) the check.
        """
        if not self.webhook_secret or not signature:
            return False
        expected = hmac.new(self.webhook_secret.encode(), body,
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature.strip())

    def _post(self, path: str, payload: dict) -> dict:
        import httpx
        auth = base64.b64encode(
            f"{self.key_id}:{self.key_secret}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}",
                   "Content-Type": "application/json"}
        if self._transport is not None:
            r = self._transport("POST", self.API + path, json=payload,
                                headers=headers)
        else:
            r = httpx.post(self.API + path, json=payload, headers=headers,
                           timeout=20.0)
        if r.status_code >= 400:
            raise BillingError(f"razorpay {path} -> {r.status_code}: "
                               f"{r.text[:300]}")
        return r.json()

    def create_subscription(self, kid: str, plan_name: str,
                            total_count: int = 12) -> dict:
        body = {
            "plan_id": plan_id_for(plan_name),
            "total_count": total_count,
            "customer_notify": 1,
            # notes ride back on every webhook, which is how a payment is
            # tied to a key without a second database.
            "notes": {"kid": kid, "plan": plan_name},
        }
        return self._post("/subscriptions", body)

    def parse_event(self, body: bytes) -> dict:
        """Extract (event, kid, plan) from an already-verified body.

        The plan NAME is read from notes, but the ALLOWANCE is not: the caller
        must look that up in PLANS, so a forged plan name buys nothing even if
        a signature were somehow obtained.
        """
        try:
            data = json.loads(body)
        except Exception as e:
            raise BillingError(f"malformed webhook body: {e}") from e
        event = str(data.get("event") or "")
        entity = (((data.get("payload") or {}).get("subscription") or {})
                  .get("entity") or {})
        notes = entity.get("notes") or {}
        if not notes:
            # some events carry the payment, not the subscription
            pay = (((data.get("payload") or {}).get("payment") or {})
                   .get("entity") or {})
            notes = pay.get("notes") or {}
            entity = entity or pay
        return {
            "event": event,
            "kid": str(notes.get("kid") or ""),
            "plan": str(notes.get("plan") or ""),
            "subscription_id": str(entity.get("id") or ""),
            "status": str(entity.get("status") or ""),
        }


def get_provider(name: str | None = None) -> Provider:
    """The configured provider, or NullProvider.

    Billing is opt-in: it requires AGENTCHECK_BILLING to be set explicitly.
    Mere RAZORPAY_* credentials in the environment do NOT arm the payment
    endpoints, because those credentials may be live and may belong to a
    different product.
    """
    setting = name if name is not None else os.environ.get(
        "AGENTCHECK_BILLING", "")
    name = (setting or "none").lower()
    if name in ("none", "null", ""):
        return NullProvider()
    if name == "razorpay":
        p = RazorpayProvider()
        return p if p.configured() else NullProvider()
    raise BillingError(f"unknown billing provider {name!r}")


# Events that should move a key, and where they move it.
ACTIVATING = {"subscription.activated", "subscription.charged",
              "subscription.resumed", "payment.captured"}
CANCELLING = {"subscription.cancelled", "subscription.completed",
              "subscription.halted", "payment.failed"}


def apply_event(store, event: dict) -> dict:
    """Move a key to the plan an event implies. Returns what happened.

    Allowance and qpm come from PLANS, checked here, so the payload can only
    ever select a plan by name — never set a number.
    """
    name = event.get("event") or ""
    kid = event.get("kid") or ""
    plan_name = event.get("plan") or ""
    if not kid:
        return {"applied": False, "reason": "event carries no kid"}
    if store.lookup_kid(kid) is None:
        return {"applied": False, "reason": f"unknown key {kid!r}"}
    if name in ACTIVATING:
        if plan_name not in PLANS:
            return {"applied": False, "reason": f"unknown plan {plan_name!r}"}
        p = PLANS[plan_name]
        store.set_plan(kid, plan_name, allowance=p["allowance"],
                       qpm=p["qpm"],
                       subscription_id=event.get("subscription_id"))
        # The workspace is what a team shares, so its seat limit has to move
        # with the plan — otherwise an upgrade buys questions but no seats.
        ws = store.workspace_for_key(kid)
        if ws is not None:
            store.set_workspace_plan(ws["id"], plan_name,
                                     int(p.get("seats", ws["seat_limit"])))
        store.record_event(kid, "billing_activated",
                           {"plan": plan_name, "event": name,
                            "subscription_id": event.get("subscription_id")})
        return {"applied": True, "plan": plan_name, "action": "activate"}
    if name in CANCELLING:
        p = PLANS["free"]
        store.set_plan(kid, "free", allowance=p["allowance"], qpm=p["qpm"],
                       subscription_id=None)
        ws = store.workspace_for_key(kid)
        if ws is not None:
            store.set_workspace_plan(ws["id"], "free", int(p["seats"]))
        store.record_event(kid, "billing_cancelled",
                           {"from_plan": plan_name, "event": name})
        return {"applied": True, "plan": "free", "action": "downgrade"}
    return {"applied": False, "reason": f"event {name!r} is not actionable"}
