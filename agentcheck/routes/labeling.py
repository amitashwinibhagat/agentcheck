"""The labeling batch: which calls to sign out to earn a measured tier."""

from fastapi import Header, Query

from agentcheck import labeling
from agentcheck.routes import shared


def register(app, store):
    @app.get("/v1/labeling/batch")
    async def labeling_batch(authorization: str | None = Header(None),
                             size: int = Query(default=30, ge=1, le=200)):
        """A stratified to-do list of unlabeled calls.

        Log order is the wrong sample: a queue is mostly near-identical
        passes, so the first 30 rows can teach nothing about confidence. This
        includes every failure, spreads passes across confidence bands, and
        prefers distinct tools — see agentcheck/labeling.py.
        """
        key = shared.authorize(store, authorization)
        plan = labeling.select_batch(store.results(key, limit=5000), size=size)
        rows = {r["id"]: r for r in store.results(key, limit=5000)}
        plan["items"] = [
            {"id": i,
             "tool": rows[i].get("tool"),
             "request": rows[i].get("request"),
             # Same two-shape trap as labeling._verdict: public rows say
             # trace_verdict, store rows say verdict.
             "verdict": rows[i].get("trace_verdict") or rows[i].get("verdict"),
             "confidence": rows[i].get("confidence"),
             "checkset": rows[i].get("checkset")}
            for i in plan["ids"] if i in rows
        ]
        return plan
