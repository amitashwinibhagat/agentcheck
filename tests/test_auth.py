"""Identity contracts: sessions, login, and membership binding.

The properties that matter:
  * sessions are stored hashed and are revocable
  * the OAuth state is signed and time-boxed; a callback cannot be forged
  * an unconfigured deployment refuses login instead of half-working
  * logging in claims unclaimed memberships for that email — and only those
  * identity is the provider's user id, never the email
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcheck import auth, workspaces
from agentcheck.store import Store

SECRET = "test-secret-please-rotate"


class FakeProvider(auth.AuthProvider):
    """Shaped like WorkOS, so the contract is tested without the vendor."""

    name = "fake"

    def __init__(self, profile=None, fail=False):
        self.profile = profile or {
            "provider_user_id": "wos_user_1",
            "email": "dev@acme.test",
            "name": "Dev Person",
        }
        self.fail = fail
        self.seen_state = None
        self.seen_code = None

    def configured(self):
        return True

    def authorize_url(self, redirect_uri, state):
        self.seen_state = state
        return f"https://fake.idp/authorize?state={state}&redirect_uri={redirect_uri}"

    def exchange(self, code, redirect_uri):
        self.seen_code = code
        if self.fail:
            raise auth.AuthError("provider rejected the code")
        return dict(self.profile)


def _client(provider=None):
    """An https client on purpose: session cookies are Secure (the base URL is
    https), and a Secure cookie is correctly dropped over http — which is
    exactly what a browser would do."""
    from fastapi.testclient import TestClient
    from agentcheck.proxy import create_app
    store = Store(Path(tempfile.mkdtemp()) / "auth.db")
    key = store.create_key("acct", qpm_limit=60000)
    app = create_app(store, default_judge="stub",
                     auth_provider=provider or FakeProvider())
    return TestClient(app, base_url="https://app.test"), key, store


class TestStateSigning(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET

    def tearDown(self):
        os.environ.pop("AGENTCHECK_SECRET", None)

    def test_round_trip(self):
        s = auth.sign_state("nonce-1")
        self.assertTrue(auth.verify_state(s))

    def test_tampered_state_fails(self):
        s = auth.sign_state("nonce-1")
        self.assertFalse(auth.verify_state(s[:-4] + "AAAA"))

    def test_expired_state_fails(self):
        s = auth.sign_state("nonce-1")
        self.assertFalse(auth.verify_state(s, now=time.time() + 601))

    def test_garbage_fails(self):
        for bad in ("", "nonsense", "Zm9v"):
            self.assertFalse(auth.verify_state(bad))

    def test_another_secrets_state_fails(self):
        s = auth.sign_state("nonce-1")
        os.environ["AGENTCHECK_SECRET"] = "a-different-secret"
        self.assertFalse(auth.verify_state(s))

    def test_no_secret_means_no_signing(self):
        os.environ.pop("AGENTCHECK_SECRET", None)
        with self.assertRaises(auth.AuthError):
            auth.sign_state("nonce-1")
        self.assertFalse(auth.verify_state("anything"))


class TestSessions(unittest.TestCase):
    def test_session_is_stored_hashed_and_revocable(self):
        s = Store(Path(tempfile.mkdtemp()) / "s.db")
        u = s.upsert_user("fake", "u1", "a@x.test", "A")
        sess = auth.new_session(u["id"])
        s.create_session(sess)
        self.assertEqual(s.session_by_token(sess["token"])["user_id"], u["id"])
        self.assertNotIn(sess["token"], open(s.path, encoding="utf-8",
                                             errors="ignore").read())
        self.assertTrue(s.delete_session(sess["token"]))
        self.assertIsNone(s.session_by_token(sess["token"]))

    def test_expired_session_is_unusable(self):
        ok, why = auth.session_usable({"expires_at": time.time() - 1})
        self.assertFalse(ok)
        self.assertIn("expired", why)

    def test_missing_session_is_unusable(self):
        self.assertFalse(auth.session_usable({})[0])


class TestLoginFlow(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        os.environ["AGENTCHECK_BASE_URL"] = "https://app.test"

    def tearDown(self):
        for k in ("AGENTCHECK_SECRET", "AGENTCHECK_BASE_URL"):
            os.environ.pop(k, None)

    def test_login_redirects_with_signed_state_cookie(self):
        client, _, _ = _client()
        r = client.get("/v1/auth/login", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("fake.idp/authorize", r.headers["location"])
        state = r.cookies.get(auth.STATE_COOKIE)
        self.assertTrue(state and auth.verify_state(state))

    def test_full_login_binds_memberships_and_opens_a_session(self):
        provider = FakeProvider()
        client, _, store = _client(provider)
        ws = store.create_workspace("Acme", "founder@acme.test", plan="pro",
                                    seat_limit=5)
        store.add_member(ws["id"], "dev@acme.test", "admin")

        r = client.get("/v1/auth/login", follow_redirects=False)
        state = r.cookies.get(auth.STATE_COOKIE)
        cb = client.get(f"/v1/auth/callback?code=abc&state={state}",
                        follow_redirects=False)
        self.assertEqual(cb.status_code, 302, cb.text)
        self.assertTrue(cb.cookies.get(auth.SESSION_COOKIE))
        self.assertEqual(provider.seen_code, "abc")
        # the pending membership is now this user's
        self.assertEqual(store.member_role(ws["id"], "dev@acme.test"), "admin")
        members = store.workspace_members(ws["id"])
        bound = [m for m in members if m["email"] == "dev@acme.test"][0]
        self.assertTrue(bound["user_id"], "membership must be bound to the user")

        me = client.get("/v1/auth/me").json()
        self.assertTrue(me["authenticated"])
        self.assertEqual(me["user"]["email"], "dev@acme.test")
        self.assertEqual([w["name"] for w in me["workspaces"]], ["Acme"])
        self.assertEqual(me["workspaces"][0]["role"], "admin")

    def test_callback_with_wrong_state_is_refused(self):
        client, _, _ = _client()
        client.get("/v1/auth/login", follow_redirects=False)
        r = client.get("/v1/auth/callback?code=abc&state=forged",
                       follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        self.assertIn("state", r.text.lower())

    def test_callback_without_prior_login_is_refused(self):
        client, _, _ = _client()
        r = client.get("/v1/auth/callback?code=abc&state=whatever",
                       follow_redirects=False)
        self.assertEqual(r.status_code, 400)

    def test_provider_error_is_reported(self):
        client, _, _ = _client(FakeProvider(fail=True))
        r = client.get("/v1/auth/login", follow_redirects=False)
        state = r.cookies.get(auth.STATE_COOKIE)
        cb = client.get(f"/v1/auth/callback?code=abc&state={state}",
                        follow_redirects=False)
        self.assertEqual(cb.status_code, 502)

    def test_state_cookie_is_single_use(self):
        client, _, _ = _client()
        r = client.get("/v1/auth/login", follow_redirects=False)
        state = r.cookies.get(auth.STATE_COOKIE)
        first = client.get(f"/v1/auth/callback?code=abc&state={state}",
                           follow_redirects=False)
        self.assertEqual(first.status_code, 302)
        # the state cookie was cleared, so replaying the same link fails
        replay = client.get(f"/v1/auth/callback?code=abc&state={state}",
                            follow_redirects=False)
        self.assertEqual(replay.status_code, 400)

    def test_offsite_next_is_ignored(self):
        client, _, _ = _client()
        r = client.get("/v1/auth/login?next=https://evil.example",
                       follow_redirects=False)
        state = r.cookies.get(auth.STATE_COOKIE)
        cb = client.get(f"/v1/auth/callback?code=abc&state={state}",
                        follow_redirects=False)
        self.assertEqual(cb.headers["location"], "/")

    def test_logout_revokes_the_session(self):
        client, _, _ = _client()
        r = client.get("/v1/auth/login", follow_redirects=False)
        state = r.cookies.get(auth.STATE_COOKIE)
        client.get(f"/v1/auth/callback?code=abc&state={state}",
                   follow_redirects=False)
        self.assertTrue(client.get("/v1/auth/me").json()["authenticated"])
        client.post("/v1/auth/logout")
        self.assertFalse(client.get("/v1/auth/me").json()["authenticated"])


class TestUnconfigured(unittest.TestCase):
    def setUp(self):
        for k in ("AGENTCHECK_SECRET", "AGENTCHECK_AUTH", "WORKOS_CLIENT_ID",
                  "WORKOS_API_KEY"):
            os.environ.pop(k, None)

    def test_login_is_503_not_a_broken_page(self):
        client, _, _ = _client(auth.NullAuthProvider())
        r = client.get("/v1/auth/login", follow_redirects=False)
        self.assertEqual(r.status_code, 503)
        self.assertIn("not configured", r.text)

    def test_me_says_unauthenticated(self):
        client, _, _ = _client(auth.NullAuthProvider())
        d = client.get("/v1/auth/me").json()
        self.assertFalse(d["authenticated"])
        self.assertEqual(d["provider"], "none")

    def test_workspaces_requires_a_session(self):
        client, _, _ = _client(auth.NullAuthProvider())
        self.assertEqual(client.get("/v1/workspaces").status_code, 401)

    def test_credentials_alone_do_not_arm_login(self):
        # opt-in, exactly like billing: ambient creds must not expose a route
        os.environ["WORKOS_CLIENT_ID"] = "client_123"
        os.environ["WORKOS_API_KEY"] = "sk_123"
        os.environ["AGENTCHECK_SECRET"] = SECRET
        try:
            self.assertIsInstance(auth.get_provider(), auth.NullAuthProvider)
        finally:
            for k in ("WORKOS_CLIENT_ID", "WORKOS_API_KEY",
                      "AGENTCHECK_SECRET"):
                os.environ.pop(k, None)


class TestWorkspaceById(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        os.environ["AGENTCHECK_BASE_URL"] = "https://app.test"

    def tearDown(self):
        for k in ("AGENTCHECK_SECRET", "AGENTCHECK_BASE_URL"):
            os.environ.pop(k, None)

    def test_member_can_read_their_workspace_by_id(self):
        client, _, store = _client()
        ws = store.create_workspace("Acme", "dev@acme.test", plan="pro",
                                    seat_limit=5)
        r = client.get("/v1/auth/login", follow_redirects=False)
        state = r.cookies.get(auth.STATE_COOKIE)
        client.get(f"/v1/auth/callback?code=abc&state={state}",
                   follow_redirects=False)
        got = client.get(f"/v1/workspaces/{ws['id']}")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["role"], "owner")

    def test_non_member_is_refused(self):
        client, _, store = _client()
        other = store.create_workspace("Other", "someone@else.test")
        r = client.get("/v1/auth/login", follow_redirects=False)
        state = r.cookies.get(auth.STATE_COOKIE)
        client.get(f"/v1/auth/callback?code=abc&state={state}",
                   follow_redirects=False)
        self.assertEqual(client.get(f"/v1/workspaces/{other['id']}").status_code,
                         403)

    def test_own_key_can_read_its_workspace(self):
        client, key, store = _client()
        ws = store.workspace_for_key(store.lookup_key(key)["kid"])
        r = client.get(f"/v1/workspaces/{ws['id']}",
                       headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_unknown_workspace_is_404(self):
        client, key, _ = _client()
        r = client.get("/v1/workspaces/ws_nope",
                       headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 404)


class TestBindingPlan(unittest.TestCase):
    def test_only_unclaimed_memberships_are_claimed(self):
        memberships = [
            {"email": "a@x.test", "user_id": None},
            {"email": "A@X.test", "user_id": None},
            {"email": "a@x.test", "user_id": "user_other"},
            {"email": "b@x.test", "user_id": None},
        ]
        picked = auth.binding_plan("a@x.test", memberships)
        self.assertEqual(len(picked), 2)
        self.assertTrue(all(not m["user_id"] for m in picked))

    def test_unrelated_email_claims_nothing(self):
        self.assertEqual(auth.binding_plan("nobody@x.test",
                                           [{"email": "a@x.test",
                                             "user_id": None}]), [])


if __name__ == "__main__":
    unittest.main()


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class TestWorkOSSkipping(unittest.TestCase):
    """The HTTP exchange cannot be verified without live credentials, but the
    RESPONSE PARSING can — and that is the part most likely to drift, since
    WorkOS returns different shapes for AuthKit and the SSO product."""

    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET

    def tearDown(self):
        os.environ.pop("AGENTCHECK_SECRET", None)

    def _provider(self, payload):
        captured = {}

        def transport(method, url, json=None):
            captured["url"] = url
            captured["body"] = json
            return _Resp(200, payload)

        return auth.WorkOSProvider("client_1", "sk_1",
                                   transport=transport), captured

    def test_authkit_shape_parses(self):
        p, captured = self._provider({
            "access_token": "at", "refresh_token": "rt",
            "user": {
                "object": "user", "id": "user_01HZZ", "email": "Dev@Acme.test",
                "first_name": "Dev", "last_name": "Person",
                "email_verified": True,
            },
        })
        prof = p.exchange("code_123", "https://app.test/v1/auth/callback")
        self.assertEqual(prof["provider_user_id"], "user_01HZZ")
        self.assertEqual(prof["email"], "dev@acme.test")   # normalised
        self.assertEqual(prof["name"], "Dev Person")
        self.assertTrue(captured["url"].endswith("/user_management/authenticate"))
        self.assertEqual(captured["body"]["grant_type"], "authorization_code")
        self.assertEqual(captured["body"]["client_id"], "client_1")

    def test_sso_profile_shape_parses(self):
        p, _ = self._provider({"profile": {"id": "prof_9", "email": "a@b.test"}})
        prof = p.exchange("c", "https://app.test/cb")
        self.assertEqual(prof["provider_user_id"], "prof_9")
        self.assertEqual(prof["email"], "a@b.test")

    def test_missing_identity_is_an_error_not_a_guest(self):
        p, _ = self._provider({"user": {"email": "a@b.test"}})
        with self.assertRaises(auth.AuthError):
            p.exchange("c", "https://app.test/cb")

    def test_http_error_is_reported(self):
        def transport(method, url, json=None):
            return _Resp(401, {}, "bad client secret")

        p = auth.WorkOSProvider("client_1", "sk_1", transport=transport)
        with self.assertRaises(auth.AuthError):
            p.exchange("c", "https://app.test/cb")

    def test_authorize_url_carries_state_and_redirect(self):
        p = auth.WorkOSProvider("client_1", "sk_1")
        u = p.authorize_url("https://app.test/v1/auth/callback", "st.1.6162.sig")
        self.assertIn("client_id=client_1", u)
        self.assertIn("state=st.1.6162.sig", u)
        self.assertIn("response_type=code", u)
        self.assertIn("redirect_uri=https%3A%2F%2Fapp.test", u)


class TestStateIsCookieSafe(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET

    def tearDown(self):
        os.environ.pop("AGENTCHECK_SECRET", None)

    def test_state_has_no_characters_that_libraries_quote(self):
        # base64 padding makes Starlette quote the cookie, and a quoted value
        # is not equal to what the browser sends back — a real bug this test
        # exists to prevent from returning
        s = auth.sign_state("nonce1")
        self.assertRegex(s, r"^[A-Za-z0-9._-]+$")
        self.assertNotIn("=", s)
        self.assertNotIn("/", s)
        self.assertNotIn("+", s)

    def test_return_path_round_trips_inside_the_signature(self):
        s = auth.sign_state("n", next_path="/workspaces/ws_1?tab=seats")
        self.assertEqual(auth.verify_state(s)["next"],
                         "/workspaces/ws_1?tab=seats")

    def test_offsite_paths_are_neutralised_at_signing_time(self):
        for evil in ("https://evil.example", "//evil.example", "javascript:x",
                     "\\\\evil", ""):
            self.assertEqual(auth.verify_state(
                auth.sign_state("n", next_path=evil))["next"], "/")

    def test_tampering_with_the_path_breaks_the_signature(self):
        s = auth.sign_state("n", next_path="/safe")
        body, sig = s.rsplit(".", 1)
        swapped = body[:-1] + ("0" if body[-1] != "0" else "1") + "." + sig
        self.assertIsNone(auth.verify_state(swapped))


class TestInviteBecomesMembership(unittest.TestCase):
    """The bridge that was missing: an invite lives in workspace_invites, a
    membership lives in workspace_members. Signing in used to claim only
    member rows, so an INVITED person could sign in and still not be a member.
    The earlier test missed it by calling add_member() directly."""

    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        os.environ["AGENTCHECK_BASE_URL"] = "https://app.test"

    def tearDown(self):
        for k in ("AGENTCHECK_SECRET", "AGENTCHECK_BASE_URL"):
            os.environ.pop(k, None)

    def test_invite_is_claimed_on_sign_in(self):
        provider = FakeProvider(profile={
            "provider_user_id": "wos_inv", "email": "invited@acme.test",
            "name": "Invited"})
        client, _, store = _client(provider)
        ws = store.create_workspace("Acme", "owner@acme.test", plan="pro",
                                    seat_limit=workspaces.seat_limit("pro"))
        inv = workspaces.new_invite("invited@acme.test", "admin")
        store.create_invite(ws["id"], inv)
        # before: invited, not a member
        self.assertIsNone(store.member_role(ws["id"], "invited@acme.test"))

        r = client.get("/v1/auth/login", follow_redirects=False)
        state = r.cookies.get(auth.STATE_COOKIE)
        cb = client.get(f"/v1/auth/callback?code=abc&state={state}",
                        follow_redirects=False)
        self.assertEqual(cb.status_code, 302, cb.text)

        # after: a member, with the invited role, bound to the user
        self.assertEqual(store.member_role(ws["id"], "invited@acme.test"),
                         "admin")
        bound = [m for m in store.workspace_members(ws["id"])
                 if m["email"] == "invited@acme.test"][0]
        self.assertTrue(bound["user_id"], "membership must be bound to the user")
        me = client.get("/v1/auth/me").json()
        self.assertEqual([w["name"] for w in me["workspaces"]], ["Acme"])

    def test_invite_is_single_use_across_logins(self):
        provider = FakeProvider(profile={
            "provider_user_id": "wos_1", "email": "inv@acme.test", "name": "I"})
        client, _, store = _client(provider)
        ws = store.create_workspace("Acme", "o@acme.test", plan="pro", seat_limit=5)
        store.create_invite(ws["id"], workspaces.new_invite("inv@acme.test",
                                                            "member"))
        for _ in range(2):
            r = client.get("/v1/auth/login", follow_redirects=False)
            state = r.cookies.get(auth.STATE_COOKIE)
            client.get(f"/v1/auth/callback?code=abc&state={state}",
                       follow_redirects=False)
        members = [m for m in store.workspace_members(ws["id"])
                   if m["email"] == "inv@acme.test"]
        self.assertEqual(len(members), 1, "signing in twice must not duplicate")

    def test_full_workspace_leaves_the_invite_pending(self):
        provider = FakeProvider(profile={
            "provider_user_id": "wos_2", "email": "late@acme.test", "name": "L"})
        client, _, store = _client(provider)
        ws = store.create_workspace("Acme", "o@acme.test", plan="pro", seat_limit=5)
        store.create_invite(ws["id"], workspaces.new_invite("late@acme.test",
                                                            "member"))
        store.set_workspace_plan(ws["id"], "pro", 1)  # no room now
        r = client.get("/v1/auth/login", follow_redirects=False)
        state = r.cookies.get(auth.STATE_COOKIE)
        client.get(f"/v1/auth/callback?code=abc&state={state}",
                   follow_redirects=False)
        self.assertIsNone(store.member_role(ws["id"], "late@acme.test"))
        self.assertEqual(len(store.invites(ws["id"], pending_only=True)), 1)

    def test_token_acceptance_materialises_the_membership(self):
        client, _, store = _client()
        ws = store.create_workspace("Acme", "o@acme.test", plan="pro", seat_limit=5)
        inv = workspaces.new_invite("tok@acme.test", "member")
        store.create_invite(ws["id"], inv)
        r = client.post("/v1/invites/accept", json={"token": inv["token"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(store.member_role(ws["id"], "tok@acme.test"), "member")
