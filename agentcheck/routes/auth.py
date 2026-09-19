"""Identity: login, sessions, membership binding."""

import uuid

from fastapi import Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from agentcheck import auth
from agentcheck.routes import shared


def register(app, store, idp):
    @app.get("/v1/auth/login")
    async def auth_login(request: Request, next: str = "/"):
        """Start a login: signed state in a cookie, then redirect out."""
        if not idp.configured():
            raise HTTPException(
                503, "login is not configured on this deployment")
        try:
            # the return path is signed INTO the state, so the callback never
            # trusts a client-supplied redirect target
            state = auth.sign_state(uuid.uuid4().hex, next_path=next)
            url = idp.authorize_url(
                f"{shared.base_url(request)}/v1/auth/callback", state)
        except auth.AuthError as e:
            raise HTTPException(503, str(e)) from None
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(auth.STATE_COOKIE, state, max_age=auth.STATE_TTL_SECONDS,
                        httponly=True, samesite="lax",
                        secure=shared.base_url(request).startswith("https"),
                        path="/")
        return resp

    @app.get("/v1/auth/callback")
    async def auth_callback(request: Request, code: str | None = None,
                            state: str | None = None, error: str | None = None):
        """Finish a login. Verifies state BEFORE exchanging the code."""
        if error:
            raise HTTPException(400, f"provider returned an error: {error}")
        if not code:
            raise HTTPException(400, "missing code")
        expected = request.cookies.get(auth.STATE_COOKIE)
        payload = auth.verify_state(state) if state else None
        if not payload or not expected or state != expected:
            raise HTTPException(400, "invalid or expired login state")
        try:
            profile = idp.exchange(code, f"{shared.base_url(request)}/v1/auth/callback")
        except auth.AuthError as e:
            raise HTTPException(502, str(e)) from None
        user = store.upsert_user(idp.name, profile["provider_user_id"],
                                 profile["email"], profile.get("name"))
        # Two ways a membership can be waiting: an unclaimed member row, or a
        # pending INVITE (which lives in another table and used to be dropped
        # here — an invited person would sign in and still not be a member).
        claimed = store.bind_memberships(profile["email"], user["id"])
        invited = store.claim_invites(profile["email"], user["id"])
        store.record_event(user["id"], "login",
                           {"provider": idp.name, "claimed": claimed,
                            "invites_claimed": len(invited)})
        session = auth.new_session(user["id"])
        store.create_session(session)
        resp = RedirectResponse(payload["next"], status_code=302)
        shared.set_session_cookie(resp, request, session["token"],
                                  auth.SESSION_TTL_SECONDS)
        resp.delete_cookie(auth.STATE_COOKIE, path="/")
        return resp

    @app.get("/v1/auth/me")
    async def auth_me(request: Request):
        """Who am I, and which workspaces do I belong to."""
        user = shared.current_user(store, request)
        if user is None:
            return {"authenticated": False, "provider": idp.name}
        rows = store.workspaces_for_user(user["id"])
        return {"authenticated": True, "provider": idp.name,
                "user": {"id": user["id"], "email": user["email"],
                         "name": user["name"]},
                "workspaces": [{"id": w["id"], "name": w["name"],
                                "role": w.get("role"), "plan": w["plan"]}
                               for w in rows]}

    @app.post("/v1/auth/logout")
    async def auth_logout(request: Request):
        token = request.cookies.get(auth.SESSION_COOKIE)
        removed = store.delete_session(token) if token else False
        resp = Response(content='{"logged_out": true}',
                        media_type="application/json")
        resp.delete_cookie(auth.SESSION_COOKIE, path="/")
        return resp if removed else resp
