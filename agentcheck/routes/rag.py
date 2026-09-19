"""RAG metrics over question/answer/contexts traces."""

import asyncio

from fastapi import Header, HTTPException

from agentcheck.judges import get_judge
from agentcheck.routes import shared
from agentcheck.routes.checks import AskRequest


def register(app, store, guard, default_judge):
    @app.post("/v1/metrics/rag")
    async def rag_metrics(req: AskRequest, authorization: str | None = Header(None)):
        key = shared.authorize(store, authorization)
        from agentcheck import metrics as M
        trace = req.state
        if not isinstance(trace, dict) or not M.is_rag_trace(trace):
            raise HTTPException(
                422, "not a RAG trace: needs question, answer and contexts")
        judge_name = req.judge or default_judge
        # Estimated questions: the main metric block plus one per context.
        nroots = len(M.build_questions(trace, include_ground_truth=bool(trace.get("ground_truth"))))
        nctx = len(trace.get("contexts") or [])
        cost = nroots + nctx
        if not guard.allow(key, cost):
            raise HTTPException(429, "rate limit exceeded; questions-per-minute cap")
        meta = store.key_meta(key) or {}
        allowance = meta.get("monthly_allowance", 500)
        if store.used_this_month(key) + cost > allowance:
            raise HTTPException(402, shared.collision_message(store, key))
        judge = get_judge(judge_name)
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: M.score_trace(judge, trace))
        except Exception as e:
            raise HTTPException(502, f"metrics failed: {e}")
        store.record(user_key=key, judge=judge.name,
                     model=getattr(judge, "_model", None) or judge.name,
                     request_id=None, input_tokens=0, output_tokens=0,
                     questions=cost, server_ms=None, cached=0, ok=1)
        return {
            "judge": judge.name,
            "metrics": {k: m.__dict__ for k, m in result.items()},
            "usage": {"questions": cost},
        }
