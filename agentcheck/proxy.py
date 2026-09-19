"""The metered proxy.

Flow:  auth -> quota guard (BEFORE forwarding) -> cache -> judge router -> meter
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from agentcheck import calibration as cal
from agentcheck import checks as check_lib
from agentcheck import monitor as mon
from agentcheck import trust
from agentcheck import auth
from agentcheck import billing
from agentcheck import policies
from agentcheck import workspaces
from agentcheck import screens
from agentcheck import tracing
from agentcheck import routes
from agentcheck.routes.checks import AskRequest  # annotation for rag_metrics until it moves
from agentcheck.stream import Bus, result_payload, sse_format
from agentcheck.reliability import ReliabilityModel, annotate, load_report


from agentcheck import redteam
from agentcheck.evals import datasets as ds
from agentcheck.judges import get_judge
from agentcheck.store import Store
from agentcheck.limits import AnswerCache, QuotaGuard, WINDOW_SECONDS
from agentcheck.web import mount_web


def get_checkset(name: str):
    """Unknown check set is a client error, not a 500."""
    try:
        return check_lib.get(name)
    except KeyError:
        raise HTTPException(422, f"unknown check set {name!r}; have {check_lib.all_names()}")


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    qpm: int = Field(default=600, ge=1, le=10_000)
    allowance: int = Field(default=500, ge=0, le=10_000_000)
    plan: str = "free"
    trial_days: int | None = Field(default=None, ge=1, le=365)


def create_app(store: Store, default_judge: str = "typesafe",
               cache: "AnswerCache | None" = None,
               bus: "Bus | None" = None,
               demo_mode: bool | None = None,
               auth_provider=None) -> FastAPI:
    app = FastAPI(title="agentcheck", version="0.1.0")
    # Identity provider: injected in tests, resolved from AGENTCHECK_AUTH in
    # production. NullAuthProvider refuses clearly when unconfigured.
    idp = auth_provider if auth_provider is not None else auth.get_provider()
    # Resolve the judge ONCE, at startup. Building it lazily meant a missing
    # API key surfaced as a 500 on the first check (and killed the request)
    # instead of a startup warning — the opposite of what the Dockerfile and
    # .env.example promise. Falling back to the offline stub keeps a
    # key-less container useful, loudly.
    try:
        get_judge(default_judge)
    except Exception as e:
        print(f"[agentcheck] judge {default_judge!r} unavailable ({e}); "
              f"falling back to the offline stub judge", file=sys.stderr)
        default_judge = "stub"
    app.state.judge = default_judge
    guard = QuotaGuard(store, WINDOW_SECONDS)
    cache = cache or AnswerCache()
    bus = bus or Bus()
    # Per-decision reliability, if a calibration report has been loaded for
    # this workspace. Without one, lookups fall back to raw confidence.
    reliability = ReliabilityModel(load_report(store))
    # OTel export, if AGENTCHECK_OTLP_ENDPOINT is set and the SDK is
    # installed. Never raises; without it checks run exactly as before.
    try:
        tracing.configure()
    except Exception:
        pass
    # Demo mode: on a hosted URL the browser cannot auto-login (localhost
    # only). AGENTCHECK_DEMO=1 lets /v1/bootstrap hand out a dedicated,
    # tightly-capped demo key from anywhere. It is NOT the admin key.
    if demo_mode is None:
        demo_mode = os.environ.get("AGENTCHECK_DEMO", "") == "1"
    if demo_mode:
        # Capped, but sized for a real demo: seeding a log plus a FULL
        # red-team run (467 attacks x 6 checks = ~2,800 questions) must fit
        # in one burst. The rate limit is blast protection; the monthly
        # allowance bounds total abuse across many visitors.
        demo_key = store.ensure_named_key("demo", qpm_limit=4000,
                                          monthly_allowance=40000)
    else:
        demo_key = None

    ui_key = store.ensure_named_key("browser", qpm_limit=600)

    def _authorize(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        token = authorization.removeprefix("Bearer ").strip()
        row = store.lookup_key(token)
        if row is None:
            raise HTTPException(401, "unknown api key")
        return row["kid"]

    def _local(request: Request) -> bool:
        host = request.client.host if request.client else ""
        return host in ("127.0.0.1", "::1", "testclient")

    @app.get("/v1/bootstrap")
    def bootstrap(request: Request):
        # Hosted demo: hand out the capped demo key from anywhere. Local:
        # hand out the full browser key, localhost only, as before.
        if demo_mode:
            return {"key": demo_key, "demo": True}
        if not _local(request):
            raise HTTPException(403, "browser auto-login is only for localhost")
        return {"key": ui_key}

    routes.checks.register(app, store, guard, cache, bus, reliability, default_judge)

    # ── rubric catalogue ──────────────────────────────────────────────
    routes.checksets.register(app, store)

    # ── monitoring ────────────────────────────────────────────────────
    routes.monitor.register(app, store)

    # ── workspaces, seats, invites ────────────────────────────────────
    routes.workspace.register(app, store)

    # ── identity: login, sessions, membership binding ──────────────────
    routes.auth.register(app, store, idp)

    routes.workspaces.register(app, store)

    # ── billing ────────────────────────────────────────────
    routes.billing.register(app, store)

    # ── trust ─────────────────────────────────────────────────────────
    routes.trust.register(app, store)

    routes.stream.register(app, store, bus, demo_mode)

    # ── calibration ───────────────────────────────────────────────────

    @app.get("/v1/calibration")
    async def calibration_report(authorization: str | None = Header(None),
                                 dataset: str = "seed",
                                 checkset: str = "safety",
                                 gate: float = 0.6, bins: int = 10):
        _authorize(authorization)
        get_checkset(checkset)
        try:
            rows = ds.load(dataset)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        if not rows:
            raise HTTPException(422, f"dataset {dataset!r} is empty")
        loop = asyncio.get_running_loop()
        try:
            rep = await loop.run_in_executor(
                None, lambda: cal.calibration(default_judge, rows,
                                             checkset=checkset, gate=gate,
                                             bins=bins))
        except Exception as e:
            raise HTTPException(502, f"calibration failed: {e}")
        rep["dataset"] = dataset
        rep["read"] = cal.verdict_for_ece(rep["decided"]["ece"],
                                         rep["decided"]["n"])
        return rep

    # ── red team ──────────────────────────────────────────────────────

    @app.post("/v1/redteam")
    async def redteam_run(authorization: str | None = Header(None),
                          checkset: str = "safety",
                          families: str | None = None,
                          judge: str | None = None):
        key = _authorize(authorization)
        cset = get_checkset(checkset)
        fams = [f.strip() for f in families.split(",") if f.strip()] if families else None
        try:
            attacks = redteam.corpus(fams)
        except KeyError as e:
            raise HTTPException(422, str(e))
        # Price the run before doing any of it, like a batch upload.
        cost = len(attacks) * len(cset.checks)
        if not guard.allow(key, cost):
            raise HTTPException(429, "rate limit exceeded; questions-per-minute cap")
        meta = store.key_meta(key) or {}
        allowance = meta.get("monthly_allowance", 500)
        if store.used_this_month(key) + cost > allowance:
            raise HTTPException(402, _collision_message(store, key))

        j = get_judge(judge or default_judge)

        def run():
            rows = []
            for a in attacks:
                judgment = j.ask(a.trace(), list(cset.checks))
                answers = {x.question_id: x for x in judgment.answers}
                v = answers.get(cset.verdict)
                probe = {"trace_verdict": str(v.value) if v else None}
                screens.apply(probe, a.trace())
                verdict = probe["trace_verdict"]
                rows.append({
                    "id": a.id, "family": a.family,
                    "verdict": verdict,
                    "confidence": float(v.confidence) if v and v.confidence else None,
                    "note": a.note, "trace": a.trace(),
                    "screen": probe.get("screen"),
                    "evaded": verdict == "pass",
                })
            return rows

        loop = asyncio.get_running_loop()
        try:
            rows = await loop.run_in_executor(None, run)
        except Exception as e:
            raise HTTPException(502, f"red team run failed: {e}")
        store.record(user_key=key, judge=j.name,
                     model=getattr(j, "_model", None) or j.name,
                     request_id=None, input_tokens=0, output_tokens=0,
                     questions=cost, server_ms=None, cached=0, ok=1)
        by_family: dict[str, dict] = {}
        for r in rows:
            s = by_family.setdefault(r["family"], {"n": 0, "evaded": 0})
            s["n"] += 1
            s["evaded"] += 1 if r["evaded"] else 0
        evaded = [r for r in rows if r["evaded"]]
        asr = len(evaded) / len(rows) if rows else 0.0
        # Persist the probe so the trust score can see adversarial
        # robustness without re-running 100+ judge calls on every read.
        try:
            store.record_event(key, "redteam_run", {
                "judge": j.name,
                "checkset": cset.name,
                "families": sorted(fams) if fams else None,
                "n": len(rows),
                "evaded": len(evaded),
                "asr": asr,
                "high_conf_evaded": sum(
                    1 for r in evaded
                    if isinstance(r.get("confidence"), (int, float))
                    and r["confidence"] >= 0.5),
            })
        except Exception:
            pass
        return {
            "judge": j.name,
            "checkset": cset.name,
            "n": len(rows),
            "asr": asr,
            "by_family": by_family,
            "evaded": evaded,
            "attacks": rows,
        }

    @app.get("/v1/runs")
    async def list_runs(authorization: str | None = Header(None),
                       limit: int = 50, only: str | None = None,
                       tool: str | None = None):
        """Agent runs, newest first, with a per-run summary.

        A run is a group of steps sharing a trace_id. `only` narrows to runs
        containing a block or a review; `tool` to runs that used a tool.
        """
        key = _authorize(authorization)
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
        key = _authorize(authorization)
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

    @app.get("/v1/redteam/families")
    def redteam_families():
        """Every family with its size and kind, so a UI can price a run
        before it starts instead of hardcoding a count that goes stale."""
        counts: dict[str, int] = {}
        for a in redteam.corpus():
            counts[a.family] = counts.get(a.family, 0) + 1
        industries = set(redteam.industry_names())
        return {
            "families": redteam.family_names(),
            "counts": counts,
            "industries": sorted(industries),
            "mechanics": sorted(set(redteam.family_names()) - industries),
            "total": sum(counts.values()),
        }

    # ── eval reports on disk ──────────────────────────────────────────

    @app.get("/v1/evals")
    def list_evals(authorization: str | None = Header(None)):
        """Eval reports found under ./evals and $AGENTCHECK_HOME/evals."""
        _authorize(authorization)
        import os as _os
        from pathlib import Path as _P
        seen = []
        for d in (_P.cwd() / "evals", _P(_os.environ.get("AGENTCHECK_HOME")
                                         or (_P.home() / ".agentcheck")) / "evals"):
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.json")):
                try:
                    rep = json.loads(f.read_text())
                except Exception:
                    continue
                if rep.get("kind") != "agentcheck.eval-report":
                    continue
                seen.append({"path": str(f), "name": rep.get("name"),
                             "ran_at": rep.get("ran_at"), "split": rep.get("split"),
                             "cells": len(rep.get("cells", [])),
                             "report": rep})
        return {"reports": seen}

    @app.get("/v1/datasets")
    def list_datasets(authorization: str | None = Header(None)):
        _authorize(authorization)
        return {"datasets": ds.inventory(), "root": str(ds.root())}

    # ── RAG metrics ───────────────────────────────────────────────────

    @app.post("/v1/metrics/rag")
    async def rag_metrics(req: AskRequest, authorization: str | None = Header(None)):
        key = _authorize(authorization)
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
            raise HTTPException(402, _collision_message(store, key))
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

    @app.get("/v1/usage")
    async def usage(authorization: str | None = Header(None)):
        key = _authorize(authorization)
        meta = store.key_meta(key) or {}
        allowance = meta.get("monthly_allowance", 500)
        used_month = store.used_this_month(key)
        _, resets_at = Store._month_start()
        return {
            "window_seconds": WINDOW_SECONDS,
            "used_in_window": guard.used(key),
            "qpm_limit": meta.get("qpm_limit"),
            "key_name": meta.get("name"),
            "plan": meta.get("plan", "free"),
            "trial_active": meta.get("trial_active", False),
            "trial_ends_at": meta.get("trial_ends_at"),
            "allowance": {
                "monthly_allowance": allowance,
                "used_this_month": used_month,
                "remaining": max(allowance - used_month, 0),
                "resets_at": resets_at,
            },
            "totals": store.totals(key),
        }

    @app.post("/v1/keys")
    async def create_key(req: CreateKeyRequest, authorization: str | None = Header(None)):
        # Local workspace: any valid key can mint a sibling key for the same store.
        _authorize(authorization)
        plan = req.plan if req.plan in ("free", "pro", "trial") else "free"
        minted = store.create_key(req.name, qpm_limit=req.qpm,
                                  monthly_allowance=req.allowance, plan=plan,
                                  trial_days=req.trial_days)
        return {"key": minted, "name": req.name, "qpm_limit": req.qpm,
                "monthly_allowance": req.allowance, "plan": plan}

    mount_web(app)
    routes.results.register(app, store)
    return app


def _collision_message(store: Store, key: str) -> str:
    """The designed paywall moment: what they did, what unlocks, one path.

    Never a dead end, never guilt copy. Names the value first."""
    used = store.used_this_month(key)
    meta = store.key_meta(key) or {}
    allowance = meta.get("monthly_allowance", 500)
    caught = store.fail_count_this_month(key)
    return (
        f"Free allowance exhausted ({used}/{allowance} questions used this month). "
        f"You caught {caught} risky call{'s' if caught != 1 else ''} so far. "
        "Teams continues with a higher allowance — upgrade to keep checking."
    )


