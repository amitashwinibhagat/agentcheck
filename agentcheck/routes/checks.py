"""Judging pipeline: single checks, batches, and ad-hoc judge questions."""

import asyncio
import time
import uuid
from typing import Any

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

from agentcheck import policies
from agentcheck import screens
from agentcheck import tracing
from agentcheck.judges import get_judge
from agentcheck.reliability import annotate
from agentcheck.routes import shared
from agentcheck.stream import result_payload


class AskRequest(BaseModel):
    state: Any
    checkset: str = "safety"
    judge: str | None = None
    questions: list[dict] | None = None


class TraceCheckRequest(BaseModel):
    trace: dict
    checkset: str = "safety"
    judge: str | None = None
    policy: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None


class BatchCheckRequest(BaseModel):
    traces: list[Any]
    checkset: str = "safety"
    judge: str | None = None
    policy: str | None = None
    trace_id: str | None = None


MAX_BATCH = 100


def _new_trace_id() -> str:
    return "tr_" + uuid.uuid4().hex[:16]


def _new_span_id() -> str:
    return "sp_" + uuid.uuid4().hex[:12]


def normalize_trace(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise HTTPException(422, "each trace must be an object")
    t = raw["trace"] if isinstance(raw.get("trace"), dict) else raw
    request = str(t.get("request") or "").strip()
    tool = str(t.get("tool") or "").strip()
    if not request:
        raise HTTPException(422, "each row needs request")
    if not tool:
        raise HTTPException(422, "each row needs tool")
    args = t.get("args") if isinstance(t.get("args"), dict) else {}
    extra = {k: v for k, v in t.items() if k not in ("request", "tool", "args", "trace")}
    return {"request": request, "tool": tool, "args": args, **extra}


async def _run(key, state, questions, judge_name, guard, cache, store) -> dict:
    if not guard.allow(key, len(questions)):
        raise HTTPException(429, "rate limit exceeded; questions-per-minute cap reached")
    meta = store.key_meta(key) or {}
    allowance = meta.get("monthly_allowance", 500)
    if store.used_this_month(key) + len(questions) > allowance:
        store.record_event(key, "quota_collision",
                           {"used": store.used_this_month(key),
                            "allowance": allowance,
                            "requested": len(questions)})
        raise HTTPException(402, shared.collision_message(store, key))

    # Resolve the judge BEFORE the cache: cache entries are keyed by judge,
    # and an unresolved name (None) would key writes by "stub" while reads
    # looked up None — never hitting, and labelling hits from a default.
    judge = get_judge(judge_name)
    hit = cache.get(state, questions, judge.name)
    if hit is not None:
        cached_answers, cached_model = hit
        # The judge that actually produced these answers is part of the key,
        # so this is a fact about the row, not a guess. It used to read
        # `judge_name or "typesafe"`, which credited TypeSafe with stub work
        # whenever the caller passed no explicit judge.
        store.record(user_key=key, judge=judge.name, model=cached_model,
                     request_id=None, input_tokens=0, output_tokens=0,
                     questions=len(questions), server_ms=None, cached=1, ok=1)
        return {"answers": [_answer_dict(a) for a in cached_answers],
                "usage": {"cached": True}, "model": cached_model, "cached": True}

    loop = asyncio.get_running_loop()
    try:
        j = await loop.run_in_executor(None, judge.ask, state, questions)
    except Exception as e:
        store.record(user_key=key, judge=getattr(judge, "name", judge_name or "unknown"),
                     model="error", request_id=None,
                     input_tokens=0, output_tokens=0, questions=len(questions),
                     server_ms=None, cached=0, ok=0)
        raise HTTPException(502, "Check unavailable. The judge did not complete. This is not a pass.") from e

    store.record(user_key=key, judge=judge.name, model=j.model, request_id=j.request_id,
                 input_tokens=j.input_tokens, output_tokens=j.output_tokens,
                 questions=len(questions), server_ms=j.server_ms, cached=0, ok=1)

    cache.put(state, questions, j.answers, j.model, judge.name)
    return {
        "answers": [_answer_dict(a) for a in j.answers],
        "usage": {"input_tokens": j.input_tokens, "output_tokens": j.output_tokens,
                  "request_id": j.request_id, "server_ms": j.server_ms},
        "model": j.model,
        "cached": False,
    }


def _answer_dict(a) -> dict:
    if isinstance(a, dict):
        return a
    def fin(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return v
        return v if (v == v and abs(v) != float("inf")) else 0.0
    return {
        "question_id": a.question_id,
        "type": a.type,
        "value": fin(a.value) if isinstance(a.value, (int, float)) else a.value,
        "confidence": fin(a.confidence),
        "probabilities": {k: fin(v) for k, v in (a.probabilities or {}).items()},
    }


def register(app, store, guard, cache, bus, reliability, default_judge):
    async def _score_inner(key: str, trace: dict, checkset: str, judge_name: str | None,
                     run_id: str | None = None, policy_name: str | None = None,
                     trace_id: str | None = None, span_id: str | None = None,
                     parent_span_id: str | None = None) -> dict:
        trace_id = trace.pop("trace_id", None) or trace_id or _new_trace_id()
        span_id = trace.pop("span_id", None) or span_id or _new_span_id()
        parent_span_id = (trace.pop("parent_span_id", None)
                          or parent_span_id)
        t0 = time.perf_counter()
        qs = list(shared.get_checkset(checkset).checks)
        result = await _run(key, trace, qs, judge_name or default_judge, guard, cache, store)
        t_judge = time.perf_counter() - t0
        ans = {a["question_id"]: a for a in result["answers"]}
        payload = {
            "trace_verdict": ans.get("verdict", {}).get("value"),
            "confidence": ans.get("verdict", {}).get("confidence"),
            "severity": ans.get("severity", {}).get("value"),
            "checks": ans,
            "usage": result["usage"],
            "model": result.get("model"),
            "cached": result.get("cached", False),
        }
        # Deterministic screens under the judge: an unnamed outbound
        # destination floors pass to review before policy sees it.
        screens.apply(payload, trace)
        # Decision policy (observe-and-recommend). The verdict is the machine's
        # judgment; the decision is the policy's ruling. Never actioned here.
        if policy_name:
            try:
                pol_spec = policies.find_policy(policy_name)
                pol = policies.Policy(pol_spec)
                decision = pol.decide(payload["trace_verdict"],
                                      payload["confidence"], payload["severity"])
            except policies.PolicyError as e:
                raise HTTPException(422, f"policy {policy_name!r}: {e}") from None
            payload["decision"] = decision
            payload["policy"] = policy_name
            store.record_event(key, "policy_decision",
                               {"policy": policy_name, "decision": decision,
                                "verdict": payload["trace_verdict"],
                                "confidence": payload["confidence"]})
        saved = store.save_result(key, trace, payload, run_id=run_id,
                                  checkset=checkset, trace_id=trace_id,
                                  span_id=span_id,
                                  parent_span_id=parent_span_id)
        # live stream: push the slim event to this key's subscribers
        bus.publish(key, result_payload({**payload, **saved}))
        payload["id"] = saved["id"]
        payload["ts"] = saved["ts"]
        payload["trace_id"] = saved.get("trace_id")
        payload["span_id"] = saved.get("span_id")
        payload["parent_span_id"] = saved.get("parent_span_id")
        payload["assessment"] = saved.get("assessment")
        payload["run_id"] = saved.get("run_id")
        payload["checkset"] = saved.get("checkset")
        payload["duplicate"] = saved.get("duplicate", False)
        payload["request"] = trace.get("request")
        payload["tool"] = trace.get("tool")
        payload["args"] = trace.get("args") or {}
        payload["trace"] = trace
        # Correct the confidence against measured reality, when we have a
        # calibration report. Without one this is a no-op passthrough.
        annotate(reliability, payload)
        # latency budget, measured: judge time vs everything after it
        payload["timing_ms"] = {
            "judge": round(t_judge * 1000, 1),
            "total": round((time.perf_counter() - t0) * 1000, 1),
        }
        return payload

    async def _score(key: str, trace: dict, checkset: str, judge_name: str | None,
                     run_id: str | None = None, policy_name: str | None = None,
                     trace_id: str | None = None, span_id: str | None = None,
                     parent_span_id: str | None = None,
                     traceparent: str | None = None) -> dict:
        """One judged check, wrapped in an OTel span when export is on.

        The span is SERVER-kind with the caller's traceparent as remote
        parent, so this shows up inside the user's trace, not beside it.
        Tracing is best-effort: off means start() returns None and the
        body below runs exactly as before.
        """
        span = tracing.start(
            "agentcheck.check", traceparent=traceparent,
            attrs={"agentcheck.tool": trace.get("tool"),
                   "agentcheck.checkset": checkset})
        try:
            payload = await _score_inner(
                key, trace, checkset, judge_name, run_id=run_id,
                policy_name=policy_name, trace_id=trace_id, span_id=span_id,
                parent_span_id=parent_span_id)
            tracing.set_attrs(span, tracing.result_attrs(
                payload, payload.get("trace_id"), payload.get("span_id")))
            return payload
        finally:
            tracing.end(span)

    @app.post("/v1/ask")
    async def ask(req: AskRequest, authorization: str | None = Header(None)):
        key = shared.authorize(store, authorization)
        if req.questions:
            from agentcheck.judges.base import Question
            qs = [Question(**q) for q in req.questions]
        else:
            qs = list(shared.get_checkset(req.checkset).checks)
        return await _run(key, req.state, qs, req.judge or default_judge, guard, cache, store)

    @app.post("/v1/check")
    async def check_trace(req: TraceCheckRequest, authorization: str | None = Header(None),
                          traceparent: str | None = Header(None)):
        key = shared.authorize(store, authorization)
        trace = normalize_trace(req.trace)
        return await _score(key, trace, req.checkset, req.judge,
                            policy_name=req.policy, trace_id=req.trace_id,
                            span_id=req.span_id,
                            parent_span_id=req.parent_span_id,
                            traceparent=traceparent)

    @app.post("/v1/check-batch")
    async def check_batch(req: BatchCheckRequest, authorization: str | None = Header(None),
                          traceparent: str | None = Header(None)):
        key = shared.authorize(store, authorization)
        if not req.traces:
            raise HTTPException(422, "traces must be a non-empty list")
        if len(req.traces) > MAX_BATCH:
            raise HTTPException(422, f"at most {MAX_BATCH} traces per upload")
        # Validate the check set once; a bad row is isolated below, not fatal.
        shared.get_checkset(req.checkset)
        # Price the whole batch before running any of it. Never half-run an upload.
        batch_cost = len(req.traces) * len(list(shared.get_checkset(req.checkset).checks))
        meta = store.key_meta(key) or {}
        allowance = meta.get("monthly_allowance", 500)
        if store.used_this_month(key) + batch_cost > allowance:
            store.record_event(key, "quota_collision",
                               {"used": store.used_this_month(key),
                                "allowance": allowance, "requested": batch_cost})
            raise HTTPException(402, shared.collision_message(store, key))
        run_id = "run_" + uuid.uuid4().hex[:12]
        # NOT one trace for the whole upload. A batch is independent calls: the
        # run_id groups the upload, while a trace_id identifies a *sequence of
        # steps*. Sharing one trace across every row made 40 unrelated calls
        # read as a single 40-step run in the Runs view. If the caller passes a
        # top-level trace_id they are saying "these rows are one trace", so it
        # is still honoured — and a row carrying its own trace_id has always
        # grouped correctly (traces are grouped by the id they carry).
        batch_trace_id = req.trace_id
        sem = asyncio.Semaphore(4)

        async def one(i, raw):
            async with sem:
                row: dict = {"index": i}
                try:
                    trace = normalize_trace(raw)
                except HTTPException as e:
                    return {**row, "error": e.detail, "trace_verdict": None,
                            "request": None, "tool": None}
                try:
                    scored = await _score(key, trace, req.checkset, req.judge,
                                          run_id=run_id, policy_name=req.policy,
                                          trace_id=batch_trace_id,
                                          traceparent=traceparent)
                    scored["index"] = i
                    return scored
                except HTTPException as e:
                    return {**row, "error": e.detail, "trace_verdict": None,
                            "request": trace.get("request"), "tool": trace.get("tool")}
                except Exception as e:  # a judge failure must not become a pass
                    return {**row, "error": f"judge failed: {e}", "trace_verdict": None,
                            "request": trace.get("request"), "tool": trace.get("tool")}

        out = await asyncio.gather(*[one(i, raw) for i, raw in enumerate(req.traces)])
        counts = {"pass": 0, "review": 0, "fail": 0, "error": 0}
        for row in out:
            v = row.get("trace_verdict") or "error"
            counts[v] = counts.get(v, 0) + 1
        store.record_event(key, "batch_upload",
                           {"n": len(out), "run_id": run_id, **counts})
        return {"results": list(out), "counts": counts, "n": len(out),
                "run_id": run_id, "trace_id": batch_trace_id}
