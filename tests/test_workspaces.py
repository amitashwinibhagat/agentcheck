"""Workspace, seat, role and invite contracts.

The properties worth protecting:
  * every key belongs to a workspace (existing installs migrate, new keys too)
  * a seat limit cannot be exceeded, and a PENDING invite reserves a seat
  * invite tokens are stored hashed and burn exactly once
  * the last owner cannot be removed or demoted
"""

import tempfile
import unittest
from pathlib import Path

from agentcheck import workspaces
from agentcheck.store import Store

def _client():
    from fastapi.testclient import TestClient
    from agentcheck.proxy import create_app
    store = Store(Path(tempfile.mkdtemp()) / "ws.db")
    key = store.create_key("owner", qpm_limit=60000)
    app = create_app(store, default_judge="stub")
    return TestClient(app), key, store


class TestMigration(unittest.TestCase):
    def test_existing_key_gets_a_workspace_at_open(self):
        import sqlite3
        p = Path(tempfile.mkdtemp()) / "old.db"
        s = Store(p)
        raw = s.create_key("acct")
        # simulate a pre-workspace database: drop the link
        c = sqlite3.connect(p)
        c.execute("UPDATE api_keys SET workspace_id = NULL")
        c.commit()
        c.close()
        reopened = Store(p)  # migration runs on open
        ws = reopened.workspace_for_key(reopened.lookup_key(raw)["kid"])
        self.assertIsNotNone(ws)
        self.assertEqual(ws["members"][0]["role"], "owner")

    def test_new_key_gets_a_workspace_immediately(self):
        s = Store(Path(tempfile.mkdtemp()) / "n.db")
        raw = s.create_key("acct")
        ws = s.workspace_for_key(s.lookup_key(raw)["kid"])
        self.assertIsNotNone(ws)
        self.assertEqual(ws["seat_limit"], workspaces.seat_limit("free"))


class TestWorkspaceApi(unittest.TestCase):
    def test_detail_reports_members_seats_and_plan(self):
        client, key, _ = _client()
        h = {"Authorization": f"Bearer {key}"}
        r = client.get("/v1/workspace", headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["plan"], "free")
        self.assertEqual(d["role"], "owner")
        self.assertEqual(d["seats"]["used"], 1)
        self.assertEqual(d["seats"]["limit"], workspaces.seat_limit("free"))

    def test_requires_auth(self):
        client, _, _ = _client()
        self.assertEqual(client.get("/v1/workspace").status_code, 401)


class TestSeatsAndInvites(unittest.TestCase):
    def test_free_plan_cannot_invite_a_second_seat(self):
        client, key, _ = _client()
        h = {"Authorization": f"Bearer {key}"}
        r = client.post("/v1/workspace/invites",
                        json={"email": "dev@acme.test"}, headers=h)
        self.assertEqual(r.status_code, 402, r.text)
        self.assertIn("seat", r.text)

    def test_upgraded_plan_can_invite_and_token_returns_once(self):
        client, key, store = _client()
        h = {"Authorization": f"Bearer {key}"}
        ws = store.workspace_for_key(store.lookup_key(key)["kid"])
        store.set_workspace_plan(ws["id"], "pro",
                                 workspaces.seat_limit("pro"))
        r = client.post("/v1/workspace/invites",
                        json={"email": "Dev@Acme.test", "role": "member"},
                        headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["email"], "dev@acme.test")  # normalised
        self.assertTrue(d["token"].startswith("inv_"))
        # the token appears in the create response and nowhere else
        listed = client.get("/v1/workspace/invites", headers=h).json()["invites"]
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token", listed[0])
        self.assertNotIn(d["token"], str(listed))

    def test_pending_invite_reserves_a_seat(self):
        client, key, store = _client()
        h = {"Authorization": f"Bearer {key}"}
        ws = store.workspace_for_key(store.lookup_key(key)["kid"])
        # pro = 5 seats: 1 owner + 4 invites fills it
        store.set_workspace_plan(ws["id"], "pro", 2)  # shrink to 2 to test
        self.assertEqual(
            client.post("/v1/workspace/invites",
                        json={"email": "a@x.test"}, headers=h).status_code, 200)
        # owner (1) + pending (1) == limit 2 -> refused
        r = client.post("/v1/workspace/invites",
                        json={"email": "b@x.test"}, headers=h)
        self.assertEqual(r.status_code, 402, r.text)

    def test_duplicate_member_is_a_conflict(self):
        client, key, store = _client()
        h = {"Authorization": f"Bearer {key}"}
        ws = store.workspace_for_key(store.lookup_key(key)["kid"])
        store.set_workspace_plan(ws["id"], "pro", 5)
        store.add_member(ws["id"], "already@x.test", "member")
        r = client.post("/v1/workspace/invites",
                        json={"email": "already@x.test"}, headers=h)
        self.assertEqual(r.status_code, 409, r.text)

    def test_bad_email_and_bad_role_are_refused(self):
        client, key, store = _client()
        h = {"Authorization": f"Bearer {key}"}
        ws = store.workspace_for_key(store.lookup_key(key)["kid"])
        store.set_workspace_plan(ws["id"], "pro", 5)
        self.assertEqual(client.post("/v1/workspace/invites",
                                     json={"email": "nope"}, headers=h).status_code, 422)
        self.assertEqual(client.post("/v1/workspace/invites",
                                     json={"email": "a@x.test", "role": "root"},
                                     headers=h).status_code, 422)

    def test_ownership_cannot_be_invited(self):
        client, key, store = _client()
        h = {"Authorization": f"Bearer {key}"}
        ws = store.workspace_for_key(store.lookup_key(key)["kid"])
        store.set_workspace_plan(ws["id"], "pro", 5)
        r = client.post("/v1/workspace/invites",
                        json={"email": "a@x.test", "role": "owner"}, headers=h)
        self.assertEqual(r.status_code, 422)
        self.assertIn("transferred", r.text)

    def test_revoke_pending_invite(self):
        client, key, store = _client()
        h = {"Authorization": f"Bearer {key}"}
        ws = store.workspace_for_key(store.lookup_key(key)["kid"])
        store.set_workspace_plan(ws["id"], "pro", 5)
        inv = client.post("/v1/workspace/invites",
                          json={"email": "a@x.test"}, headers=h).json()
        self.assertEqual(client.delete(f"/v1/workspace/invites/{inv['id']}",
                                       headers=h).status_code, 200)
        self.assertEqual(client.get("/v1/workspace/invites",
                                    headers=h).json()["invites"], [])
        # revoking twice is a 404, not a silent success
        self.assertEqual(client.delete(f"/v1/workspace/invites/{inv['id']}",
                                       headers=h).status_code, 404)


class TestAcceptInvite(unittest.TestCase):
    def _invite(self):
        client, key, store = _client()
        h = {"Authorization": f"Bearer {key}"}
        ws = store.workspace_for_key(store.lookup_key(key)["kid"])
        store.set_workspace_plan(ws["id"], "pro", 5)
        inv = client.post("/v1/workspace/invites",
                          json={"email": "new@x.test", "role": "admin"},
                          headers=h).json()
        return client, h, inv

    def test_accept_binds_and_burns(self):
        client, _, inv = self._invite()
        r = client.post("/v1/invites/accept", json={"token": inv["token"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["email"], "new@x.test")
        self.assertEqual(r.json()["role"], "admin")

    def test_replay_is_refused(self):
        client, _, inv = self._invite()
        client.post("/v1/invites/accept", json={"token": inv["token"]})
        r = client.post("/v1/invites/accept", json={"token": inv["token"]})
        self.assertEqual(r.status_code, 410, r.text)

    def test_unknown_token_is_404(self):
        client, _, _ = self._invite()
        self.assertEqual(client.post("/v1/invites/accept",
                                     json={"token": "inv_made_up"}).status_code, 404)

    def test_missing_token_is_422(self):
        client, _, _ = self._invite()
        self.assertEqual(client.post("/v1/invites/accept", json={}).status_code, 422)

    def test_expired_invite_is_refused(self):
        # an invite created with a past expiry must not be redeemable
        client, key, store = _client()
        h = {"Authorization": f"Bearer {key}"}
        ws = store.workspace_for_key(store.lookup_key(key)["kid"])
        store.set_workspace_plan(ws["id"], "pro", 5)
        expired = workspaces.new_invite("exp@x.test", "member", ttl=-1)
        store.create_invite(ws["id"], expired)
        r = client.post("/v1/invites/accept", json={"token": expired["token"]})
        self.assertEqual(r.status_code, 410, r.text)
        self.assertIn("expired", r.text)


class TestMemberManagement(unittest.TestCase):
    def test_role_change_and_removal(self):
        client, key, store = _client()
        h = {"Authorization": f"Bearer {key}"}
        ws = store.workspace_for_key(store.lookup_key(key)["kid"])
        store.set_workspace_plan(ws["id"], "pro", 5)
        store.add_member(ws["id"], "dev@x.test", "member")
        r = client.patch("/v1/workspace/members/dev@x.test",
                         json={"role": "admin"}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(store.member_role(ws["id"], "dev@x.test"), "admin")
        self.assertEqual(client.delete("/v1/workspace/members/dev@x.test",
                                       headers=h).status_code, 200)
        self.assertIsNone(store.member_role(ws["id"], "dev@x.test"))

    def test_last_owner_cannot_be_demoted(self):
        client, key, store = _client()
        h = {"Authorization": f"Bearer {key}"}
        ws = store.workspace_for_key(store.lookup_key(key)["kid"])
        owner_email = ws["members"][0]["email"]
        r = client.patch(f"/v1/workspace/members/{owner_email}",
                         json={"role": "member"}, headers=h)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("only owner", r.text)

    def test_last_owner_cannot_be_removed(self):
        client, key, store = _client()
        h = {"Authorization": f"Bearer {key}"}
        ws = store.workspace_for_key(store.lookup_key(key)["kid"])
        owner_email = ws["members"][0]["email"]
        r = client.delete(f"/v1/workspace/members/{owner_email}", headers=h)
        self.assertEqual(r.status_code, 409, r.text)

    def test_unknown_member_is_404(self):
        client, key, _ = _client()
        h = {"Authorization": f"Bearer {key}"}
        self.assertEqual(
            client.delete("/v1/workspace/members/nobody@x.test",
                          headers=h).status_code, 404)
        self.assertEqual(
            client.patch("/v1/workspace/members/nobody@x.test",
                         json={"role": "admin"}, headers=h).status_code, 404)


class TestRolePolicy(unittest.TestCase):
    def test_permission_table_is_coherent(self):
        self.assertTrue(workspaces.can("owner", "manage_billing"))
        self.assertFalse(workspaces.can("admin", "manage_billing"))
        self.assertFalse(workspaces.can("member", "manage_members"))
        self.assertTrue(workspaces.can("member", "view"))
        self.assertFalse(workspaces.can("", "view"))

    def test_seat_limit_unknown_plan_is_the_floor_not_unlimited(self):
        self.assertEqual(workspaces.seat_limit("nonsense"),
                         workspaces.seat_limit("free"))

    def test_seats_count_distinct_people(self):
        members = [{"email": "a@x"}, {"email": "A@X"}, {"email": "b@x"}]
        self.assertEqual(workspaces.seats_used(members), 2)


if __name__ == "__main__":
    unittest.main()


class TestPlanChangesSeats(unittest.TestCase):
    def test_upgrade_grants_seats_and_downgrade_takes_them_back(self):
        from agentcheck import billing
        s = Store(Path(tempfile.mkdtemp()) / "p.db")
        raw = s.create_key("acct")
        kid = s.lookup_key(raw)["kid"]
        ws = s.workspace_for_key(kid)
        self.assertEqual(ws["seat_limit"], workspaces.seat_limit("free"))

        billing.apply_event(s, {"event": "subscription.activated", "kid": kid,
                                "plan": "pro"})
        self.assertEqual(s.workspace_for_key(kid)["seat_limit"],
                         workspaces.seat_limit("pro"))
        self.assertEqual(s.workspace_for_key(kid)["plan"], "pro")

        billing.apply_event(s, {"event": "subscription.cancelled", "kid": kid,
                                "plan": "pro"})
        self.assertEqual(s.workspace_for_key(kid)["seat_limit"],
                         workspaces.seat_limit("free"))
