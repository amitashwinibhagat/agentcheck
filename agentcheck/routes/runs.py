"""Agent runs: the steps of one run, newest runs first."""

from fastapi import Header, HTTPException

from agentcheck.routes import shared


def register(app, store):
    @app.get("/v1/runs")
    async def list_runs(authorization: str | None = Header(None),
                       limit: int = 50, only: str | None = None,
                       tool: str | None = None):
        """Agent runs, newest first, with a per-run summary.

        A run is a group of steps sharing a trace_id. `only` narrows to runs
        containing a block or a review; `tool` to runs that used a tool.
        """
        key = shared.authorize(store, authorization)
        if only not in (None, "", "blocked", "review"):
            raise HTTPException(422, "only must be 'blocked' or 'review'")
        runs = store.runs(key, limit=max(1, min(int(limit), 500)),
                          only=only or None, tool=tool or None)
        return {"runs": runs, "n": len(runs),
                "blocked": sum(1 for r in runs if r["blocked"])}

    @app.get("/v1/traces/{trace_id}")
    async def trace_view(trace_id: str,
                         authorization: str | None = Header(None),
                         limit: int = 500):
        """One agent run, oldest step first. 404 when the id is unknown."""
        key = shared.authorize(store, authorization)
        steps = store.trace_steps(key, trace_id, limit=limit)
        if not steps:
            raise HTTPException(404, f"unknown trace {trace_id!r}")
        tools = [s.get("tool") for s in steps]
        verdicts = [s.get("trace_verdict") for s in steps]
        return {
            "trace_id": trace_id,
            "n": len(steps),
            "tools": tools,
            "verdicts": verdicts,
            "blocked": any(v == "fail" for v in verdicts if v),
            "steps": steps,
        }
