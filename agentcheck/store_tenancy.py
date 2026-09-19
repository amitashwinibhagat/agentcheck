"""Workspaces, members, invites, roles and seats.

Never instantiated on its own; mixed into Store so the class keeps one
public API. Methods are byte-identical to their originals.
"""

from __future__ import annotations

import time
import uuid
from agentcheck import db as _db


class TenancyMixin:
    def _migrate_workspaces(self, c) -> None:
        """Every key belongs to a workspace. Existing keys each get their own,
        named after the key, with that key as owner — so a single-account
        install becomes a one-person workspace rather than a special case.
        """
        if "workspace_id" not in _db.columns(c, "api_keys"):
            return
        orphans = c.execute(
            "SELECT kid, name FROM api_keys WHERE workspace_id IS NULL"
        ).fetchall()
        for r in orphans:
            kid = _db.first(r, 0)
            name = str(_db.first(r, 1) or "workspace")
            self._attach_workspace(c, kid, name)
    @staticmethod
    def _new_workspace_id() -> str:
        return "ws_" + uuid.uuid4().hex[:16]
    def _attach_workspace(self, c, kid: str, name: str,
                          plan: str = "free") -> str:
        """Give a new key its own workspace, with the key as owner.

        Called for every key created after startup too — the migration only
        covers keys that existed when the store opened. Seat limit comes from
        the plan catalogue, so it cannot drift.
        """
        from agentcheck import workspaces as _ws
        seat_limit = _ws.seat_limit(plan)
        wid = self._new_workspace_id()
        c.execute(
            "INSERT INTO workspaces (id, name, created, plan, seat_limit) "
            "VALUES (?,?,?,?,?)",
            (wid, str(name or "workspace"), time.time(), plan, seat_limit))
        c.execute(
            "INSERT INTO workspace_members (id, workspace_id, email, role, "
            "created, user_id) VALUES (?,?,?,?,?,?)",
            ("wm_" + uuid.uuid4().hex[:16], wid, f"{name or 'owner'}@local",
             "owner", time.time(), kid))
        c.execute("UPDATE api_keys SET workspace_id = ? WHERE kid = ?",
                  (wid, kid))
        return wid
    def create_workspace(self, name: str, owner_email: str,
                         plan: str = "free", seat_limit: int = 1) -> dict:
        """A workspace with exactly one owner. Owner is a member row, so the
        same lookup serves members and permission checks."""
        wid = self._new_workspace_id()
        email = str(owner_email).strip().lower()
        with self._conn() as c:
            c.execute(
                "INSERT INTO workspaces (id, name, created, plan, seat_limit) "
                "VALUES (?,?,?,?,?)",
                (wid, str(name).strip(), time.time(), plan, seat_limit))
            c.execute(
                "INSERT INTO workspace_members (id, workspace_id, email, role, "
                "created, user_id) VALUES (?,?,?,?,?,?)",
                ("wm_" + uuid.uuid4().hex[:16], wid, email, "owner",
                 time.time(), None))
        return self.workspace(wid)
    def workspace(self, wid: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM workspaces WHERE id = ?",
                            (wid,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["members"] = self.workspace_members(wid)
        out["keys"] = self.workspace_keys(wid)
        return out
    def workspace_for_key(self, kid: str) -> dict | None:
        """The workspace a key belongs to, fully populated. Every result can
        therefore be rolled up to the team that owns it."""
        with self._conn() as c:
            row = c.execute(
                "SELECT w.id FROM workspaces w JOIN api_keys k "
                "ON k.workspace_id = w.id WHERE k.kid = ?", (kid,)).fetchone()
        if row is None:
            return None
        return self.workspace(_db.first(row))
    def workspaces_for_user(self, user_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT w.*, m.role AS role FROM workspaces w "
                "JOIN workspace_members m ON m.workspace_id = w.id "
                "WHERE m.user_id = ? ORDER BY w.created", (user_id,)).fetchall()
        return [dict(r) for r in rows]
    def workspace_members(self, wid: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM workspace_members WHERE workspace_id = ? "
                "ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 "
                "ELSE 2 END, email", (wid,)).fetchall()
        return [dict(r) for r in rows]
    def member_role(self, wid: str, email: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT role FROM workspace_members WHERE workspace_id = ? "
                "AND email = ?",
                (wid, str(email).strip().lower())).fetchone()
        return None if row is None else _db.first(row)
    def add_member(self, wid: str, email: str, role: str = "member",
                   user_id: str | None = None) -> dict:
        email = str(email).strip().lower()
        with self._conn() as c:
            c.execute(
                "INSERT INTO workspace_members (id, workspace_id, email, role, "
                "created, user_id) VALUES (?,?,?,?,?,?)",
                ("wm_" + uuid.uuid4().hex[:16], wid, email, role,
                 time.time(), user_id))
        return {"email": email, "role": role, "workspace_id": wid}
    def set_member_role(self, wid: str, email: str, role: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE workspace_members SET role = ? WHERE workspace_id = ? "
                "AND email = ?", (role, wid, str(email).strip().lower()))
            return cur.rowcount == 1
    def remove_member(self, wid: str, email: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM workspace_members WHERE workspace_id = ? "
                "AND email = ?", (wid, str(email).strip().lower()))
            return cur.rowcount == 1
    def create_invite(self, wid: str, invite: dict) -> dict:
        iid = "wi_" + uuid.uuid4().hex[:16]
        with self._conn() as c:
            c.execute(
                "INSERT INTO workspace_invites (id, workspace_id, email, role, "
                "token_hash, created, expires_at) VALUES (?,?,?,?,?,?,?)",
                (iid, wid, invite["email"], invite["role"],
                 invite["token_hash"], invite["created"],
                 invite["expires_at"]))
        return {"id": iid, "workspace_id": wid, **invite}
    def invites(self, wid: str, pending_only: bool = False) -> list[dict]:
        sql = ("SELECT * FROM workspace_invites WHERE workspace_id = ?"
               + (" AND accepted_at IS NULL" if pending_only else "")
               + " ORDER BY created DESC")
        with self._conn() as c:
            rows = c.execute(sql, (wid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("token_hash", None)  # never leaves the store
            out.append(d)
        return out
    def invite_by_hash(self, token_hash: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM workspace_invites WHERE token_hash = ?",
                (token_hash,)).fetchone()
        return None if row is None else dict(row)
    def revoke_invite(self, wid: str, invite_id: str) -> bool:
        """Only a pending invite can be revoked; an accepted one is history."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM workspace_invites WHERE id = ? AND workspace_id = ? "
                "AND accepted_at IS NULL", (invite_id, wid))
            return cur.rowcount == 1
    def accept_invite(self, invite_id: str, user_id: str | None = None) -> bool:
        """Single use: only an unaccepted invite can be accepted, enforced in
        the WHERE clause so two concurrent accepts cannot both win."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE workspace_invites SET accepted_at = ? WHERE id = ? "
                "AND accepted_at IS NULL", (time.time(), invite_id))
            return cur.rowcount == 1
    def workspace_keys(self, wid: str) -> list[dict]:
        """Credentials for a workspace, without the secret material."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT kid, key_prefix, name, created, qpm_limit, "
                "monthly_allowance, plan FROM api_keys WHERE workspace_id = ? "
                "ORDER BY created", (wid,)).fetchall()
        return [dict(r) for r in rows]
    def set_workspace_plan(self, wid: str, plan: str, seat_limit: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE workspaces SET plan = ?, seat_limit = ? WHERE id = ?",
                (plan, seat_limit, wid))
            return cur.rowcount == 1
