"""Cross-domain HTTP helpers. Every function takes `store` explicitly.

These lived as closures inside `create_app`, which meant 38 endpoints shared
state only by nesting. Same bodies, store threaded through — nothing else
changed.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, Response

from agentcheck import auth
from agentcheck import checks as check_lib
from agentcheck import workspaces

if TYPE_CHECKING:  # annotations only; the store is duck-typed at runtime
    from agentcheck.store import Store


def authorize(store: Store, authorization: str | None) -> str:
    # isinstance, not just falsy: a caller passing a non-string (FastAPI's
    # Header default object, a list, None from a direct call) used to reach
    # .startswith and raise AttributeError — a 500 where a 401 belongs.
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    row = store.lookup_key(token)
    if row is None:
        raise HTTPException(401, "unknown api key")
    return row["kid"]


def is_local(request: Request | None) -> bool:
    """Is this a browser on the same box — the only caller a key is handed to?

    The peer address is NOT sufficient. Behind a reverse proxy running on the
    same host (Caddy with host networking, nginx, a `docker -p
    127.0.0.1:7373:7373` publish) every public request arrives from loopback,
    and this predicate gates `/v1/bootstrap` — i.e. hands out a working API
    key. Our own deployment avoided that by accident: Caddy reaches the app
    over the docker bridge, so the peer was 172.x. A change in networking
    would have silently published a key.

    So a request is local only when ALL of these hold:
      * no proxy headers (a proxy is in front; we are not the edge)
      * the socket peer is loopback
      * the caller addressed this box as itself (`localhost`, `127.0.0.1`),
        not by a public name that happens to resolve here
    """
    if request is None:
        return False
    for header in ("x-forwarded-for", "x-forwarded-host", "x-real-ip",
                   "forwarded"):
        if request.headers.get(header):
            return False
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "testclient"):
        return False
    name = (request.headers.get("host") or "").split(":")[0].strip().lower()
    return name in ("localhost", "127.0.0.1", "::1")


def acting(store: Store, authorization: str | None) -> tuple[str, dict, str]:
    """(kid, workspace, role) for the caller.

    A request acts on the workspace its key belongs to. During Layer 1 the
    caller IS the key's owner, so the role comes from that member row —
    the check is real, and Layer 2 just supplies a different user.
    """
    key = authorize(store, authorization)
    ws = store.workspace_for_key(key)
    if ws is None:
        raise HTTPException(500, "key has no workspace")
    role = "member"
    for m in ws.get("members") or []:
        if m.get("user_id") == key:
            role = m.get("role") or "member"
            break
    return key, ws, role


def need_role(role: str, action: str) -> None:
    try:
        workspaces.require(role, action)
    except workspaces.WorkspaceError as e:
        raise HTTPException(403, str(e)) from None


def base_url(request: Request) -> str:
    """Public base URL for redirect URIs. Behind a proxy the request URL
    is the internal one, so an explicit setting wins."""
    base = (os.environ.get("AGENTCHECK_BASE_URL") or "").rstrip("/")
    if base:
        return base
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def set_session_cookie(response: Response, request: Request, token: str,
                       max_age: int) -> None:
    secure = (base_url(request).startswith("https")
              or request.url.scheme == "https")
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=max_age,
                        httponly=True, samesite="lax", secure=secure,
                        path="/")


def current_user(store: Store, request: Request) -> dict | None:
    """The signed-in person, or None. Expired sessions are refused and
    cleared, so a stale cookie cannot act."""
    token = request.cookies.get(auth.SESSION_COOKIE)
    if not token:
        return None
    rec = store.session_by_token(token)
    ok, _reason = auth.session_usable(rec or {})
    if not ok:
        return None
    store.touch_session(token)
    return store.user(rec["user_id"])


def collision_message(store: Store, key: str) -> str:
    """The designed paywall moment: what they did, what unlocks, one path.

    Never a dead end, never guilt copy. Names the value first."""
    used = store.used_this_month(key)
    meta = store.key_meta(key) or {}
    allowance = meta.get("monthly_allowance", 500)
    caught = store.fail_count_this_month(key)
    return (
        f"Free allowance exhausted ({used}/{allowance} questions used this month). "
        f"You caught {caught} risky call{'s' if caught != 1 else ''} so far. "
        "Teams continues with a higher allowance — upgrade to keep checking."
    )


def get_checkset(name: str):
    """Unknown check set is a client error, not a 500."""
    try:
        return check_lib.get(name)
    except KeyError:
        raise HTTPException(422, f"unknown check set {name!r}; have {check_lib.all_names()}")
