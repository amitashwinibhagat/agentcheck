"""Workspace listing across every workspace the caller can see."""

from fastapi import Header, HTTPException, Request

from agentcheck import workspaces
from agentcheck.routes import shared


def register(app, store):
    @app.get("/v1/workspaces")
    async def my_workspaces(request: Request):
        """Workspaces the signed-in person belongs to, with their role."""
        user = shared.current_user(store, request)
        if user is None:
            raise HTTPException(401, "not signed in")
        rows = store.workspaces_for_user(user["id"])
        return {"user": user["id"], "workspaces": [
            {"id": w["id"], "name": w["name"], "plan": w["plan"],
             "role": w.get("role"),
             "seats": {"used": workspaces.seats_used(
                 store.workspace_members(w["id"])),
                 "limit": w["seat_limit"]}}
            for w in rows]}

    @app.get("/v1/workspaces/{wid}")
    async def workspace_by_id(wid: str, request: Request,
                              authorization: str | None = Header(None)):
        """One workspace, for a member (session) or its own key."""
        ws = store.workspace(wid)
        if ws is None:
            raise HTTPException(404, "no such workspace")
        user = shared.current_user(store, request)
        role = None
        if user is not None:
            for m in ws["members"]:
                if m.get("user_id") == user["id"]:
                    role = m.get("role")
                    break
        if role is None and authorization:
            key = shared.authorize(store, authorization)
            own = store.workspace_for_key(key)
            if own is not None and own["id"] == wid:
                role = "owner" if any(
                    m.get("user_id") == key and m.get("role") == "owner"
                    for m in ws["members"]) else "admin"
        if role is None:
            raise HTTPException(403, "not a member of this workspace")
        pending = store.invites(wid, pending_only=True)
        return {**ws, "role": role,
                "seats": {"used": workspaces.seats_used(ws["members"]),
                          "limit": ws["seat_limit"],
                          "pending_invites": len(pending)},
                "invites": pending}
