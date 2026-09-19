"""Identity: login, sessions, and binding a person to a workspace.

Layer 1 gave a workspace members identified by an email that nobody had
proven they owned. This module is the proof half — sign in with a provider,
get a session, and any membership carrying that email becomes yours.

Same discipline as the rest of the codebase:

  * the session token is stored HASHED. A database read must not hand over a
    live session, exactly as it must not hand over an API key or an invite.
  * the OAuth ``state`` is signed and time-boxed, so a callback cannot be
    forged or replayed.
  * an unconfigured deployment refuses clearly (503) instead of rendering a
    login that cannot work.

WorkOS is the first provider (AuthKit for login, enterprise SSO as an
add-on). Another provider implements ``AuthProvider`` and nothing else moves.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

SESSION_COOKIE = "ac_session"
STATE_COOKIE = "ac_state"
SESSION_TTL_SECONDS = 14 * 86400
STATE_TTL_SECONDS = 600


class AuthError(RuntimeError):
    """A refused login or session, with a reason a user can act on."""


def session_token() -> str:
    return "sess_" + secrets.token_urlsafe(32)


def hash_session(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def secret() -> str:
    """The signing secret. Auth stays OFF without it, on purpose: a default
    secret is worse than no auth, because it looks enabled and is forgeable."""
    return (os.environ.get("AGENTCHECK_SECRET") or "").strip()


# ── signed OAuth state ──────────────────────────────────────────────────────

def _safe_next(path: str) -> str:
    """Only a same-origin absolute path survives. Anything else becomes "/".

    Storing this inside the signed state (rather than a second cookie) removes
    both an open-redirect and a cookie-quoting problem in one move.
    """
    p = str(path or "/").strip()
    if not p.startswith("/") or p.startswith("//") or "\\" in p:
        return "/"
    return p[:200]


def sign_state(nonce: str | None = None, next_path: str = "/",
               now: float | None = None) -> str:
    """A short-lived, signed state value, safe to put in a cookie.

    Hex and dots only: base64 padding makes cookie libraries quote the value,
    and a quoted value does not compare equal to what the browser sends back.
    The return path rides inside the signature, so it cannot be swapped.
    """
    key = secret()
    if not key:
        raise AuthError("AGENTCHECK_SECRET is required for login")
    nonce = nonce or secrets.token_hex(16)
    ts = int(now if now is not None else time.time())
    nxt = _safe_next(next_path).encode().hex()
    body = f"{nonce}.{ts}.{nxt}"
    sig = hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_state(state: str, now: float | None = None,
                 ttl: int = STATE_TTL_SECONDS) -> dict | None:
    """The signed payload, or None. Truthy when valid, so callers may use it
    as a boolean and still read ``["next"]``."""
    key = secret()
    if not key or not state:
        return None
    parts = str(state).split(".")
    if len(parts) != 4:
        return None
    nonce, ts_raw, nxt_hex, sig = parts
    try:
        ts = int(ts_raw)
    except Exception:
        return None
    expected = hmac.new(key.encode(), f"{nonce}.{ts_raw}.{nxt_hex}".encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    now = now if now is not None else time.time()
    if not (0 <= (now - ts) <= ttl):
        return None
    try:
        nxt = bytes.fromhex(nxt_hex).decode()
    except Exception:
        return None
    return {"nonce": nonce, "ts": ts, "next": _safe_next(nxt)}


# ── provider interface ──────────────────────────────────────────────────────

class AuthProvider:
    name = "none"

    def configured(self) -> bool:
        return False

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        raise AuthError("login is not configured on this deployment")

    def exchange(self, code: str, redirect_uri: str) -> dict:
        """code -> {"provider_user_id", "email", "name"}"""
        raise AuthError("login is not configured on this deployment")


class NullAuthProvider(AuthProvider):
    """No identity provider: every login attempt is a clear error."""


class OIDCProvider(AuthProvider):
    """Any standard OpenID Connect provider — Keycloak, Zitadel, Authentik,
    BoxyHQ Polis. Endpoints come from the issuer's discovery document, so a
    self-hosted IdP needs three values, not five.

    WorkOS is NOT this class: AuthKit has its own user-management flow (below).
    Keeping them separate is honest — pretending WorkOS is plain OIDC would
    work until it didn't.
    """

    name = "oidc"

    def __init__(self, issuer: str | None = None, client_id: str | None = None,
                 client_secret: str | None = None, transport=None):
        self.issuer = (issuer or os.environ.get("AGENTCHECK_OIDC_ISSUER", "")
                       ).rstrip("/")
        self.client_id = client_id or os.environ.get(
            "AGENTCHECK_OIDC_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get(
            "AGENTCHECK_OIDC_CLIENT_SECRET", "")
        self._transport = transport
        self._doc: dict | None = None

    def configured(self) -> bool:
        return bool(self.issuer and self.client_id and secret())

    def _request(self, method: str, url: str, **kw):
        if self._transport is not None:
            return self._transport(method, url, **kw)
        import httpx
        return httpx.request(method, url, timeout=20.0, **kw)

    def discover(self) -> dict:
        """The provider's endpoints, fetched once and cached."""
        if self._doc is not None:
            return self._doc
        url = f"{self.issuer}/.well-known/openid-configuration"
        r = self._request("GET", url)
        if r.status_code >= 400:
            raise AuthError(f"OIDC discovery failed at {url}: {r.status_code}")
        doc = r.json()
        for key in ("authorization_endpoint", "token_endpoint"):
            if not doc.get(key):
                raise AuthError(f"discovery document has no {key}")
        self._doc = doc
        return doc

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        from urllib.parse import urlencode
        doc = self.discover()
        return doc["authorization_endpoint"] + "?" + urlencode({
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
        })

    def exchange(self, code: str, redirect_uri: str) -> dict:
        doc = self.discover()
        # form-encoded, per the OIDC spec for the token endpoint
        r = self._request("POST", doc["token_endpoint"], data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        if r.status_code >= 400:
            raise AuthError(f"token exchange failed: {r.status_code} "
                            f"{r.text[:200]}")
        tok = r.json()
        claims = {}
        ui = doc.get("userinfo_endpoint")
        if ui:
            u = self._request("GET", ui,
                              headers={"Authorization":
                                       f"Bearer {tok.get('access_token', '')}"})
            if u.status_code < 400:
                claims = u.json()
        if not claims.get("email") and tok.get("id_token"):
            # Code flow: this token arrived directly from the token endpoint
            # over TLS, so reading its claims is the standard trust model.
            # The audience is still checked, because a token minted for a
            # different client must not sign anyone in here.
            claims = claims or _id_token_claims(tok["id_token"], self.client_id)
        sub = str(claims.get("sub") or claims.get("user_id") or "")
        email = str(claims.get("email") or "").strip().lower()
        if not sub or not email:
            raise AuthError("provider returned no subject or email")
        name = (claims.get("name") or claims.get("preferred_username")
                or email.split("@")[0])
        return {"provider_user_id": sub, "email": email, "name": str(name)}


def _id_token_claims(id_token: str, client_id: str) -> dict:
    """Unverified payload read, with the audience checked. See the note in
    OIDCProvider.exchange for why this is acceptable here and nowhere else."""
    import base64 as _b64
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(_b64.urlsafe_b64decode(payload.encode()))
    except Exception as e:
        raise AuthError(f"could not read id_token: {e}") from e
    aud = claims.get("aud")
    if isinstance(aud, list):
        ok = client_id in aud
    else:
        ok = aud == client_id
    if not ok:
        raise AuthError("id_token audience does not match this client")
    if float(claims.get("exp") or 0) < time.time():
        raise AuthError("id_token is expired")
    return claims


class WorkOSProvider(AuthProvider):
    """WorkOS AuthKit (login) — enterprise SSO is a connection on the org.

    httpx rather than the SDK, so a single integration does not grow the
    dependency list.
    """

    name = "workos"
    AUTHORIZE = "https://api.workos.com/user_management/authorize"
    AUTHENTICATE = "https://api.workos.com/user_management/authenticate"

    def __init__(self, client_id: str | None = None,
                 api_key: str | None = None, transport=None):
        self.client_id = client_id or os.environ.get("WORKOS_CLIENT_ID", "")
        self.api_key = api_key or os.environ.get("WORKOS_API_KEY", "")
        self._transport = transport

    def configured(self) -> bool:
        # a login that cannot complete is worse than a login that is honestly
        # absent, so the signing secret is part of "configured"
        return bool(self.client_id and self.api_key and secret())

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        from urllib.parse import urlencode
        return self.AUTHORIZE + "?" + urlencode({
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "provider": "authkit",
            "state": state,
        })

    def exchange(self, code: str, redirect_uri: str) -> dict:
        import httpx
        payload = {
            "client_id": self.client_id,
            "client_secret": self.api_key,
            "grant_type": "authorization_code",
            "code": code,
        }
        if self._transport is not None:
            r = self._transport("POST", self.AUTHENTICATE, json=payload)
        else:
            r = httpx.post(self.AUTHENTICATE, json=payload, timeout=20.0)
        if r.status_code >= 400:
            raise AuthError(f"workos authenticate -> {r.status_code}: "
                            f"{r.text[:200]}")
        data = r.json()
        user = data.get("user") or data.get("profile") or {}
        uid = str(user.get("id") or user.get("user_id") or "")
        email = str(user.get("email") or "").strip().lower()
        if not uid or not email:
            raise AuthError("workos response carried no user id or email")
        name = " ".join(
            x for x in (user.get("first_name"), user.get("last_name")) if x
        ).strip() or email.split("@")[0]
        return {"provider_user_id": uid, "email": email, "name": name}


def get_provider(name: str | None = None) -> AuthProvider:
    """The configured provider, or a provider that refuses.

    Opt-in like billing: AGENTCHECK_AUTH must be set, so credentials sitting
    in a shell never silently expose a login route.
    """
    setting = name if name is not None else os.environ.get("AGENTCHECK_AUTH", "")
    name = (setting or "none").lower()
    if name in ("none", "null", ""):
        return NullAuthProvider()
    if name == "workos":
        p = WorkOSProvider()
        return p if p.configured() else NullAuthProvider()
    if name in ("oidc", "keycloak", "zitadel", "authentik", "polis"):
        # the same client serves every standard OIDC IdP, so the name is
        # documentation, not a different code path
        p = OIDCProvider()
        return p if p.configured() else NullAuthProvider()
    raise AuthError(f"unknown auth provider {name!r}")


# ── session lifecycle ───────────────────────────────────────────────────────

def new_session(user_id: str, ttl: int = SESSION_TTL_SECONDS) -> dict:
    token = session_token()
    now = time.time()
    return {"token": token, "token_hash": hash_session(token),
            "user_id": user_id, "created": now, "expires_at": now + ttl}


def session_usable(record: dict, now: float | None = None) -> tuple[bool, str]:
    now = now if now is not None else time.time()
    if not record:
        return False, "no session"
    if float(record.get("expires_at") or 0) < now:
        return False, "session expired"
    return True, ""


def binding_plan(email: str, memberships: list[dict]) -> list[dict]:
    """Which memberships a login with this email should claim.

    Only unclaimed rows: binding over an existing user_id would let an email
    change silently take over a membership someone else already holds.
    """
    e = str(email or "").strip().lower()
    return [m for m in memberships
            if str(m.get("email") or "").strip().lower() == e
            and not m.get("user_id")]
