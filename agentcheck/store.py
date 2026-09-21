"""Metering and result store. Every call logged with its request id.

Assembly only: the schema, the migrations, the connection, and the class that
mixes the five domain mixins together. Nothing here knows what a workspace or
a verdict is -- see store_keys, store_identity, store_tenancy, store_meter and
store_verdict.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from agentcheck import db as _db
from agentcheck.store_identity import IdentityMixin
from agentcheck.store_keys import KeysMixin, _hash_key
from agentcheck.store_meter import MeterMixin
from agentcheck.store_tenancy import TenancyMixin
from agentcheck.store_verdict import VerdictMixin, trace_digest  # noqa: F401  (re-exported)

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
    # The authority a credential carries. A key minted INTO a workspace has no
    # member row (it is a credential, not a person), so without this it derived
    # role "member" — an owner's own first key could not invite anyone.
    ("api_keys", "role", "TEXT"),
    ("results", "decision", "TEXT"),
    ("results", "policy", "TEXT"),
)


class Store(KeysMixin, IdentityMixin, TenancyMixin, MeterMixin, VerdictMixin):
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






    # ── identity ───────────────────────────────────────────────────────














    # ── workspaces ─────────────────────────────────────────────────────










































