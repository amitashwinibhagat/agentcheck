""" Sign-in identity: users, sessions, and binding people to memberships. 

Never instantiated directly; mixed into Store.
"""

from __future__ import annotations

import time
import uuid
from agentcheck import db as _db


class IdentityMixin:
    def upsert_user(self, provider: str, provider_user_id: str, email: str,
                    name: str | None = None) -> dict:
        """Find or create the person behind a provider account.

        Identity is (provider, provider_user_id), never the email: emails get
        reassigned, and treating one as an identity is how an account is
        hijacked.
        """
        email = str(email).strip().lower()
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM users WHERE provider = ? AND "
                "provider_user_id = ?", (provider, provider_user_id)).fetchone()
            if row is not None:
                c.execute("UPDATE users SET email = ?, name = COALESCE(?, name), "
                          "last_login_at = ? WHERE id = ?",
                          (email, name, now, _db.first(row)))
                uid = _db.first(row)
            else:
                uid = "user_" + uuid.uuid4().hex[:16]
                c.execute(
                    "INSERT INTO users (id, provider, provider_user_id, email, "
                    "name, created, last_login_at) VALUES (?,?,?,?,?,?,?)",
                    (uid, provider, provider_user_id, email, name, now, now))
        return self.user(uid)
    def user(self, user_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE id = ?",
                            (user_id,)).fetchone()
        return None if row is None else dict(row)
    def unclaimed_memberships(self, email: str) -> list[dict]:
        """Invites already waiting for this address, not yet bound."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM workspace_members WHERE LOWER(email) = ? "
                "AND user_id IS NULL", (str(email).strip().lower(),)).fetchall()
        return [dict(r) for r in rows]
    def bind_memberships(self, email: str, user_id: str) -> int:
        """Claim every unclaimed membership for this address. Returns how
        many were claimed."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE workspace_members SET user_id = ? WHERE LOWER(email) = ? "
                "AND user_id IS NULL",
                (user_id, str(email).strip().lower()))
            return cur.rowcount
    def link_member(self, wid: str, email: str, user_id: str) -> bool:
        """Attach a user to an unclaimed membership. Never re-points one that
        already belongs to somebody."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE workspace_members SET user_id = ? WHERE workspace_id = ? "
                "AND LOWER(email) = ? AND user_id IS NULL",
                (user_id, wid, str(email).strip().lower()))
            return cur.rowcount == 1
    def materialize_invite(self, invite: dict,
                           user_id: str | None = None) -> bool:
        """Turn an invite into a membership. The bridge that was missing.

        An invite lived in ``workspace_invites`` while memberships live in
        ``workspace_members``; without this, an invited person could sign in
        and still not be a member. Seats are checked here too: an invite
        created while a plan had room must not quietly mint a seat on a plan
        that no longer does, so a full workspace leaves the invite pending.
        """
        from agentcheck import workspaces as _ws
        wid = invite["workspace_id"]
        email = str(invite["email"]).strip().lower()
        existing = self.member_role(wid, email)
        if existing is None:
            ws = self.workspace(wid)
            if ws is None:
                return False
            try:
                _ws.check_seat_available(ws["seat_limit"], ws["members"],
                                         plan=ws["plan"])
            except _ws.WorkspaceError:
                return False  # no seat: leave the invite pending
            self.add_member(wid, email, invite.get("role") or "member",
                            user_id)
        elif user_id:
            self.link_member(wid, email, user_id)
        self.accept_invite(invite["id"], user_id)
        return True
    def claim_invites(self, email: str, user_id: str) -> list[dict]:
        """Every pending invite for this address becomes a membership.

        Called on sign-in: proving you own the address is exactly what the
        invite was waiting for. Returns what was claimed.
        """
        e = str(email).strip().lower()
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM workspace_invites WHERE LOWER(email) = ? "
                "AND accepted_at IS NULL", (e,)).fetchall()
        claimed = []
        for r in rows:
            inv = dict(r)
            if self.materialize_invite(inv, user_id):
                claimed.append({"workspace_id": inv["workspace_id"],
                                "role": inv.get("role") or "member"})
        return claimed
    def create_session(self, session: dict) -> dict:
        sid = "ses_" + uuid.uuid4().hex[:16]
        with self._conn() as c:
            c.execute(
                "INSERT INTO sessions (id, token_hash, user_id, created, "
                "expires_at, last_seen_at) VALUES (?,?,?,?,?,?)",
                (sid, session["token_hash"], session["user_id"],
                 session["created"], session["expires_at"],
                 session["created"]))
        return {"id": sid, **session}
    def session_by_token(self, token: str) -> dict | None:
        """Look up a session by its raw cookie value. The token is hashed on
        the way in, so the table holds no live credential."""
        from agentcheck import auth as _auth
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE token_hash = ?",
                            (_auth.hash_session(token),)).fetchone()
        return None if row is None else dict(row)
    def delete_session(self, token: str) -> bool:
        from agentcheck import auth as _auth
        with self._conn() as c:
            cur = c.execute("DELETE FROM sessions WHERE token_hash = ?",
                            (_auth.hash_session(token),))
            return cur.rowcount == 1
    def delete_user_sessions(self, user_id: str) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return cur.rowcount
    def touch_session(self, token: str) -> None:
        from agentcheck import auth as _auth
        with self._conn() as c:
            c.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                      (time.time(), _auth.hash_session(token)))
    def purge_expired_sessions(self) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM sessions WHERE expires_at < ?",
                            (time.time(),))
            return cur.rowcount
