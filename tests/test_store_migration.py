"""The key-hashing migration, on the shape that actually broke.

Every other test builds a fresh store, so the upgrade path had no coverage —
and the upgrade path is the only place the plaintext column exists. It failed
on any store holding more than one legacy key: `key` is the PRIMARY KEY, so
SQLite refuses `DROP COLUMN key`, and the fallback blanked every row to `''`
on the same UNIQUE column. Every open then raised IntegrityError, so the
store could never be opened again and the keys stayed plaintext at rest —
which is the invariant the migration exists to protect.

These tests build the legacy schema by hand because nothing else does.
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcheck.store import Store  # noqa: E402


def _legacy(path: Path, n: int = 3) -> list[str]:
    """A pre-hash store: `key` is PRIMARY KEY, meter rows keyed by the token."""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE api_keys (
            key TEXT PRIMARY KEY, name TEXT, created REAL, qpm_limit INTEGER,
            monthly_allowance INTEGER, plan TEXT, trial_ends_at REAL);
        CREATE TABLE metering (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, user_key TEXT, judge TEXT, model TEXT, request_id TEXT,
            input_tokens INTEGER, output_tokens INTEGER, questions INTEGER,
            server_ms REAL, cached INTEGER, ok INTEGER);
        CREATE TABLE results (
            id TEXT PRIMARY KEY, user_key TEXT, ts REAL, request TEXT, tool TEXT,
            args_json TEXT, trace_hash TEXT);
    """)
    plain = []
    for i in range(n):
        raw = f"ac_legacy{i}"
        plain.append(raw)
        c.execute("INSERT INTO api_keys(key,name,qpm_limit) VALUES(?,?,?)",
                  (raw, f"key-{i}", 100))
        c.execute("INSERT INTO metering(ts,user_key,judge,model,questions,cached,ok)"
                  " VALUES(1,?,'typesafe','jev',5,0,1)", (raw,))
        c.execute("INSERT INTO results(id,user_key,ts,request,tool,args_json,trace_hash)"
                  " VALUES(?,?,1,'r','t','{}','h')", (f"rs_{i}", raw))
    c.commit()
    c.close()
    return plain


def test_migration_multi_key_store_opens_and_hashes():
    """The exact shape that crashed: >1 legacy key, `key` the PRIMARY KEY."""
    tmp = Path(tempfile.mkdtemp()) / "legacy.db"
    plain = _legacy(tmp)
    store = Store(tmp)  # must not raise

    c = sqlite3.connect(tmp)
    rows = c.execute("SELECT kid, key_hash, key FROM api_keys").fetchall()
    assert len(rows) == len(plain)

    for kid, key_hash, key in rows:
        assert kid, "every migrated row needs a kid"
        assert key_hash, "every migrated row needs a hash"
        # The column survives (it is part of the PRIMARY KEY) but must hold
        # no live credential. A constant '' used to collide on UNIQUE here.
        assert not str(key).startswith("ac_legacy"), \
            f"plaintext key survived migration: {key}"
    assert len({r[0] for r in rows}) == len(plain), "kids must be unique"

    # History is re-pointed from the raw token to the kid, not dropped.
    metered = {r[0] for r in c.execute("SELECT DISTINCT user_key FROM metering")}
    assert metered == {r[0] for r in rows}, "metering rows must move to kids"
    results = {r[0] for r in c.execute("SELECT DISTINCT user_key FROM results")}
    assert results == {r[0] for r in rows}, "results must move to kids"
    c.close()

    # The key still works — the point of the migration is that it does.
    for raw in plain:
        assert store.lookup_key(raw) is not None, "a migrated key must still authenticate"


def test_migration_is_idempotent():
    """It re-runs on every open while the column survives. Re-keying on the
    second pass would generate new kids and silently orphan every metered
    call, because the meter rows already point at the first pass's kids."""
    tmp = Path(tempfile.mkdtemp()) / "legacy.db"
    _legacy(tmp)
    Store(tmp)
    c = sqlite3.connect(tmp)
    before = [r[0] for r in c.execute("SELECT kid FROM api_keys ORDER BY name")]
    c.close()

    Store(tmp)  # second open
    c = sqlite3.connect(tmp)
    after = [r[0] for r in c.execute("SELECT kid FROM api_keys ORDER BY name")]
    metered = {r[0] for r in c.execute("SELECT DISTINCT user_key FROM metering")}
    c.close()

    assert before == after, "kids must be stable across opens"
    assert metered == set(after), "history must not be orphaned by a second pass"


def test_fresh_store_has_no_plaintext_column():
    """The migration must not touch a store that never had the column."""
    tmp = Path(tempfile.mkdtemp()) / "fresh.db"
    store = Store(tmp)
    key = store.create_key("fresh")
    assert store.lookup_key(key) is not None
    cols = [r[1] for r in sqlite3.connect(tmp).execute("PRAGMA table_info(api_keys)")]
    assert "key" not in cols, "fresh schema must never grow a plaintext column"
