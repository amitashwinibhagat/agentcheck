""" API keys and their plans: minting, lookup, rotation, allowances. 

Never instantiated directly; mixed into Store.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
import sqlite3
from agentcheck import db as _db


def _hash_key(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()


class KeysMixin:
    @staticmethod
    def _new_kid() -> str:
        return "kid_" + uuid.uuid4().hex[:16]
    def create_key(self, name: str, qpm_limit: int = 600,
                   monthly_allowance: int = 500, plan: str = "free",
                   trial_days: int | None = None,
                   workspace_id: str | None = None,
                   role: str | None = None) -> str:
        """Create a key. The raw token is returned ONCE and never stored;
        only its hash lives in the DB from here on.

        Without workspace_id the key gets its own fresh workspace (CLI
        issuance, migrations). With one the key joins that workspace and no
        new workspace is spawned — the signup flow, where the workspace
        already exists. A bad id fails loudly rather than orphaning a key.

        `role` is the authority the credential acts with. It must never exceed
        the creator's role; callers pass their own.
        """
        import secrets
        key = "ac_" + secrets.token_urlsafe(32)
        now = time.time()
        trial_ends_at = (now + trial_days * 86400) if trial_days else None
        kid = self._new_kid()
        with self._conn() as c:
            if workspace_id is None:
                c.execute(
                    "INSERT INTO api_keys (kid, key_hash, key_prefix, name, created, "
                    "qpm_limit, monthly_allowance, plan, trial_ends_at, role) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (kid, _hash_key(key), key[:8], name, now, qpm_limit,
                     monthly_allowance, plan, trial_ends_at, role or "owner"),
                )
                self._attach_workspace(c, kid, name, plan=plan)
            else:
                exists = c.execute("SELECT 1 FROM workspaces WHERE id = ?",
                                   (workspace_id,)).fetchone()
                if exists is None:
                    raise ValueError(f"unknown workspace: {workspace_id}")
                c.execute(
                    "INSERT INTO api_keys (kid, key_hash, key_prefix, name, created, "
                    "qpm_limit, monthly_allowance, plan, trial_ends_at, "
                    "workspace_id, role) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (kid, _hash_key(key), key[:8], name, now, qpm_limit,
                     monthly_allowance, plan, trial_ends_at, workspace_id,
                     role or "member"),
                )
            c.execute(
                "INSERT INTO events (ts, user_key, event, props_json) VALUES (?,?,?,?)",
                (now, kid, "key_created",
                 json.dumps({"name": name, "plan": plan,
                             "prefix": key[:8]})),
            )
        return key
    def lookup_kid(self, kid: str) -> sqlite3.Row | None:
        """Look up by stable key id (post-auth identity, metering owner)."""
        with self._conn() as c:
            return c.execute("SELECT * FROM api_keys WHERE kid = ?",
                             (kid,)).fetchone()
    def lookup_key(self, key: str) -> sqlite3.Row | None:
        """Look up by raw token. Matches the current hash or the previous
        one (rotation overlap), so a rotation never 401s a live client."""
        h = _hash_key(key)
        with self._conn() as c:
            row = c.execute("SELECT * FROM api_keys WHERE key_hash = ?",
                            (h,)).fetchone()
            if row is None:
                row = c.execute("SELECT * FROM api_keys WHERE prev_key_hash = ?",
                                (h,)).fetchone()
            return row
    def ensure_named_key(self, name: str, **kw) -> str:
        """Singleton keys (demo/browser): rotate with overlap and return the
        fresh raw token. The previous token keeps working until the NEXT
        rotation, so restarts never strand live clients."""
        import secrets
        with self._conn() as c:
            row = c.execute("SELECT * FROM api_keys WHERE name = ? "
                            "ORDER BY created DESC LIMIT 1", (name,)).fetchone()
            fresh = "ac_" + secrets.token_urlsafe(32)
            if row is None:
                new_kid = self._new_kid()
                c.execute(
                    "INSERT INTO api_keys (kid, key_hash, key_prefix, name, "
                    "created, qpm_limit, monthly_allowance, plan) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (new_kid, _hash_key(fresh), fresh[:8], name,
                     time.time(), kw.get("qpm_limit", 600),
                     kw.get("monthly_allowance", 500),
                     kw.get("plan", "free")))
                self._attach_workspace(c, new_kid, name,
                                       plan=kw.get("plan", "free"))
            else:
                # Limits are refreshed on rotation too: raising the cap in a
                # new release must reach a key that already exists.
                c.execute("UPDATE api_keys SET prev_key_hash = key_hash, "
                          "key_hash = ?, key_prefix = ?, qpm_limit = ?, "
                          "monthly_allowance = ? WHERE kid = ?",
                          (_hash_key(fresh), fresh[:8],
                           kw.get("qpm_limit", row["qpm_limit"]),
                           kw.get("monthly_allowance", row["monthly_allowance"]),
                           row["kid"]))
            return fresh
    def set_plan(self, kid: str, plan: str, allowance: int, qpm: int,
                 subscription_id: str | None = None) -> bool:
        """Move a key to a plan. Allowance and qpm are passed in from OUR
        catalogue, so a provider payload can never set a number."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE api_keys SET plan = ?, monthly_allowance = ?, "
                "qpm_limit = ? WHERE kid = ?",
                (plan, allowance, qpm, kid))
            return cur.rowcount == 1
    def key_meta(self, key: str) -> dict | None:
        """Metadata for a key, addressed by either identity.

        Callers pass a kid (post-auth, from _authorize) or a raw token
        (CLI, tests). Resolving both here is load-bearing: an unresolved
        key silently falls back to the 500-question default, which made a
        generous demo allowance read as exhausted.
        """
        row = self.lookup_kid(key)
        if row is None:
            row = self.lookup_key(key)
        if row is None:
            return None
        return {
            "name": row["name"],
            "created": row["created"],
            "qpm_limit": row["qpm_limit"],
            "monthly_allowance": row["monthly_allowance"]
            if "monthly_allowance" in row.keys() else 500,
            "plan": row["plan"] if "plan" in row.keys() else "free",
            "trial_ends_at": row["trial_ends_at"]
            if "trial_ends_at" in row.keys() else None,
            "trial_active": bool(
                (row["plan"] if "plan" in row.keys() else "free") == "trial"
                and (row["trial_ends_at"] if "trial_ends_at" in row.keys() else 0)
                and row["trial_ends_at"] > time.time()
            ),
        }
