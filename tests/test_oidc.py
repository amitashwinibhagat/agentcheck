"""A real OIDC login, end to end, against a real HTTP identity provider.

WorkOS credentials do not exist in this environment, so the AuthKit exchange
cannot be exercised. What CAN be exercised — and is the same code a
self-hosted IdP would drive — is the standard OIDC path: discovery, an
authorization redirect, a form-encoded token exchange, and a userinfo call.

The stub below is a genuine HTTP server on a real socket. Nothing is injected,
so this exercises httpx, the form encoding, the discovery document, and the
claim parsing rather than a mock of them.
"""

import base64
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agentcheck import auth
from agentcheck.store import Store

SECRET = "oidc-test-secret"


class _IdP(BaseHTTPRequestHandler):
    """A minimal but conformant OIDC provider."""

    userinfo = True
    audience = "agentcheck-client"
    seen_token_body = None
    seen_bearer = None

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path.startswith("/.well-known/openid-configuration"):
            doc = {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "jwks_uri": f"{base}/jwks",
            }
            if type(self).userinfo:
                doc["userinfo_endpoint"] = f"{base}/userinfo"
            return self._json(doc)
        if self.path.startswith("/userinfo"):
            type(self).seen_bearer = self.headers.get("Authorization")
            if not type(self).seen_bearer:
                return self._json({"error": "no bearer"}, 401)
            return self._json({
                "sub": "idp-user-42",
                "email": "Dev@Acme.test",
                "name": "Dev Person",
                "preferred_username": "dev",
            })
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode()
        if self.path.startswith("/token"):
            form = parse_qs(raw)
            type(self).seen_token_body = form
            if form.get("code") == ["good-code"]:
                return self._json({"access_token": "at-123", "token_type":
                                   "Bearer", "expires_in": 3600,
                                   "id_token": _jwt(type(self).audience)})
            return self._json({"error": "invalid_grant"}, 400)
        return self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


def _jwt(aud, exp_offset=3600):
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": "idp-user-42", "email": "dev@acme.test",
        "aud": aud, "exp": int(time.time()) + exp_offset,
    }).encode()).decode().rstrip("=")
    return f"{header}.{payload}.not-a-real-signature"


class TestOIDCLogin(unittest.TestCase):
    def setUp(self):
        import os
        os.environ["AGENTCHECK_SECRET"] = SECRET
        os.environ["AGENTCHECK_BASE_URL"] = "https://app.test"
        _IdP.userinfo = True
        _IdP.audience = "agentcheck-client"
        _IdP.seen_token_body = None
        _IdP.seen_bearer = None
        self.server = HTTPServer(("127.0.0.1", 0), _IdP)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.issuer = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        import os
        for k in ("AGENTCHECK_SECRET", "AGENTCHECK_BASE_URL"):
            os.environ.pop(k, None)

    def _client(self, store=None):
        from fastapi.testclient import TestClient
        from agentcheck.proxy import create_app
        store = store or Store(Path(tempfile.mkdtemp()) / "o.db")
        key = store.create_key("acct", qpm_limit=60000)
        idp = auth.OIDCProvider(issuer=self.issuer,
                                client_id="agentcheck-client",
                                client_secret="shh")
        app = create_app(store, default_judge="stub", auth_provider=idp)
        return TestClient(app, base_url="https://app.test"), key, store

    def test_discovery_finds_the_endpoints(self):
        idp = auth.OIDCProvider(issuer=self.issuer, client_id="c",
                                client_secret="s")
        doc = idp.discover()
        self.assertEqual(doc["token_endpoint"], f"{self.issuer}/token")
        self.assertEqual(doc["userinfo_endpoint"], f"{self.issuer}/userinfo")

    def test_full_login_over_real_http(self):
        client, _, store = self._client()
        ws = store.create_workspace("Acme", "founder@acme.test", plan="pro",
                                    seat_limit=5)
        store.add_member(ws["id"], "dev@acme.test", "admin")

        # 1. start the login: a redirect to the IdP with signed state
        r = client.get("/v1/auth/login", follow_redirects=False)
        self.assertEqual(r.status_code, 302, r.text)
        loc = urlparse(r.headers["location"])
        self.assertEqual(loc.path, "/authorize")
        q = parse_qs(loc.query)
        self.assertEqual(q["client_id"], ["agentcheck-client"])
        self.assertIn("openid", q["scope"][0])
        state = q["state"][0]
        self.assertTrue(r.cookies.get(auth.STATE_COOKIE))

        # 2. the IdP sends the browser back with a code
        cb = client.get(f"/v1/auth/callback?code=good-code&state={state}",
                        follow_redirects=False)
        self.assertEqual(cb.status_code, 302, cb.text)
        self.assertEqual(cb.headers["location"], "/")
        self.assertTrue(cb.cookies.get(auth.SESSION_COOKIE))

        # the token exchange was a real form-encoded POST
        form = _IdP.seen_token_body
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(form["code"], ["good-code"])
        self.assertEqual(form["client_id"], ["agentcheck-client"])
        # and userinfo was called with the bearer token
        self.assertEqual(_IdP.seen_bearer, "Bearer at-123")

        # 3. the session works, and the invited membership is now theirs
        me = client.get("/v1/auth/me").json()
        self.assertTrue(me["authenticated"], me)
        self.assertEqual(me["user"]["email"], "dev@acme.test")
        self.assertEqual(me["user"]["name"], "Dev Person")
        self.assertEqual(me["workspaces"][0]["name"], "Acme")
        self.assertEqual(me["workspaces"][0]["role"], "admin")
        bound = [m for m in store.workspace_members(ws["id"])
                 if m["email"] == "dev@acme.test"][0]
        self.assertEqual(bound["user_id"], me["user"]["id"])

    def test_bad_code_is_refused_by_the_provider(self):
        client, _, _ = self._client()
        r = client.get("/v1/auth/login", follow_redirects=False)
        state = urlparse(r.headers["location"]).query.split("state=")[1]
        cb = client.get(f"/v1/auth/callback?code=wrong&state={state}",
                        follow_redirects=False)
        self.assertEqual(cb.status_code, 502)
        self.assertIn("token exchange failed", cb.text)

    def test_id_token_fallback_when_no_userinfo(self):
        # a provider with no userinfo endpoint must still work, from the
        # id_token — with the audience checked
        _IdP.userinfo = False
        client, _, _ = self._client()
        r = client.get("/v1/auth/login", follow_redirects=False)
        state = urlparse(r.headers["location"]).query.split("state=")[1]
        cb = client.get(f"/v1/auth/callback?code=good-code&state={state}",
                        follow_redirects=False)
        self.assertEqual(cb.status_code, 302, cb.text)
        me = client.get("/v1/auth/me").json()
        self.assertTrue(me["authenticated"], me)
        self.assertEqual(me["user"]["email"], "dev@acme.test")

    def test_id_token_for_another_client_is_refused(self):
        # a token minted for a different audience must not sign anyone in
        _IdP.userinfo = False
        _IdP.audience = "some-other-app"
        client, _, _ = self._client()
        r = client.get("/v1/auth/login", follow_redirects=False)
        state = urlparse(r.headers["location"]).query.split("state=")[1]
        cb = client.get(f"/v1/auth/callback?code=good-code&state={state}",
                        follow_redirects=False)
        self.assertEqual(cb.status_code, 502)
        self.assertIn("audience", cb.text)

    def test_expired_id_token_is_refused(self):
        _IdP.userinfo = False
        expired = _jwt("agentcheck-client", exp_offset=-60)
        with self.assertRaises(auth.AuthError):
            auth._id_token_claims(expired, "agentcheck-client")

    def test_discovery_failure_is_a_clear_error(self):
        # a port that was just released: connecting fails fast rather than
        # hanging, and nothing is listening
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()
        idp = auth.OIDCProvider(issuer=f"http://127.0.0.1:{dead}",
                                client_id="c", client_secret="s")
        with self.assertRaises(Exception):
            idp.discover()


class TestProviderNaming(unittest.TestCase):
    def setUp(self):
        import os
        os.environ["AGENTCHECK_SECRET"] = SECRET
        os.environ.update(AGENTCHECK_OIDC_ISSUER="https://idp.test",
                          AGENTCHECK_OIDC_CLIENT_ID="app",
                          AGENTCHECK_OIDC_CLIENT_SECRET="sec")

    def tearDown(self):
        import os
        for k in ("AGENTCHECK_SECRET", "AGENTCHECK_OIDC_ISSUER",
                  "AGENTCHECK_OIDC_CLIENT_ID", "AGENTCHECK_OIDC_CLIENT_SECRET"):
            os.environ.pop(k, None)

    def test_selfhosted_names_all_use_the_same_client(self):
        for name in ("oidc", "keycloak", "zitadel", "authentik", "polis"):
            self.assertIsInstance(auth.get_provider(name), auth.OIDCProvider)

    def test_workos_is_its_own_flow_not_oidc(self):
        self.assertFalse(issubclass(auth.WorkOSProvider, auth.OIDCProvider))


if __name__ == "__main__":
    unittest.main()
