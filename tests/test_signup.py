"""Signup: a stranger signs in and reaches a first judged call.

The funnel the WorkOS integration was missing:
  * first login with no invite creates exactly one workspace (owner: you),
    and a second login creates none
  * an invited login claims the invite and creates no workspace
  * POST /v1/me/keys mints a working key from the session alone; the raw
    token is returned once and never listed
  * sessions and Bearer stay separate: no session 401s, a Bearer 401s,
    the 6th key 429s, a workspace-less session 403s
"""

import os
import tempfile
import unittest
from pathlib import Path

from agentcheck import auth
from agentcheck.store import Store
from tests.test_auth import FakeProvider

SECRET = "test-secret-please-rotate"


def _client(provider=None):
    from fastapi.testclient import TestClient
    from agentcheck.proxy import create_app
    store = Store(Path(tempfile.mkdtemp()) / "signup.db")
    key = store.create_key("acct", qpm_limit=60000)
    app = create_app(store, default_judge="stub",
                     auth_provider=provider or FakeProvider())
    client = TestClient(app, base_url="https://app.test")
    return client, key, store


def _login(client):
    """Full login dance through the fake provider. The client keeps the
    session cookie, exactly like a browser would."""
    r = client.get("/v1/auth/login", follow_redirects=False)
    state = r.cookies.get(auth.STATE_COOKIE)
    cb = client.get(f"/v1/auth/callback?code=abc&state={state}",
                    follow_redirects=False)
    assert cb.status_code == 302, cb.text
    return cb


class TestFirstLogin(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        os.environ["AGENTCHECK_BASE_URL"] = "https://app.test"

    def tearDown(self):
        for k in ("AGENTCHECK_SECRET", "AGENTCHECK_BASE_URL"):
            os.environ.pop(k, None)

    def test_stranger_gets_one_workspace_owned_by_them(self):
        client, _, store = _client()
        _login(client)
        me = client.get("/v1/auth/me").json()
        self.assertTrue(me["authenticated"])
        self.assertEqual(len(me["workspaces"]), 1)
        ws = me["workspaces"][0]
        self.assertEqual(ws["name"], "Dev Person's workspace")
        self.assertEqual(ws["role"], "owner")
        self.assertEqual(ws["plan"], "free")

    def test_second_login_creates_nothing(self):
        client, _, store = _client()
        _login(client)
        _login(client)
        me = client.get("/v1/auth/me").json()
        self.assertEqual(len(me["workspaces"]), 1)

    def test_nameless_profile_falls_back_to_email_local_part(self):
        p = FakeProvider(profile={"provider_user_id": "wos_2",
                                  "email": "sam@acme.test", "name": ""})
        client, _, store = _client(p)
        _login(client)
        me = client.get("/v1/auth/me").json()
        self.assertEqual(me["workspaces"][0]["name"], "sam's workspace")

    def test_invited_login_claims_and_creates_no_workspace(self):
        from agentcheck import workspaces as _ws
        client, _, store = _client()
        ws = store.create_workspace("Acme", "founder@acme.test", plan="pro",
                                    seat_limit=5)
        inv = store.create_invite(ws["id"], _ws.new_invite("dev@acme.test", "admin"))
        _login(client)
        me = client.get("/v1/auth/me").json()
        self.assertEqual([w["name"] for w in me["workspaces"]], ["Acme"])
        self.assertTrue(store.invite_by_hash(inv["token_hash"])["accepted_at"])


class TestSessionKeyMinting(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        os.environ["AGENTCHECK_BASE_URL"] = "https://app.test"

    def tearDown(self):
        for k in ("AGENTCHECK_SECRET", "AGENTCHECK_BASE_URL"):
            os.environ.pop(k, None)

    def test_session_mints_a_working_key_bound_to_the_workspace(self):
        client, _, store = _client()
        _login(client)
        wid = client.get("/v1/auth/me").json()["workspaces"][0]["id"]
        r = client.post("/v1/me/keys", json={"name": "laptop"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["key"].startswith("ac_"))
        self.assertEqual(body["workspace_id"], wid)
        self.assertEqual(body["plan"], "free")
        # the key works on the product path, and joined the workspace —
        # no second workspace was spawned
        ok = client.get("/v1/results",
                        headers={"Authorization": f"Bearer {body['key']}"})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(store.workspace_for_key(body["kid"])["id"], wid)
        self.assertEqual(len(store.workspaces_for_user(
            client.get("/v1/auth/me").json()["user"]["id"])), 1)
        # the raw token is returned once and never listed
        listed = store.workspace_keys(wid)
        self.assertEqual(len(listed), 1)
        self.assertFalse(any(v == body["key"] for row in listed
                             for v in row.values()))

    def test_no_session_401s(self):
        client, _, _ = _client()
        r = client.post("/v1/me/keys", json={"name": "x"})
        self.assertEqual(r.status_code, 401)

    def test_bearer_cannot_use_the_session_endpoint(self):
        client, key, _ = _client()
        _login(client)
        r = client.post("/v1/me/keys", json={"name": "x"},
                        headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 401)

    def test_sixth_key_429s(self):
        client, _, _ = _client()
        _login(client)
        for i in range(5):
            r = client.post("/v1/me/keys", json={"name": f"k{i}"})
            self.assertEqual(r.status_code, 200, r.text)
        r = client.post("/v1/me/keys", json={"name": "one-too-many"})
        self.assertEqual(r.status_code, 429)

    def test_workspace_less_session_403s(self):
        client, _, store = _client()
        u = store.upsert_user("fake", "ghost@test", "ghost@test", "Ghost")
        sess = auth.new_session(u["id"])
        store.create_session(sess)
        client.cookies.set(auth.SESSION_COOKIE, sess["token"])
        r = client.post("/v1/me/keys", json={"name": "x"})
        self.assertEqual(r.status_code, 403)

    def test_create_key_with_bad_workspace_fails_loudly(self):
        _, _, store = _client()
        with self.assertRaises(ValueError):
            store.create_key("orphan", workspace_id="ws_nope")


if __name__ == "__main__":
    unittest.main()
