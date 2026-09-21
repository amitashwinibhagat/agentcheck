"""The verdict log: saved results, runs, trace steps, human sign-out.

Never instantiated on its own; mixed into Store so the class keeps one
public API. The free function(s) below moved with their only caller.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any
from agentcheck import db as _db


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


class VerdictMixin:
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
    def total_results(self) -> int:
        """Every result in the store, regardless of key.

        `doctor` needs the store-wide count; `totals(key)` is per workspace, and
        passing None to it returned a cheerful 0 — which reads as "empty store"
        rather than "wrong question".
        """
        with self._conn() as c:
            return _db.first(c.execute("SELECT COUNT(*) FROM results").fetchone())
    def result(self, user_key: str, result_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM results WHERE user_key = ? AND id = ?",
                (user_key, result_id),
            ).fetchone()
        return None if row is None else self._public(dict(row))
    def signoff_counts(self, user_key: str | None = None,
                       gate: float = 0.6) -> dict:
        """Sign-out counts, with the three numbers kept distinct.

        `total`  — every row a person signed out (any assessment)
        `signal` — the ones carrying signal (looks_correct / actual_issue)
        `decided`— those at or above the gate, i.e. what a calibration counts

        They were one number once, which made `total` mean two things
        depending on who asked. Counted in SQL because the badge endpoint
        asks on every render and pulling rows to count them would be silly.
        """
        sql = (
            "SELECT COUNT(*), "
            "SUM(CASE WHEN assessment IN ('looks_correct','actual_issue') "
            "THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN assessment IN ('looks_correct','actual_issue') "
            "AND confidence >= ? THEN 1 ELSE 0 END) "
            "FROM results WHERE assessment IS NOT NULL")
        args: tuple = (gate,)
        if user_key is not None:
            sql += " AND user_key = ?"
            args = (gate, user_key)
        with self._conn() as c:
            row = c.execute(sql, args).fetchone()
        total, signal, decided = (_db.first(row, 0), _db.first(row, 1),
                                  _db.first(row, 2))
        return {"total": int(total or 0), "signal": int(signal or 0),
                "decided": int(decided or 0)}
    def signoffs(self, user_key: str | None, limit: int = 5000) -> list[dict]:
        """Every signed-out row, with the judge's confidence and verdict.

        The raw material for a calibration measured against the user's own
        labels rather than ours. The math drops what carries no signal (see
        calibration.report_from_signoffs); this returns the rows.

        `user_key=None` means every key in the store — what a single-operator
        local install wants from `agentcheck calibrate --from-signoffs`. The
        HTTP path always passes a key, so a hosted workspace can never see
        another's labels.
        """
        sql = ("SELECT id, ts, verdict, confidence, severity, checkset, "
               "assessment, model FROM results WHERE assessment IS NOT NULL")
        args: tuple = ()
        if user_key is not None:
            sql += " AND user_key = ?"
            args = (user_key,)
        sql += " ORDER BY ts DESC LIMIT ?"
        with self._conn() as c:
            rows = c.execute(sql, (*args, limit)).fetchall()
        return [dict(r) for r in rows]
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

    # ── waitlist: demand before there is anything to sell ──────────────────
    def join_waitlist(self, email: str, plan: str | None = None,
                      note: str | None = None) -> bool:
        """Record interest. Returns False when already on the list.

        Deliberately idempotent: someone clicking twice is not two prospects,
        and a count that double-counts is worse than no count. The rows are
        small and a real signup flow replaces them.
        """
        e = str(email).strip().lower()
        if "@" not in e or len(e) > 200 or " " in e:
            raise ValueError("a valid email address is required")
        with self._conn() as c:
            exists = c.execute("SELECT 1 FROM waitlist WHERE email = ?",
                               (e,)).fetchone()
            if exists is not None:
                return False
            c.execute("INSERT INTO waitlist (email, created, plan, note) "
                      "VALUES (?,?,?,?)", (e, time.time(), plan, note))
        return True

    def waitlist(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT email, created, plan, note FROM waitlist "
                             "ORDER BY created DESC").fetchall()
        return [dict(r) for r in rows]
