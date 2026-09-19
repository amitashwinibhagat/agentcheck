"""The caller's workspace: members, seats, invites, roles."""

from fastapi import Header, HTTPException

from agentcheck import workspaces
from agentcheck.routes import shared


def register(app, store):
    @app.get("/v1/workspace")
    async def workspace_detail(authorization: str | None = Header(None)):
        """The caller's workspace: members, seats, keys, plan."""
        _, ws, role = shared.acting(store, authorization)
        members = ws.get("members") or []
        pending = store.invites(ws["id"], pending_only=True)
        return {**ws, "role": role,
                "seats": {"used": workspaces.seats_used(members),
                          "limit": workspaces.seat_limit(ws["plan"]),
                          "pending_invites": len(pending)},
                "plan_seats": workspaces.seat_limit(ws["plan"]),
                "invites": pending}

    @app.post("/v1/workspace/invites")
    async def create_invite(payload: dict,
                            authorization: str | None = Header(None)):
        """Invite someone. The seat is reserved at invite time."""
        key, ws, role = shared.acting(store, authorization)
        shared.need_role(role, "manage_invites")
        email = str(payload.get("email") or "").strip().lower()
        if "@" not in email:
            raise HTTPException(422, "a valid email is required")
        invite_role = str(payload.get("role") or "member")
        try:
            invite = workspaces.new_invite(email, invite_role)
            members = store.workspace_members(ws["id"])
            workspaces.check_seat_available(
                ws["seat_limit"], members,
                [i["email"] for i in store.invites(ws["id"],
                                                   pending_only=True)],
                plan=ws["plan"])
        except workspaces.WorkspaceError as e:
            # 402 for a seat limit (a billing problem), 422 for a bad role
            code = 402 if "seat" in str(e) else 422
            raise HTTPException(code, str(e)) from None
        if any((m.get("email") or "").lower() == email for m in members):
            raise HTTPException(409, f"{email} is already a member")
        rec = store.create_invite(ws["id"], invite)
        store.record_event(key, "member_invited",
                           {"workspace": ws["id"], "email": email,
                            "role": invite_role})
        # the raw token is returned exactly once, like an API key
        return {"id": rec["id"], "email": email, "role": invite_role,
                "expires_at": rec["expires_at"], "token": invite["token"]}

    @app.get("/v1/workspace/invites")
    async def list_invites(authorization: str | None = Header(None)):
        _, ws, role = shared.acting(store, authorization)
        shared.need_role(role, "view")
        return {"invites": store.invites(ws["id"])}

    @app.delete("/v1/workspace/invites/{invite_id}")
    async def revoke_invite(invite_id: str,
                            authorization: str | None = Header(None)):
        _, ws, role = shared.acting(store, authorization)
        shared.need_role(role, "manage_invites")
        ok = store.revoke_invite(ws["id"], invite_id)
        if not ok:
            raise HTTPException(404, "no such pending invite")
        return {"revoked": True}

    @app.patch("/v1/workspace/members/{email}")
    async def change_role(email: str, payload: dict,
                          authorization: str | None = Header(None)):
        key, ws, role = shared.acting(store, authorization)
        shared.need_role(role, "manage_members")
        new_role = str(payload.get("role") or "")
        if new_role not in workspaces.ROLES:
            raise HTTPException(422, f"role must be one of {list(workspaces.ROLES)}")
        members = store.workspace_members(ws["id"])
        if store.member_role(ws["id"], email) is None:
            raise HTTPException(404, f"{email} is not a member")
        try:
            workspaces.check_owner_survives(members, email, new_role)
        except workspaces.WorkspaceError as e:
            raise HTTPException(409, str(e)) from None
        store.set_member_role(ws["id"], email, new_role)
        store.record_event(key, "member_role_changed",
                           {"workspace": ws["id"], "email": email,
                            "role": new_role})
        return {"email": email, "role": new_role}

    @app.delete("/v1/workspace/members/{email}")
    async def remove_member(email: str,
                            authorization: str | None = Header(None)):
        key, ws, role = shared.acting(store, authorization)
        shared.need_role(role, "manage_members")
        members = store.workspace_members(ws["id"])
        if store.member_role(ws["id"], email) is None:
            raise HTTPException(404, f"{email} is not a member")
        try:
            workspaces.check_owner_survives(members, email, None)
        except workspaces.WorkspaceError as e:
            raise HTTPException(409, str(e)) from None
        store.remove_member(ws["id"], email)
        store.record_event(key, "member_removed",
                           {"workspace": ws["id"], "email": email})
        return {"removed": True, "email": email}

    @app.post("/v1/invites/accept")
    async def accept_invite(payload: dict):
        """Redeem an invite token.

        Token-only, no key: the invitee has no account yet. Accepting
        MATERIALISES the membership — an invite that only marked itself
        accepted left the person invited but not a member.
        """
        token = str(payload.get("token") or "")
        if not token:
            raise HTTPException(422, "token is required")
        rec = store.invite_by_hash(workspaces.hash_token(token))
        if rec is None:
            raise HTTPException(404, "invite not found")
        ok, reason = workspaces.invite_usable(rec)
        if not ok:
            raise HTTPException(410, reason)
        if not store.materialize_invite(rec, payload.get("user_id")):
            raise HTTPException(
                409, "that workspace has no free seat; ask for an upgrade")
        return {"accepted": True, "workspace_id": rec["workspace_id"],
                "email": rec["email"], "role": rec["role"]}
