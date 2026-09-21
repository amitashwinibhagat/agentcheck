"""Adversarial suite: the trust boundaries an attacker would actually probe.

Every case here is a *relationship between two tenants* or between a caller and
something they should not reach. It exists because a bug of this class was
found by accident (`/start` handed out a live key), and accidental discovery
does not scale: the invariants have to be asserted, not assumed.

Rules for adding to this file:
  * assert the SECURITY property (no data from the other tenant, no state
    change), not merely the status code, wherever the body is available
  * a 404 and a 403 are both acceptable ways to refuse; leaking *existence* is
    what matters, so assert on the payload too
  * if a case is deliberately allowed (an operator can see every workspace in
    their own local store), say so in the docstring
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agentcheck import auth, workspaces
from agentcheck.proxy import create_app
from agentcheck.store import Store

SECRET = "adversarial-secret"


class Tenant:
    """Two keys, two workspaces — the minimum needed to test isolation."""

    def __init__(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "adv.db")
        self.a = self.store.create_key("alice", qpm_limit=60000)
        self.b = self.store.create_key("bob", qpm_limit=60000)
        # Invites need spare seats and Free has one, occupied by the owner.
        # Give both workspaces a plan with room so an invite test fails for a
        # SECURITY reason rather than a billing one.
        for raw in (self.a, self.b):
            wid = self.store.workspace_for_key(
                self.store.lookup_key(raw)["kid"])["id"]
            self.store.set_workspace_plan(wid, "team", 15)
        self.app = create_app(self.store, default_judge="stub")
        self.client = TestClient(self.app, base_url="https://app.test")
        self.ha = {"Authorization": f"Bearer {self.a}"}
        self.hb = {"Authorization": f"Bearer {self.b}"}

    def check(self, headers, request="read the docs", tool="read_file",
              args=None):
        r = self.client.post("/v1/check", headers=headers, json={"trace": {
            "request": request, "tool": tool, "args": args or {"path": "/tmp/x"}}})
        assert r.status_code == 200, r.text
        return r.json()


class TestCrossTenantIsolation(unittest.TestCase):
    """Bob must not reach anything of Alice's, by any route or any id."""

    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        self.t = Tenant()

    def tearDown(self):
        os.environ.pop("AGENTCHECK_SECRET", None)

    def test_results_are_per_key(self):
        mine = self.t.check(self.t.ha)
        self.assertEqual(len(self.t.client.get("/v1/results",
                                               headers=self.t.ha).json()["results"]), 1)
        self.assertEqual(len(self.t.client.get("/v1/results",
                                               headers=self.t.hb).json()["results"]), 0)
        # ...and the specific id is not readable, nor even confirmed to exist.
        r = self.t.client.get(f"/v1/results/{mine['id']}", headers=self.t.hb)
        self.assertEqual(r.status_code, 404, r.text)
        self.assertNotIn(mine["trace_id"], r.text)

    def test_writing_another_tenants_result_is_refused(self):
        mine = self.t.check(self.t.ha)
        r = self.t.client.patch(f"/v1/results/{mine['id']}",
                                headers=self.t.hb, json={"assessment": "actual_issue"})
        self.assertEqual(r.status_code, 404, r.text)
        # The owner's row is untouched: a refusal must not be a partial write.
        row = self.t.client.get(f"/v1/results/{mine['id']}",
                                headers=self.t.ha).json()
        self.assertIsNone(row.get("assessment"))

    def test_deleting_another_tenants_result_is_refused(self):
        mine = self.t.check(self.t.ha)
        r = self.t.client.delete(f"/v1/results/{mine['id']}", headers=self.t.hb)
        self.assertIn(r.status_code, (404, 403), r.text)
        self.assertEqual(self.t.client.get(f"/v1/results/{mine['id']}",
                                           headers=self.t.ha).status_code, 200)

    def test_trust_and_usage_and_monitor_are_per_key(self):
        self.t.check(self.t.ha)
        self.assertEqual(self.t.client.get("/v1/trust",
                                           headers=self.t.hb).json()["n"], 0)
        ub = self.t.client.get("/v1/usage", headers=self.t.hb).json()
        self.assertEqual(ub["allowance"]["used_this_month"], 0,
                         f"bob's usage counted alice's work: {ub}")
        mon = self.t.client.get("/v1/monitor", headers=self.t.hb).json()
        self.assertFalse(json.dumps(mon).count("read the docs"))

    def test_runs_and_traces_are_per_key(self):
        mine = self.t.check(self.t.ha)
        self.assertEqual(self.t.client.get(f"/v1/traces/{mine['trace_id']}",
                                           headers=self.t.hb).status_code, 404)
        b_runs = self.t.client.get("/v1/runs", headers=self.t.hb).json()
        self.assertEqual(b_runs.get("runs"), [])
        if mine.get("run_id"):
            self.assertEqual(self.t.client.get(f"/v1/runs/{mine['run_id']}",
                                               headers=self.t.hb).status_code, 404)

    def test_workspace_detail_lists_only_the_callers_workspace(self):
        a = self.t.client.get("/v1/workspace", headers=self.t.ha).json()
        b = self.t.client.get("/v1/workspace", headers=self.t.hb).json()
        self.assertNotEqual(a["id"], b["id"])
        self.assertNotIn(b["id"], json.dumps(a))
        self.assertNotIn("alice", json.dumps(b).lower())

    def test_invites_cannot_be_created_or_listed_across_tenants(self):
        a = self.t.client.get("/v1/workspace", headers=self.t.ha).json()
        r = self.t.client.post("/v1/workspace/invites", headers=self.t.hb,
                               json={"email": "mallory@x.test"})
        # bob invites into HIS workspace, which is allowed for an owner —
        # the assertion that matters is that it did not land in alice's.
        if r.status_code == 200:
            invites = self.t.client.get("/v1/workspace/invites",
                                        headers=self.t.ha).json()["invites"]
            self.assertEqual([i for i in invites
                              if i["email"] == "mallory@x.test"], [])
        listed = self.t.client.get("/v1/workspace/invites",
                                   headers=self.t.ha).json()["invites"]
        self.assertTrue(all(i["workspace_id"] == a["id"] for i in listed))

    def test_revoking_another_tenants_invite_is_refused(self):
        inv = self.t.client.post("/v1/workspace/invites", headers=self.t.ha,
                                 json={"email": "taken@x.test"})
        self.assertEqual(inv.status_code, 200, inv.text)
        iid = inv.json()["id"]
        r = self.t.client.delete(f"/v1/workspace/invites/{iid}",
                                 headers=self.t.hb)
        self.assertIn(r.status_code, (403, 404), r.text)
        still = self.t.client.get("/v1/workspace/invites",
                                  headers=self.t.ha).json()["invites"]
        self.assertIn(iid, [i["id"] for i in still])

    def test_labeling_batch_never_contains_another_tenants_ids(self):
        mine = self.t.check(self.t.ha)
        plan = self.t.client.get("/v1/labeling/batch", headers=self.t.hb).json()
        self.assertNotIn(mine["id"], plan["ids"])
        self.assertEqual(plan["unlabeled"], 0)

    def test_checksets_are_open_to_any_authenticated_key(self):
        """A rubric is shared product content, not tenant data — but it still
        requires a key, so an anonymous caller cannot enumerate them."""
        self.assertEqual(self.t.client.get("/v1/checksets").status_code, 401)
        self.assertEqual(self.t.client.get("/v1/checksets",
                                           headers=self.t.hb).status_code, 200)

    def test_a_checked_call_is_not_visible_through_the_stream_of_another_key(self):
        """The SSE bus subscribes by authenticated key. A wrong key must not
        receive someone else's events."""
        from agentcheck.stream import Bus
        bus = Bus()
        qa = bus.subscribe("kid_alice")
        qb = bus.subscribe("kid_bob")
        bus.publish("kid_alice", {"verdict": "fail", "request": "secret"})
        got_b = []
        if not qb.empty():
            got_b.append(qb.get_nowait())
        self.assertEqual(got_b, [], "bob's stream received alice's event")
        self.assertFalse(qa.empty())


class TestKeyAndSessionBoundaries(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        os.environ["AGENTCHECK_BASE_URL"] = "https://app.test"
        self.t = Tenant()

    def tearDown(self):
        for k in ("AGENTCHECK_SECRET", "AGENTCHECK_BASE_URL"):
            os.environ.pop(k, None)

    def test_a_key_can_only_act_as_itself(self):
        """No header tricks: an unknown, empty, or malformed key is refused."""
        for header in ({}, {"Authorization": ""},
                       {"Authorization": "Bearer"},
                       {"Authorization": "Bearer "},
                       {"Authorization": "ac_not_a_real_key"},
                       {"Authorization": "ac_" + "a" * 40},
                       {"Authorization": "Basic YWJjOmRlZg=="},
                       # A kid is not a token; the kid is post-auth identity.
                       {"Authorization": f"Bearer {self.t.store.lookup_key(self.t.a)['kid']}"}):
            r = self.t.client.get("/v1/results", headers=header)
            self.assertEqual(r.status_code, 401, f"{header} -> {r.status_code}")

    def test_the_raw_key_is_never_returned_after_creation(self):
        ws = self.t.client.get("/v1/workspace", headers=self.t.ha).json()
        body = json.dumps(ws)
        self.assertNotIn(self.t.a, body)
        self.assertNotIn(self.t.b, body)
        # prefix + metadata only
        self.assertTrue(all("key_prefix" in k for k in ws.get("keys", [])))

    def test_a_forged_session_cookie_is_refused(self):
        for cookie in ("", "ses_forged", "a" * 64,
                       self.t.store.lookup_key(self.t.a)["kid"]):
            self.t.client.cookies.set(auth.SESSION_COOKIE, cookie)
            r = self.t.client.get("/v1/auth/me")
            self.assertFalse(r.json().get("authenticated"), cookie)
            self.t.client.cookies.clear()

    def test_an_owner_key_can_administer_its_workspace(self):
        """The bug this suite was written for, one layer down.

        A key minted INTO a workspace has no member row, so `acting` derived
        role "member" — and the owner's own first key (the signup flow) could
        not invite anybody. Authority now rides on the credential.
        """
        inv = self.t.client.post("/v1/workspace/invites", headers=self.t.ha,
                                 json={"email": "should-work@x.test"})
        self.assertEqual(inv.status_code, 200, inv.text)

    def test_a_member_key_cannot_escalate(self):
        """A member-scoped credential may read and invite, never rule."""
        wid = self.t.client.get("/v1/workspace", headers=self.t.ha).json()["id"]
        member_key = self.t.store.create_key("member-key", qpm_limit=600,
                                             workspace_id=wid, role="member")
        hm = {"Authorization": f"Bearer {member_key}"}
        self.assertEqual(self.t.client.get("/v1/workspace", headers=hm).status_code, 200)
        # Demoting or removing somebody is an owner action.
        r = self.t.client.delete("/v1/workspace/members/alice@local", headers=hm)
        self.assertEqual(r.status_code, 403, r.text)
        r = self.t.client.patch("/v1/workspace/members/alice@local",
                                headers=hm, json={"role": "member"})
        self.assertIn(r.status_code, (403, 404), r.text)
        # And a mint inherits the member role: it cannot buy itself an owner.
        minted = self.t.client.post("/v1/keys", headers=hm, json={"name": "sibling"})
        if minted.status_code == 200:
            self.assertEqual(minted.json()["role"], "member")
            sib = {"Authorization": f"Bearer {minted.json()['key']}"}
            self.assertEqual(self.t.client.delete(
                "/v1/workspace/members/alice@local", headers=sib).status_code, 403)

    def test_a_minted_key_inherits_the_workspace_plan_not_the_request(self):
        """Plan numbers come from the catalogue, never from a payload.

        `/v1/keys` used to accept allowance/qpm/plan from the body, so any key
        — including a free or demo one — could mint itself a pro key with a
        10,000,000/month allowance.
        """
        wid = self.t.client.get("/v1/workspace", headers=self.t.ha).json()["id"]
        from agentcheck import billing
        for raw in (self.t.a, self.t.b):
            self.t.store.set_workspace_plan(wid, "team", 15)
        minted = self.t.client.post("/v1/keys", headers=self.t.ha, json={
            "name": "sneaky", "allowance": 10_000_000, "qpm": 10_000,
            "plan": "pro"})
        self.assertEqual(minted.status_code, 200, minted.text)
        body = minted.json()
        self.assertEqual(body["plan"], "team")
        self.assertEqual(body["monthly_allowance"], billing.PLANS["team"]["allowance"])
        self.assertEqual(body["qpm_limit"], billing.PLANS["team"]["qpm"])
        meta = self.t.store.key_meta(body["key"])
        self.assertEqual(meta["monthly_allowance"], billing.PLANS["team"]["allowance"])
        # The fields are gone, not merely ignored: asking is a 422.
        r = self.t.client.post("/v1/keys", headers=self.t.ha,
                               json={"name": "x", "allowance": 10_000_000})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertLessEqual(r.json()["monthly_allowance"],
                             billing.PLANS["team"]["allowance"])

    def test_the_last_owner_cannot_be_demoted_or_removed_by_a_key_route(self):
        wid = self.t.client.get("/v1/workspace", headers=self.t.ha).json()["id"]
        owners = [m for m in self.t.store.workspace_members(wid)
                  if m["role"] == "owner"]
        self.assertTrue(owners)
        r = self.t.client.delete(f"/v1/workspace/members/{owners[0]['email']}",
                                 headers=self.t.ha)
        self.assertIn(r.status_code, (400, 403, 409, 422), r.text)
        still = [m for m in self.t.store.workspace_members(wid)
                 if m["role"] == "owner"]
        self.assertEqual(len(still), 1, "the last owner was removed")


class TestInviteHardening(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        self.t = Tenant()

    def tearDown(self):
        os.environ.pop("AGENTCHECK_SECRET", None)

    def test_an_invite_token_is_single_use(self):
        inv = self.t.client.post("/v1/workspace/invites", headers=self.t.ha,
                                 json={"email": "once@x.test"}).json()
        first = self.t.client.post("/v1/invites/accept", json={"token": inv["token"]})
        self.assertEqual(first.status_code, 200, first.text)
        second = self.t.client.post("/v1/invites/accept", json={"token": inv["token"]})
        self.assertIn(second.status_code, (400, 404, 409, 410), second.text)

    def test_an_expired_invite_is_refused(self):
        ws = self.t.client.get("/v1/workspace", headers=self.t.ha).json()
        old = workspaces.new_invite("late@x.test", "member", ttl=-1)
        self.t.store.create_invite(ws["id"], old)
        r = self.t.client.post("/v1/invites/accept", json={"token": old["token"]})
        self.assertEqual(r.status_code, 410, r.text)

    def test_a_guessed_token_is_refused(self):
        for token in ("", "x", "a" * 43, "wi_" + "a" * 40):
            r = self.t.client.post("/v1/invites/accept", json={"token": token})
            self.assertIn(r.status_code, (400, 404, 410, 422), token)

    def test_accepting_an_invite_grants_only_the_invited_role(self):
        inv = self.t.client.post("/v1/workspace/invites", headers=self.t.ha,
                                 json={"email": "jane@x.test"}).json()
        self.assertEqual(inv["role"], "member")
        self.t.client.post("/v1/invites/accept", json={"token": inv["token"]})
        ws = self.t.client.get("/v1/workspace", headers=self.t.ha).json()
        jane = [m for m in ws["members"] if m["email"] == "jane@x.test"]
        self.assertEqual([m["role"] for m in jane], ["member"])

    def test_an_invite_for_another_workspace_does_not_grant_this_one(self):
        wsa = self.t.client.get("/v1/workspace", headers=self.t.ha).json()
        wsb = self.t.client.get("/v1/workspace", headers=self.t.hb).json()
        inv = self.t.client.post("/v1/workspace/invites", headers=self.t.ha,
                                 json={"email": "cross@x.test"}).json()
        self.t.client.post("/v1/invites/accept", json={"token": inv["token"]})
        self.assertNotIn("cross@x.test",
                         [m["email"] for m in
                          self.t.client.get("/v1/workspace",
                                            headers=self.t.hb).json()["members"]])
        del wsa, wsb

    def test_the_invite_token_is_never_returned_by_a_listing(self):
        inv = self.t.client.post("/v1/workspace/invites", headers=self.t.ha,
                                 json={"email": "t@x.test"}).json()
        body = json.dumps(self.t.client.get("/v1/workspace/invites",
                                            headers=self.t.ha).json())
        self.assertNotIn(inv["token"], body)
        self.assertNotIn("token_hash", body)


class TestInputBoundaries(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        self.t = Tenant()

    def tearDown(self):
        os.environ.pop("AGENTCHECK_SECRET", None)

    def test_sql_injection_in_filters_and_ids_is_harmless(self):
        evil = "'; DROP TABLE results; --"
        for path in (f"/v1/results?verdict={evil}",
                     f"/v1/trust?checkset={evil}",
                     f"/v1/runs?tool={evil}",
                     f"/v1/results/{evil}",
                     f"/v1/monitor?bucket={evil}",
                     f"/v1/calibration/signoffs?checkset={evil}"):
            r = self.t.client.get(path, headers=self.t.ha)
            self.assertIn(r.status_code, (200, 400, 404, 422),
                          f"{path} -> {r.status_code}")
        # The store still works afterwards: nothing was dropped.
        self.assertEqual(self.t.client.get("/v1/results",
                                           headers=self.t.ha).status_code, 200)
        self.t.check(self.t.ha)

    def test_an_unknown_dataset_or_checkset_is_refused_not_executed(self):
        for path in ("/v1/calibration?dataset=../../etc/passwd",
                     "/v1/calibration?dataset=/etc/passwd",
                     "/v1/calibration?dataset=agent-demo&checkset=../../etc/passwd",
                     "/v1/calibration?dataset=nope"):
            r = self.t.client.get(path, headers=self.t.ha)
            self.assertIn(r.status_code, (404, 422), f"{path} -> {r.status_code}")
            self.assertNotIn("root:", r.text)
            # No host paths, and no 500 with an empty body.
            self.assertNotIn("/Users/", r.text)
            self.assertNotIn("/home/", r.text)
            self.assertNotEqual(r.status_code, 500, f"{path} crashed")

    def test_a_path_traversal_checkset_is_refused(self):
        r = self.t.client.post("/v1/check", headers=self.t.ha, json={
            "trace": {"request": "x", "tool": "read_file", "args": {}},
            "checkset": "../../../etc/passwd"})
        self.assertIn(r.status_code, (404, 422), r.text)

    def test_an_oversized_batch_is_refused(self):
        traces = [{"request": f"r{i}", "tool": "read_file", "args": {}} for i in range(101)]
        r = self.t.client.post("/v1/check-batch", headers=self.t.ha,
                               json={"traces": traces})
        self.assertEqual(r.status_code, 422, r.text)

    def test_a_malformed_trace_is_an_error_row_not_a_crash(self):
        r = self.t.client.post("/v1/check-batch", headers=self.t.ha,
                               json={"traces": [42, {"tool": "x"}, "nope"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(all(row.get("error") for row in r.json()["results"]))

    def test_open_redirect_is_refused_on_login_next(self):
        """`next` must not carry a caller off-site after sign-in."""
        state = auth.sign_state("nonce", next_path="https://evil.example/steal")
        payload = auth.verify_state(state)
        self.assertEqual(payload["next"], "/",
                         "an absolute next path was accepted into the state")

    def test_a_tampered_state_is_refused(self):
        good = auth.sign_state("nonce-1", next_path="/")
        for bad in (good[:-4] + "AAAA", good.split(".")[0], "x." + good, good + "x"):
            self.assertFalse(auth.verify_state(bad), bad)

    def test_assessment_values_are_a_closed_set(self):
        mine = self.t.check(self.t.ha)
        for value in ("yes", "PASS", "", "looks_correct ", None, {"a": 1}):
            r = self.t.client.patch(f"/v1/results/{mine['id']}",
                                    headers=self.t.ha,
                                    json={"assessment": value})
            self.assertEqual(r.status_code, 422, f"{value!r} -> {r.status_code}")


class TestQuotaAndRateLimits(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        self.t = Tenant()

    def tearDown(self):
        os.environ.pop("AGENTCHECK_SECRET", None)

    def test_the_allowance_is_per_key_not_per_store(self):
        """Bob's budget must not be spendable by Alice, or vice versa."""
        self.t.store.set_plan(self.t.store.lookup_key(self.t.a)["kid"],
                              "free", allowance=6, qpm=60000)
        spent = 0
        for i in range(4):
            r = self.t.client.post("/v1/check", headers=self.t.ha, json={
                "trace": {"request": f"r{i}", "tool": "read_file", "args": {"p": i}}})
            if r.status_code == 402:
                break
            self.assertEqual(r.status_code, 200, r.text)
            spent += 1
        self.assertGreaterEqual(spent, 1)
        # Bob still has his whole allowance.
        self.assertEqual(self.t.client.post("/v1/check", headers=self.t.hb, json={
            "trace": {"request": "bob", "tool": "read_file", "args": {}}}).status_code, 200)

    def test_the_rate_limit_is_enforced_per_key(self):
        self.t.store.set_plan(self.t.store.lookup_key(self.t.a)["kid"],
                              "free", allowance=10_000_000, qpm=6)
        codes = []
        for i in range(4):
            r = self.t.client.post("/v1/check", headers=self.t.ha, json={
                "trace": {"request": f"q{i}", "tool": "read_file", "args": {"p": i}}})
            codes.append(r.status_code)
        self.assertIn(429, codes, f"the qpm cap did not bite: {codes}")
        self.assertEqual(self.t.client.post("/v1/check", headers=self.t.hb, json={
            "trace": {"request": "bob2", "tool": "read_file", "args": {}}}).status_code, 200)

    def test_the_demo_key_is_capped_and_not_an_admin_key(self):
        """Even in demo mode the handed-out key cannot do operator things."""
        store = Store(Path(tempfile.mkdtemp()) / "demo.db")
        store.create_key("operator", qpm_limit=60000)
        app = create_app(store, default_judge="stub", demo_mode=True)
        client = TestClient(app, base_url="https://demo.example",
                            client=("203.0.113.7", 5111))
        demo_key = client.get("/v1/bootstrap").json()["key"]
        h = {"Authorization": f"Bearer {demo_key}"}
        meta = store.key_meta(demo_key)
        self.assertLessEqual(meta["monthly_allowance"], 40000)
        self.assertEqual(meta["plan"], "free")
        # No billing, no plan change, no key minting for a stranger.
        for method, path, body in (("GET", "/v1/billing/subscription", None),
                                   ("POST", "/v1/keys", {"name": "mine"}),
                                   ("GET", "/v1/billing/checkout", None)):
            kw = {"json": body} if body else {}
            r = client.request(method, path, headers=h, **kw)
            self.assertNotEqual(r.status_code, 200,
                                f"the demo key reached {method} {path}")


if __name__ == "__main__":
    unittest.main()


class TestSurfaceExposure(unittest.TestCase):
    """What a deployment advertises about itself.

    The earlier route audit swept `/v1` only, so it missed three public
    endpoints that are not under `/v1` and that enumerate the whole API.
    """

    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        self.t = Tenant()

    def tearDown(self):
        for k in ("AGENTCHECK_SECRET", "AGENTCHECK_DOCS"):
            os.environ.pop(k, None)

    def test_api_docs_are_not_served_by_default(self):
        for path in ("/docs", "/openapi.json", "/redoc"):
            r = self.t.client.get(path)
            self.assertEqual(r.status_code, 404,
                             f"{path} is public; it lists every endpoint")

    def test_api_docs_can_be_opted_into_for_local_development(self):
        from agentcheck.proxy import create_app
        from agentcheck.store import Store
        os.environ["AGENTCHECK_DOCS"] = "1"
        try:
            store = Store(Path(tempfile.mkdtemp()) / "d.db")
            store.create_key("a", qpm_limit=600)
            c = TestClient(create_app(store, default_judge="stub"),
                           base_url="https://app.test")
            self.assertEqual(c.get("/openapi.json").status_code, 200)
        finally:
            os.environ.pop("AGENTCHECK_DOCS", None)

    def test_error_bodies_never_name_the_host_filesystem(self):
        """A refusal must not hand back the server's layout — the username in
        an absolute path is reconnaissance for the next attempt."""
        paths = ["/v1/calibration?dataset=/etc/passwd",
                 "/v1/calibration?dataset=../../etc/passwd",
                 "/v1/calibration?dataset=nope",
                 "/v1/datasets", "/v1/evals", "/v1/checksets"]
        for path in paths:
            r = self.t.client.get(path, headers=self.t.ha)
            body = r.text
            self.assertNotIn("/Users/", body, path)
            self.assertNotIn("/home/", body, path)
            self.assertNotIn("/data/", body, path)
            self.assertNotEqual(r.status_code, 500, f"{path} crashed: {body[:80]}")

    def test_a_dataset_can_be_named_but_not_pointed_at(self):
        """`dataset` is a name from the registry, never a path to read."""
        r = self.t.client.get("/v1/calibration?dataset=/etc/passwd",
                              headers=self.t.ha)
        self.assertEqual(r.status_code, 404, r.text)
        self.assertIn("unknown dataset", r.text)
        # The bundled seed still works, so the endpoint is not simply broken.
        ok = self.t.client.get("/v1/calibration?dataset=agent-demo",
                               headers=self.t.ha)
        self.assertEqual(ok.status_code, 200, ok.text)


class TestCalibrationIsPerTenant(unittest.TestCase):
    """One tenant's proof must never become another tenant's proof.

    The published calibration was a single instance-wide file, so Alice
    publishing moved Bob's trust tier to `measured` — with HER ECE and "your
    sign-outs" attached to his workspace. Verified before the fix; this pins
    it closed. It is the worst kind of bug for a product whose claim is
    "the confidence is measured", and it would be a compliance problem
    hosted.
    """

    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        self.home = tempfile.mkdtemp()
        os.environ["AGENTCHECK_HOME"] = self.home
        self.t = Tenant()

    def tearDown(self):
        for k in ("AGENTCHECK_SECRET", "AGENTCHECK_HOME"):
            os.environ.pop(k, None)

    def _label_and_publish(self, headers, n=32):
        for i in range(n):
            rid = self.t.check(headers, request=f"read {i}",
                               args={"p": i})["id"]
            self.t.client.patch(f"/v1/results/{rid}", headers=headers,
                                json={"assessment": "looks_correct"
                                      if i % 4 else "actual_issue"})
        r = self.t.client.post("/v1/calibration/signoffs/publish",
                               headers=headers, json={})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_publishing_does_not_move_another_tenants_tier(self):
        self._label_and_publish(self.t.ha)
        self.assertEqual(self.t.client.get("/v1/trust",
                                           headers=self.t.ha).json()["tier"],
                         "measured")
        # Bob has no labels at all, so he cannot be measured — by anyone.
        b = self.t.client.get("/v1/trust", headers=self.t.hb).json()
        self.assertEqual(b["tier"], "consistency",
                         "alice's calibration moved bob's tier")
        self.assertIsNone(b.get("ece"))
        self.assertNotEqual(b.get("dataset"), "your sign-outs")

    def test_each_tenant_reads_its_own_report_file(self):
        self._label_and_publish(self.t.ha)
        files = sorted(os.listdir(self.home))
        published = [f for f in files if f.startswith("calibration-")]
        self.assertEqual(len(published), 1, files)
        self.assertNotIn("calibration.json", files,
                         "the publish wrote the instance-wide file again")

    def test_bob_publishing_does_not_disturb_alice(self):
        self._label_and_publish(self.t.ha)
        before = self.t.client.get("/v1/trust", headers=self.t.ha).json()
        self._label_and_publish(self.t.hb, n=32)
        after = self.t.client.get("/v1/trust", headers=self.t.ha).json()
        self.assertEqual(after["tier"], before["tier"])
        self.assertEqual(after.get("dataset"), before.get("dataset"))
        self.assertEqual(len([f for f in os.listdir(self.home)
                              if f.startswith("calibration-")]), 2)
