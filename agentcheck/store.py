"""Metering and result store. Every call logged with its request id."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from agentcheck import db as _db
from agentcheck.store_identity import IdentityMixin
from agentcheck.store_keys import KeysMixin, _hash_key
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS metering (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user_key TEXT NOT NULL,
    judge TEXT NOT NULL,
    model TEXT NOT NULL,
    request_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    questions INTEGER NOT NULL,
    server_ms REAL,
    cached INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS api_keys (
    kid TEXT PRIMARY KEY,
    key_hash TEXT UNIQUE NOT NULL,
    key_prefix TEXT NOT NULL,
    prev_key_hash TEXT,
    name TEXT NOT NULL,
    created REAL NOT NULL,
    qpm_limit INTEGER NOT NULL DEFAULT 600
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    judge TEXT NOT NULL,
    dataset TEXT NOT NULL,
    tp INTEGER, fp INTEGER, tn INTEGER, fn INTEGER,
    mean_confidence REAL
);

CREATE TABLE IF NOT EXISTS results (
    id TEXT PRIMARY KEY,
    user_key TEXT NOT NULL,
    ts REAL NOT NULL,
    request TEXT,
    tool TEXT,
    args_json TEXT,
    extra_json TEXT,
    verdict TEXT,
    confidence REAL,
    severity REAL,
    checks_json TEXT NOT NULL,
    usage_json TEXT,
    model TEXT,
    request_id TEXT,
    cached INTEGER NOT NULL DEFAULT 0,
    assessment TEXT,
    run_id TEXT,
    trace_hash TEXT,
    checkset TEXT
);

CREATE INDEX IF NOT EXISTS idx_results_user_ts ON results(user_key, ts DESC);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user_key TEXT NOT NULL,
    event TEXT NOT NULL,
    props_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_key, event, ts);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created REAL NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    seat_limit INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS workspace_members (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    created REAL NOT NULL,
    user_id TEXT
);

CREATE TABLE IF NOT EXISTS workspace_invites (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    token_hash TEXT NOT NULL,
    created REAL NOT NULL,
    expires_at REAL NOT NULL,
    accepted_at REAL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    email TEXT NOT NULL,
    name TEXT,
    created REAL NOT NULL,
    last_login_at REAL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen_at REAL
);
"""

# Indexes that depend on migrated columns run after MIGRATIONS, not in SCHEMA.
POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_results_hash ON results(user_key, trace_hash);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(user_key, run_id);
CREATE INDEX IF NOT EXISTS idx_results_trace ON results(user_key, trace_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_members_unique
    ON workspace_members(workspace_id, email);
CREATE INDEX IF NOT EXISTS idx_invites_ws ON workspace_invites(workspace_id, accepted_at);
CREATE INDEX IF NOT EXISTS idx_keys_ws ON api_keys(workspace_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_provider
    ON users(provider, provider_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_hash ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_invites_hash
    ON workspace_invites(token_hash);
"""

# Columns added after the first release. Existing local stores get them here.
MIGRATIONS = (
    ("api_keys", "kid", "TEXT"),
    ("api_keys", "key_hash", "TEXT"),
    ("api_keys", "key_prefix", "TEXT"),
    ("api_keys", "prev_key_hash", "TEXT"),
    ("results", "run_id", "TEXT"),
    ("results", "trace_hash", "TEXT"),
    ("results", "checkset", "TEXT"),
    ("api_keys", "monthly_allowance", "INTEGER NOT NULL DEFAULT 500"),
    ("api_keys", "plan", "TEXT NOT NULL DEFAULT 'free'"),
    ("api_keys", "trial_ends_at", "REAL"),
    ("results", "trace_id", "TEXT"),
    ("results", "span_id", "TEXT"),
    ("results", "parent_span_id", "TEXT"),
    ("api_keys", "workspace_id", "TEXT"),
    ("results", "decision", "TEXT"),
    ("results", "policy", "TEXT"),
)


class Store(KeysMixin, IdentityMixin):
    def __init__(self, path: str | Path) -> None:
        """A file path (SQLite) or a postgresql:// URL (Postgres)."""
        self.target = str(path)
        self.path = Path(path) if not _db.is_postgres(path) else None
        with self._conn() as c:
            c.executescript(SCHEMA)
            for table, column, decl in MIGRATIONS:
                if column not in _db.columns(c, table):
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            self._migrate_keys(c)
            self._migrate_workspaces(c)
            c.executescript(POST_MIGRATION_INDEXES)

    @property
    def dialect(self) -> str:
        return "postgres" if _db.is_postgres(self.target) else "sqlite"


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

    def _migrate_keys(self, c) -> None:
        """One-way upgrade: plaintext keys -> hash-only with stable kids.

        Every legacy row gets a kid; metering/results/events are re-pointed
        from the raw token to the kid; then the plaintext column is dropped.
        Fresh databases never have it at all.

        Two failure modes this guards against, both real:

        * ``key`` is the PRIMARY KEY in the legacy schema, so SQLite refuses
          ``DROP COLUMN key``. The fallback must blank it, and it must blank
          to a value unique per row: a constant ``''`` collides on the UNIQUE
          constraint as soon as a store holds more than one legacy key, and
          every open then died with IntegrityError — the store could never
          be opened again.
        * The migration runs on every open while the column survives, so it
          must skip rows that already have a kid. Regenerating kids re-points
          nothing (the metering rows moved on the first pass) and silently
          orphans every metered call.
        """
        cols = _db.columns(c, "api_keys")
        if "key" not in cols:
            return
        for r in c.execute(
                "SELECT key, kid FROM api_keys"):
            raw, existing = _db.first(r, 0), _db.first(r, 1)
            if existing:
                continue  # already migrated; re-keying would orphan history
            kid = self._new_kid()
            c.execute(
                "UPDATE api_keys SET kid = ?, key_hash = ?, key_prefix = ? "
                "WHERE key = ?",
                (kid, _hash_key(raw), raw[:8], raw))
            for tbl in ("metering", "results", "events"):
                try:
                    c.execute(f"UPDATE {tbl} SET user_key = ? WHERE user_key = ?",
                              (kid, raw))
                except Exception:
                    pass
        try:
            c.execute("ALTER TABLE api_keys DROP COLUMN key")
        except Exception:
            # Old sqlite without DROP COLUMN, or `key` is part of the PRIMARY
            # KEY: blank the secrets instead. Per-row unique so the UNIQUE
            # constraint holds; no plaintext remains.
            c.execute("UPDATE api_keys SET key = '__migrated_' || kid "
                      "WHERE key IS NOT NULL")

    @contextmanager
    def _conn(self):
        with _db.session(self.target) as conn:
            yield conn

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





    # ── identity ───────────────────────────────────────────────────────














    # ── workspaces ─────────────────────────────────────────────────────

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



    def record(self, **kw) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO metering
                   (ts,user_key,judge,model,request_id,input_tokens,output_tokens,
                    questions,server_ms,cached,ok)
                   VALUES (:ts,:user_key,:judge,:model,:request_id,:input_tokens,
                           :output_tokens,:questions,:server_ms,:cached,:ok)""",
                {"ts": time.time(), **kw},
            )

    def used_in_window(self, user_key: str, seconds: float) -> int:
        with self._conn() as c:
            cur = c.execute(
                "SELECT COALESCE(SUM(questions),0) FROM metering "
                "WHERE user_key = ? AND ts >= ? AND ok = 1 AND cached = 0",
                (user_key, time.time() - seconds),
            )
            return int(_db.first(cur.fetchone()))

    @staticmethod
    def _month_start(now: float | None = None) -> float:
        import calendar
        import datetime
        now = now or time.time()
        dt = datetime.datetime.fromtimestamp(now).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        month_start = dt.timestamp()
        last_day = calendar.monthrange(dt.year, dt.month)[1]
        resets_at = (dt + datetime.timedelta(days=last_day)).timestamp()
        return month_start, resets_at

    def used_this_month(self, user_key: str) -> int:
        """Metered questions this calendar month. Cache replays are free."""
        month_start, _ = self._month_start()
        with self._conn() as c:
            cur = c.execute(
                "SELECT COALESCE(SUM(questions),0) FROM metering "
                "WHERE user_key = ? AND ts >= ? AND ok = 1 AND cached = 0",
                (user_key, month_start),
            )
            return int(_db.first(cur.fetchone()))

    def fail_count_this_month(self, user_key: str) -> int:
        """Flagged verdicts this month — the number the upgrade message quotes."""
        month_start, _ = self._month_start()
        with self._conn() as c:
            cur = c.execute(
                "SELECT COUNT(*) FROM results "
                "WHERE user_key = ? AND ts >= ? AND verdict = 'fail'",
                (user_key, month_start),
            )
            return int(_db.first(cur.fetchone()))

    def record_event(self, user_key: str, event: str, props: dict | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO events (ts, user_key, event, props_json) VALUES (?,?,?,?)",
                (time.time(), user_key, event, json.dumps(props or {})),
            )

    def latest_event(self, user_key: str, event: str) -> dict | None:
        """Most recent event props for this key, with its timestamp.

        Used for slow-moving signals like red-team runs: the trust score
        wants the latest adversarial probe without re-running it.
        Returns None when the key has never produced this event.
        """
        with self._conn() as c:
            r = c.execute(
                "SELECT ts, props_json FROM events WHERE user_key = ? AND event = ? "
                "ORDER BY ts DESC LIMIT 1",
                (user_key, event)).fetchone()
        if not r:
            return None
        try:
            props = json.loads(r["props_json"] or "{}")
        except Exception:
            props = {}
        props = dict(props)
        props["ts"] = r["ts"]
        return props

    def funnel(self) -> dict:
        """Signup -> activated -> engaged -> returning, per key.

        signed_up:  key exists
        activated:  >=1 successful judged call
        engaged:    >=1 human sign-out (assessment)
        returning:  judged call in the trailing 7 days
        """
        with self._conn() as c:
            keys = [r["kid"] for r in c.execute("SELECT kid FROM api_keys").fetchall()]
            checked = {r["user_key"] for r in c.execute(
                "SELECT DISTINCT user_key FROM metering WHERE ok = 1 AND cached = 0").fetchall()}
            signed = {r["user_key"] for r in c.execute(
                "SELECT DISTINCT user_key FROM results WHERE assessment IS NOT NULL").fetchall()}
            recent = {r["user_key"] for r in c.execute(
                "SELECT DISTINCT user_key FROM metering "
                "WHERE ok = 1 AND cached = 0 AND ts >= ?",
                (time.time() - 7 * 86400,)).fetchall()}
        stages = {
            "signed_up": len(keys),
            "activated": len(set(keys) & checked),
            "engaged": len(set(keys) & signed),
            "returning_7d": len(set(keys) & recent),
        }
        # Step conversion only where the stage is a true subset of the
        # previous one. returning_7d is measured against activated — a key
        # can have recent checks without ever signing one out.
        conv = {}
        base = {"signed_up": None, "activated": "signed_up",
                "engaged": "activated", "returning_7d": "activated"}
        for stage, against in base.items():
            if against is None:
                conv[stage] = 1.0 if stages[stage] else 0.0
            else:
                denom = stages[against]
                conv[stage] = (stages[stage] / denom) if denom else 0.0
        return {"stages": stages, "step_conversion": conv}

    def runs(self, user_key: str, limit: int = 50,
             only: str | None = None, tool: str | None = None) -> list[dict]:
        """Agent runs (traces with steps), newest first, with a summary.

        Grouped and aggregated in SQL rather than by pulling rows and
        folding in Python: a busy key has more steps than a page should read.
        Legacy rows with no trace_id are simply not runs.
        """
        where = ["user_key = ?", "trace_id IS NOT NULL"]
        params: list[Any] = [user_key]
        if tool:
            where.append("trace_id IN (SELECT trace_id FROM results "
                         "WHERE user_key = ? AND tool = ?)")
            params += [user_key, tool]
        sql = (
            "SELECT trace_id AS tid, COUNT(*) AS n, MIN(ts) AS started, "
            "MAX(ts) AS ended, "
            "SUM(CASE WHEN verdict = 'fail' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN verdict = 'review' THEN 1 ELSE 0 END) AS review, "
            "SUM(CASE WHEN verdict = 'pass' THEN 1 ELSE 0 END) AS passed "
            "FROM results WHERE " + " AND ".join(where) +
            " GROUP BY trace_id")
        if only == "blocked":
            sql += (" HAVING SUM(CASE WHEN verdict = 'fail' THEN 1 ELSE 0 END) > 0")
        elif only == "review":
            sql += (" HAVING SUM(CASE WHEN verdict = 'review' THEN 1 ELSE 0 END) > 0")
        sql += " ORDER BY started DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        out = [dict(r) for r in rows]
        if not out:
            return []
        # one extra query for the tools in this page, not one per run
        ids = [r["tid"] for r in out]
        marks = ",".join("?" * len(ids))
        with self._conn() as c:
            steps = c.execute(
                f"SELECT trace_id, tool, verdict, confidence, decision "
                f"FROM results WHERE user_key = ? AND trace_id IN ({marks}) "
                f"ORDER BY ts ASC", [user_key, *ids]).fetchall()
        by_run: dict[str, list[dict]] = {}
        for s in steps:
            by_run.setdefault(_db.first(s, 0), []).append({
                "tool": _db.first(s, 1), "verdict": _db.first(s, 2),
                "confidence": _db.first(s, 3), "decision": _db.first(s, 4)})
        for r in out:
            r["trace_id"] = r.pop("tid")
            steps_ = by_run.get(r["trace_id"], [])
            r["tools"] = [s["tool"] for s in steps_]
            r["blocked"] = bool(r.get("failed"))
            r["step_count"] = len(steps_)
            r["duration_ms"] = round(
                max(0.0, float(r.get("ended") or 0)
                    - float(r.get("started") or 0)) * 1000, 1)
        return out

    def trace_steps(self, user_key: str, trace_id: str, limit: int = 500) -> list[dict]:
        """One agent run, oldest first. Empty when the id is unknown."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM results WHERE user_key = ? AND trace_id = ? "
                "ORDER BY ts ASC LIMIT ?",
                (user_key, trace_id, limit)).fetchall()
        return [self._public(dict(r)) for r in rows]

    def results_by_run(self, user_key: str, run_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM results WHERE user_key = ? AND run_id = ? ORDER BY ts",
                (user_key, run_id),
            ).fetchall()
        return [self._public(dict(r)) for r in rows]

    def totals(self, user_key: str | None = None) -> dict:
        with self._conn() as c:
            if user_key:
                cur = c.execute(
                    """SELECT COUNT(*) calls, SUM(questions) questions,
                              SUM(input_tokens) in_tok, SUM(output_tokens) out_tok,
                              SUM(cached) cached
                       FROM metering WHERE user_key = ?""",
                    (user_key,),
                )
            else:
                cur = c.execute(
                    """SELECT COUNT(*) calls, SUM(questions) questions,
                              SUM(input_tokens) in_tok, SUM(output_tokens) out_tok,
                              SUM(cached) cached
                       FROM metering"""
                )
            row = cur.fetchone()
            return {k: (row[k] or 0) for k in row.keys()}

    def per_user(self) -> list[dict]:
        with self._conn() as c:
            cur = c.execute(
                """SELECT user_key, SUM(questions) questions,
                          SUM(input_tokens+output_tokens) tokens, COUNT(*) calls
                   FROM metering GROUP BY user_key ORDER BY tokens DESC"""
            )
            return [dict(r) for r in cur.fetchall()]

    def record_eval(self, **kw) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO eval_runs
                   (ts,judge,dataset,tp,fp,tn,fn,mean_confidence)
                   VALUES (:ts,:judge,:dataset,:tp,:fp,:tn,:fn,:mean_confidence)""",
                {"ts": time.time(), **kw},
            )

    def save_result(self, user_key: str, trace: dict, payload: dict,
                    run_id: str | None = None,
                    checkset: str | None = None,
                    trace_id: str | None = None,
                    span_id: str | None = None,
                    parent_span_id: str | None = None) -> dict:
        """Store a result, or return the existing row for an identical trace.

        Dedupe is deliberate: re-clicking an example should not grow the log.
        One row per distinct (request, tool, args) per account.
        A trace_id opts out: steps in an agent run are events, so the same
        tool called twice in one run stores two rows.
        """
        digest = trace_digest(trace, checkset)
        if not trace_id:
            existing = self.find_by_hash(user_key, digest)
            if existing is not None:
                row = dict(existing)
                row["duplicate"] = True
                return self._public(row)

        result_id = "rs_" + uuid.uuid4().hex[:16]
        extra = {k: v for k, v in trace.items() if k not in ("request", "tool", "args")}
        row = {
            "id": result_id,
            "user_key": user_key,
            "ts": time.time(),
            "request": str(trace.get("request") or ""),
            "tool": str(trace.get("tool") or ""),
            "args_json": json.dumps(trace.get("args", {}), default=str),
            "extra_json": json.dumps(extra, default=str),
            "verdict": payload.get("trace_verdict"),
            "confidence": payload.get("confidence"),
            "severity": payload.get("severity"),
            "checks_json": json.dumps(payload.get("checks") or {}, default=str),
            "usage_json": json.dumps(payload.get("usage") or {}, default=str),
            "model": payload.get("model"),
            "request_id": (payload.get("usage") or {}).get("request_id"),
            "cached": 1 if payload.get("cached") else 0,
            "assessment": None,
            "run_id": run_id,
            "trace_hash": digest,
            "checkset": checkset,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "decision": payload.get("decision"),
            "policy": payload.get("policy"),
        }
        with self._conn() as c:
            c.execute(
                """INSERT INTO results
                   (id,user_key,ts,request,tool,args_json,extra_json,verdict,confidence,
                    severity,checks_json,usage_json,model,request_id,cached,assessment,
                    run_id,trace_hash,checkset,trace_id,span_id,parent_span_id,
                    decision,policy)
                   VALUES (:id,:user_key,:ts,:request,:tool,:args_json,:extra_json,:verdict,
                           :confidence,:severity,:checks_json,:usage_json,:model,:request_id,
                           :cached,:assessment,:run_id,:trace_hash,:checkset,:trace_id,
                           :span_id,:parent_span_id,:decision,:policy)""",
                row,
            )
        out = self._public(row)
        out["duplicate"] = False
        return out

    def find_by_hash(self, user_key: str, trace_hash: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM results WHERE user_key = ? AND trace_hash = ? LIMIT 1",
                (user_key, trace_hash),
            ).fetchone()

    def results(self, user_key: str, verdict: str | None = None,
                limit: int = 500) -> list[dict]:
        sql = "SELECT * FROM results WHERE user_key = ?"
        params: list[Any] = [user_key]
        if verdict in ("pass", "review", "fail"):
            sql += " AND verdict = ?"
            params.append(verdict)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._public(dict(r)) for r in rows]

    def result_counts(self, user_key: str) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT verdict, COUNT(*) n FROM results WHERE user_key = ? GROUP BY verdict",
                (user_key,),
            ).fetchall()
        out = {"pass": 0, "review": 0, "fail": 0, "total": 0}
        for r in rows:
            key = r["verdict"] if r["verdict"] in out else None
            if key:
                out[key] = r["n"]
            out["total"] += r["n"]
        with self._conn() as c:
            out["assessed"] = _db.first(c.execute(
                "SELECT COUNT(*) FROM results WHERE user_key = ? AND assessment IS NOT NULL",
                (user_key,)).fetchone())
            out["batches"] = _db.first(c.execute(
                "SELECT COUNT(DISTINCT run_id) FROM results "
                "WHERE user_key = ? AND run_id IS NOT NULL",
                (user_key,)).fetchone())
        return out

    def result(self, user_key: str, result_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM results WHERE user_key = ? AND id = ?",
                (user_key, result_id),
            ).fetchone()
        return None if row is None else self._public(dict(row))

    def assess(self, user_key: str, result_id: str, assessment: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE results SET assessment = ? WHERE user_key = ? AND id = ?",
                (assessment, user_key, result_id),
            )
            ok = cur.rowcount == 1
            if ok:
                c.execute(
                    "INSERT INTO events (ts, user_key, event, props_json) VALUES (?,?,?,?)",
                    (time.time(), user_key, "signed_out",
                     json.dumps({"result_id": result_id, "assessment": assessment})),
                )
            return ok

    def delete_result(self, user_key: str, result_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM results WHERE user_key = ? AND id = ?",
                (user_key, result_id),
            )
            return cur.rowcount == 1

    @staticmethod
    def _public(row: dict[str, Any]) -> dict:
        args = row.get("args_json")
        extra = row.get("extra_json")
        checks = row.get("checks_json")
        usage = row.get("usage_json")
        return {
            "id": row["id"],
            "ts": row["ts"],
            "request": row.get("request") or "",
            "tool": row.get("tool") or "",
            "args": json.loads(args) if isinstance(args, str) else (args or {}),
            "extra": json.loads(extra) if isinstance(extra, str) else (extra or {}),
            "trace_verdict": row.get("verdict"),
            "confidence": row.get("confidence"),
            "severity": row.get("severity"),
            "checks": json.loads(checks) if isinstance(checks, str) else (checks or {}),
            "usage": json.loads(usage) if isinstance(usage, str) else (usage or {}),
            "model": row.get("model"),
            "cached": bool(row.get("cached")),
            "assessment": row.get("assessment"),
            "run_id": row.get("run_id"),
            "checkset": row.get("checkset"),
            "trace_id": row.get("trace_id"),
            "span_id": row.get("span_id"),
            "parent_span_id": row.get("parent_span_id"),
            "decision": row.get("decision"),
            "policy": row.get("policy"),
            "duplicate": row.get("duplicate", False),
        }


def trace_digest(trace: dict, checkset: str | None = None) -> str:
    """Stable identity for a trace, so the log holds one row per distinct call.

    The rubric is part of the identity. The same trace judged by ``safety`` and
    by ``refund-policy`` produces different verdicts, so deduping across rubrics
    would silently return another rubric's answer.
    """
    blob = json.dumps(
        {
            "request": str(trace.get("request") or "").strip(),
            "tool": str(trace.get("tool") or "").strip(),
            "args": trace.get("args") or {},
            "question": str(trace.get("question") or "").strip() or None,
            "checkset": checkset,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()
