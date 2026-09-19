"""The meter: quota windows, monthly usage, events and the funnel.

Never instantiated on its own; mixed into Store so the class keeps one
public API. The free function(s) below moved with their only caller.
"""

from __future__ import annotations

import json
import time
from agentcheck import db as _db


class MeterMixin:
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
