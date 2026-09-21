"""Billing tests. The properties that matter:

  * a webhook is untrusted until its signature verifies over the RAW bytes
  * a payload can select a plan by NAME but can never set an allowance
  * an unconfigured deployment refuses clearly instead of pretending
"""

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path

from agentcheck import billing
from agentcheck.store import Store

SECRET = "whsec_test_123"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _provider(**kw):
    kw.setdefault("key_id", "rzp_test_key")
    kw.setdefault("key_secret", "rzp_test_secret")
    kw.setdefault("webhook_secret", SECRET)
    return billing.RazorpayProvider(**kw)


def _sub_event(kid="kid_1", plan="pro", event="subscription.activated"):
    body = {
        "event": event,
        "payload": {"subscription": {"entity": {
            "id": "sub_ABC123", "status": "active",
            "notes": {"kid": kid, "plan": plan},
        }}},
    }
    return json.dumps(body).encode()


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class TestSignature(unittest.TestCase):
    def test_valid_signature_passes(self):
        p = _provider()
        body = _sub_event()
        self.assertTrue(p.verify_webhook(body, _sign(body)))

    def test_wrong_secret_fails(self):
        p = _provider()
        body = _sub_event()
        self.assertFalse(p.verify_webhook(body, _sign(body, "other_secret")))

    def test_tampered_body_fails(self):
        p = _provider()
        body = _sub_event()
        sig = _sign(body)
        tampered = body.replace(b"pro", b"team")
        self.assertNotEqual(body, tampered)
        self.assertFalse(p.verify_webhook(tampered, sig))

    def test_missing_signature_fails(self):
        p = _provider()
        self.assertFalse(p.verify_webhook(_sub_event(), None))
        self.assertFalse(p.verify_webhook(_sub_event(), ""))

    def test_unconfigured_provider_verifies_nothing(self):
        p = billing.RazorpayProvider(key_id="", key_secret="", webhook_secret="")
        body = _sub_event()
        self.assertFalse(p.verify_webhook(body, _sign(body)))


class TestParse(unittest.TestCase):
    def test_extracts_event_kid_plan(self):
        p = _provider()
        e = p.parse_event(_sub_event(kid="kid_9", plan="team"))
        self.assertEqual(e["event"], "subscription.activated")
        self.assertEqual(e["kid"], "kid_9")
        self.assertEqual(e["plan"], "team")

    def test_payment_event_falls_back_to_payment_notes(self):
        p = _provider()
        body = json.dumps({
            "event": "payment.captured",
            "payload": {"payment": {"entity": {
                "id": "pay_1", "notes": {"kid": "kid_2", "plan": "pro"}}}},
        }).encode()
        e = p.parse_event(body)
        self.assertEqual((e["kid"], e["plan"]), ("kid_2", "pro"))

    def test_malformed_body_is_a_clear_error(self):
        with self.assertRaises(billing.BillingError):
            _provider().parse_event(b"{not json")


class TestApplyEvent(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "b.db")
        self.raw = self.store.create_key("acct")
        self.kid = self.store.lookup_key(self.raw)["kid"]

    def test_activation_uses_our_allowance_not_the_payload(self):
        # a payload that TRIES to set numbers must not change them
        event = {"event": "subscription.activated", "kid": self.kid,
                 "plan": "pro", "subscription_id": "sub_1",
                 "allowance": 99999999, "qpm_limit": 99999999}
        out = billing.apply_event(self.store, event)
        self.assertTrue(out["applied"], out)
        meta = self.store.key_meta(self.kid)
        self.assertEqual(meta["plan"], "pro")
        self.assertEqual(meta["monthly_allowance"], billing.PLANS["pro"]["allowance"])
        self.assertEqual(meta["qpm_limit"], billing.PLANS["pro"]["qpm"])
        self.assertNotEqual(meta["monthly_allowance"], 99999999)

    def test_unknown_plan_name_is_refused(self):
        out = billing.apply_event(self.store, {
            "event": "subscription.activated", "kid": self.kid,
            "plan": "enterprise-unlimited"})
        self.assertFalse(out["applied"])
        self.assertEqual(self.store.key_meta(self.kid)["plan"], "free")

    def test_unknown_kid_is_refused(self):
        out = billing.apply_event(self.store, {
            "event": "subscription.activated", "kid": "kid_nope", "plan": "pro"})
        self.assertFalse(out["applied"])

    def test_no_kid_is_refused(self):
        out = billing.apply_event(self.store, {
            "event": "subscription.activated", "kid": "", "plan": "pro"})
        self.assertFalse(out["applied"])

    def test_cancellation_returns_to_free(self):
        billing.apply_event(self.store, {
            "event": "subscription.activated", "kid": self.kid, "plan": "team"})
        self.assertEqual(self.store.key_meta(self.kid)["plan"], "team")
        out = billing.apply_event(self.store, {
            "event": "subscription.cancelled", "kid": self.kid, "plan": "team"})
        self.assertTrue(out["applied"])
        meta = self.store.key_meta(self.kid)
        self.assertEqual(meta["plan"], "free")
        self.assertEqual(meta["monthly_allowance"], billing.PLANS["free"]["allowance"])

    def test_irrelevant_event_is_ignored(self):
        out = billing.apply_event(self.store, {
            "event": "subscription.pending", "kid": self.kid, "plan": "pro"})
        self.assertFalse(out["applied"])
        self.assertEqual(self.store.key_meta(self.kid)["plan"], "free")

    def test_billing_events_are_recorded(self):
        billing.apply_event(self.store, {
            "event": "subscription.activated", "kid": self.kid, "plan": "pro",
            "subscription_id": "sub_9"})
        ev = self.store.latest_event(self.kid, "billing_activated")
        self.assertEqual(ev["plan"], "pro")


class TestCheckout(unittest.TestCase):
    def test_subscription_carries_kid_and_plan_in_notes(self):
        seen = {}

        def transport(method, url, json=None, headers=None):
            seen["url"] = url
            seen["body"] = json
            seen["auth"] = headers["Authorization"]
            return _Resp(200, {"id": "sub_1", "status": "created"})

        os.environ["RAZORPAY_PLAN_ID_PRO"] = "plan_pro_123"
        p = _provider(transport=transport)
        out = p.create_subscription("kid_7", "pro", total_count=6)
        self.assertEqual(out["id"], "sub_1")
        self.assertTrue(seen["url"].endswith("/subscriptions"))
        self.assertEqual(seen["body"]["plan_id"], "plan_pro_123")
        self.assertEqual(seen["body"]["notes"], {"kid": "kid_7", "plan": "pro"})
        self.assertEqual(seen["body"]["total_count"], 6)
        self.assertTrue(seen["auth"].startswith("Basic "))
        os.environ.pop("RAZORPAY_PLAN_ID_PRO", None)

    def test_missing_plan_id_is_a_clear_error(self):
        os.environ.pop("RAZORPAY_PLAN_ID_PRO", None)
        with self.assertRaises(billing.BillingError):
            billing.plan_id_for("pro")

    def test_generic_plan_id_is_never_reused(self):
        # a shared account holds plans for other products; reusing whichever
        # one is in the environment would bill for the wrong product
        os.environ["RAZORPAY_PLAN_ID"] = "plan_from_another_product"
        os.environ.pop("RAZORPAY_PLAN_ID_PRO", None)
        try:
            with self.assertRaises(billing.BillingError):
                billing.plan_id_for("pro")
        finally:
            os.environ.pop("RAZORPAY_PLAN_ID", None)

    def test_api_error_surfaces(self):
        os.environ["RAZORPAY_PLAN_ID_PRO"] = "plan_pro_123"
        p = _provider(transport=lambda *a, **k: _Resp(400, {}, "bad plan"))
        with self.assertRaises(billing.BillingError):
            p.create_subscription("kid_7", "pro")
        os.environ.pop("RAZORPAY_PLAN_ID_PRO", None)


class TestProviderSelection(unittest.TestCase):
    def test_ambient_credentials_alone_do_not_arm_billing(self):
        # live keys in the environment must NOT enable the payment endpoints
        os.environ["RAZORPAY_KEY_ID"] = "rzp_live_x"
        os.environ["RAZORPAY_KEY_SECRET"] = "s"
        os.environ["RAZORPAY_WEBHOOK_SECRET"] = "w"
        try:
            self.assertIsInstance(billing.get_provider(),
                                  billing.NullProvider)
        finally:
            for k in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET",
                      "RAZORPAY_WEBHOOK_SECRET"):
                os.environ.pop(k, None)

    def test_unconfigured_is_null_and_refuses(self):
        os.environ.pop("RAZORPAY_KEY_ID", None)
        os.environ.pop("RAZORPAY_KEY_SECRET", None)
        os.environ.pop("RAZORPAY_WEBHOOK_SECRET", None)
        p = billing.get_provider("razorpay")
        self.assertIsInstance(p, billing.NullProvider)
        with self.assertRaises(billing.BillingError):
            p.create_subscription("kid_1", "pro")

    def test_unknown_provider_is_clear(self):
        with self.assertRaises(billing.BillingError):
            billing.get_provider("stripe")


if __name__ == "__main__":
    unittest.main()


class TestWebhookEndpoint(unittest.TestCase):
    """End-to-end over the real FastAPI surface: signature gate first, then
    the plan moves. Set the env so get_provider() actually returns Razorpay."""

    def setUp(self):
        os.environ["AGENTCHECK_BILLING"] = "razorpay"
        os.environ["RAZORPAY_KEY_ID"] = "rzp_test_key"
        os.environ["RAZORPAY_KEY_SECRET"] = "rzp_test_secret"
        os.environ["RAZORPAY_WEBHOOK_SECRET"] = SECRET
        from fastapi.testclient import TestClient
        from agentcheck.proxy import create_app
        self.store = Store(Path(tempfile.mkdtemp()) / "w.db")
        self.raw = self.store.create_key("acct")
        self.kid = self.store.lookup_key(self.raw)["kid"]
        self.client = TestClient(create_app(self.store, default_judge="stub"))

    def tearDown(self):
        for k in ("AGENTCHECK_BILLING", "RAZORPAY_KEY_ID",
                  "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"):
            os.environ.pop(k, None)

    def test_plans_endpoint_is_public_and_honest(self):
        r = self.client.get("/v1/billing/plans")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        names = [p["name"] for p in d["plans"]]
        self.assertEqual(names, ["free", "pro", "team", "enterprise"])
        ent = next(p for p in d["plans"] if p["name"] == "enterprise")
        self.assertIsNone(ent["price_inr"], "enterprise is not a monthly SKU")
        self.assertEqual(d["provider"], "razorpay")
        self.assertTrue(d["configured"])

    def test_valid_webhook_upgrades_the_key(self):
        body = _sub_event(kid=self.kid, plan="pro")
        r = self.client.post("/v1/billing/webhook", content=body,
                             headers={"X-Razorpay-Signature": _sign(body)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["applied"], r.json())
        meta = self.store.key_meta(self.kid)
        self.assertEqual(meta["plan"], "pro")
        self.assertEqual(meta["monthly_allowance"],
                         billing.PLANS["pro"]["allowance"])

    def test_bad_signature_is_401_and_changes_nothing(self):
        body = _sub_event(kid=self.kid, plan="pro")
        r = self.client.post("/v1/billing/webhook", content=body,
                             headers={"X-Razorpay-Signature": "0" * 64})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.store.key_meta(self.kid)["plan"], "free")

    def test_no_signature_header_is_401(self):
        r = self.client.post("/v1/billing/webhook", content=_sub_event())
        self.assertEqual(r.status_code, 401)

    def test_checkout_without_credentials_is_503_not_a_fake_success(self):
        os.environ.pop("RAZORPAY_KEY_SECRET", None)
        h = {"Authorization": f"Bearer {self.raw}"}
        r = self.client.post("/v1/billing/checkout", json={"plan": "pro"},
                             headers=h)
        self.assertEqual(r.status_code, 503, r.text)

    def test_checkout_rejects_unknown_plan(self):
        h = {"Authorization": f"Bearer {self.raw}"}
        r = self.client.post("/v1/billing/checkout",
                             json={"plan": "enterprise-unlimited"}, headers=h)
        self.assertEqual(r.status_code, 422)
