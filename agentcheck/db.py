"""Database backend: SQLite by default, Postgres when asked.

The store speaks one dialect: ``?`` placeholders, dict-like rows, and the
schema in store.SCHEMA. This module is the only place that knows there are
two engines. Postgres arrives via AGENTCHECK_DB_URL
(postgresql://...); everything else is a file path.

Deliberate limits: DDL is translated, not abstracted (AUTOINCREMENT ->
SERIAL); migrations use information_schema on Postgres, PRAGMA on SQLite.
If a query ever needs engine-specific SQL, it branches here, not in the
store.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def is_postgres(target: str) -> bool:
    return str(target).startswith("postgres")


def first(row, pos: int = 0):
    """Positional read that works for sqlite3.Row and pg dicts."""
    if row is None:
        return None
    if isinstance(row, dict):
        return list(row.values())[pos]
    return row[pos]


_NAMED = re.compile(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)")


class Conn:
    """Uniform connection: execute/commit/close, dict rows, ? placeholders."""

    def __init__(self, raw, dialect: str):
        self._raw = raw
        self.dialect = dialect

    def execute(self, sql: str, params=()):
        if self.dialect == "postgres":
            sql = sql.replace("?", "%s")
            sql = _NAMED.sub(r"%(\1)s", sql)
            cur = self._raw.cursor(row_factory=__import__(
                "psycopg.rows").rows.dict_row)
            cur.execute(sql, params)
            return cur
        cur = self._raw.cursor()
        cur.execute(sql, params)
        return cur

    def executescript(self, sql: str):
        if self.dialect == "postgres":
            ddl = translate_ddl(sql, "postgres")
            with self._raw.cursor() as cur:
                for stmt in [s for s in ddl.split(";") if s.strip()]:
                    cur.execute(stmt)
            return None
        return self._raw.executescript(sql)

    def commit(self):
        self._raw.commit()

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass


def translate_ddl(sql: str, dialect: str) -> str:
    if dialect == "postgres":
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                          "SERIAL PRIMARY KEY")
    return sql


def columns(conn: Conn, table: str) -> set[str]:
    if conn.dialect == "postgres":
        with conn._raw.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = %s", (table,))
            return {r[0] for r in cur.fetchall()}
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in cur.fetchall()}


def connect(target: str | Path) -> Conn:
    target = str(target)
    if is_postgres(target):
        import psycopg
        raw = psycopg.connect(target)
        raw.autocommit = False
        return Conn(raw, "postgres")
    p = Path(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(p))
    raw.row_factory = sqlite3.Row
    return Conn(raw, "sqlite")


@contextmanager
def session(target: str | Path):
    """Yield a Conn; commit on success, close always."""
    conn = connect(target)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
