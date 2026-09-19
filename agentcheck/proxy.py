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
from agentcheck.stream import Bus, result_payload, sse_format
from agentcheck.reliability import ReliabilityModel, annotate


def _load_reliability(store: Store) -> dict | None:
    """The workspace's latest saved calibration report, if any.

    `calibrate --publish` writes it to $AGENTCHECK_HOME (default
    ~/.agentcheck); a copy next to the DB also works, for workspaces that
    keep everything in one directory. None means uncalibrated, and the
    model says so honestly.
    """
    from pathlib import Path
    candidates = []
    home = os.environ.get("AGENTCHECK_HOME")
    if home:
        candidates.append(Path(home) / "calibration.json")
    else:
        try:
            candidates.append(Path.home() / ".agentcheck" / "calibration.json")
        except Exception:
            pass
    try:
        # Postgres-backed stores have no local directory; the
        # AGENTCHECK_HOME sidecar above is the source of truth there.
        if getattr(store, "path", None) is not None:
            candidates.append(store.path.parent / "calibration.json")
    except Exception:
        pass
    for p in candidates:
        try:
            if p.exists():
                return json.loads(p.read_text())
        except Exception:
            continue
    return None


def _calibration_for(store: Store, checkset: str | None) -> dict | None:
    """The published calibration report, in the shape trust_score wants.

    This is the bridge the measured tier never had: `calibrate --publish`
    writes calibration.json for the reliability model, but the trust endpoint
    read nothing, so the tier could never leave `consistency` however many
    labels were supplied. Scoped by rubric — a calibration for `safety` must
    not upgrade the tier for `refund-policy`.
    """
    report = trust.dataset_report_from_calibration(_load_reliability(store))
    if report and checkset and report.get("checkset") and \
            report["checkset"] != checkset:
        return None
    return report
from agentcheck import redteam
from agentcheck.evals import datasets as ds
from agentcheck.judges import get_judge
from agentcheck.store import Store
from agentcheck.limits import AnswerCache, QuotaGuard
from agentcheck.web import mount_web


def _trust_for(store: Store, key: str, checkset: str | None,
               gate: float) -> dict:
    """The trust payload both the JSON endpoint and the SVG badge serve.

    One function so the badge can never drift from the number: same rows,
    same rubric scope, same adversarial probe, same calibration bridge.
    """
    rows = store.results(key, limit=5000)
    if checkset:
        rows = [r for r in rows if r.get("checkset") == checkset]
    try:
        adv = store.latest_event(key, "redteam_run")
    except Exception:
        adv = None
    return trust.trust_score(rows, gate=gate, adversarial=adv,
                             dataset_report=_calibration_for(store, checkset))


def yaml_dirs():
    """Where rubrics are looked for, for display in the UI."""
    from agentcheck.checks.yaml_checksets import search_dirs
    return search_dirs()

WINDOW_SECONDS = 60.0


class AskRequest(BaseModel):
    state: Any
    checkset: str = "safety"
    judge: str | None = None
    questions: list[dict] | None = None


def _new_trace_id() -> str:
    return "tr_" + uuid.uuid4().hex[:16]


def _new_span_id() -> str:
    return "sp_" + uuid.uuid4().hex[:12]


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


def get_checkset(name: str):
    """Unknown check set is a client error, not a 500."""
    try:
        return check_lib.get(name)
    except KeyError:
        raise HTTPException(422, f"unknown check set {name!r}; have {check_lib.all_names()}")


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
    reliability = ReliabilityModel(_load_reliability(store))
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

    @app.post("/v1/ask")
    async def ask(req: AskRequest, authorization: str | None = Header(None)):
        key = _authorize(authorization)
        if req.questions:
            from agentcheck.judges.base import Question
            qs = [Question(**q) for q in req.questions]
        else:
            qs = list(get_checkset(req.checkset).checks)
        return await _run(key, req.state, qs, req.judge or default_judge, guard, cache, store)

    async def _score_inner(key: str, trace: dict, checkset: str, judge_name: str | None,
                     run_id: str | None = None, policy_name: str | None = None,
                     trace_id: str | None = None, span_id: str | None = None,
                     parent_span_id: str | None = None) -> dict:
        trace_id = trace.pop("trace_id", None) or trace_id or _new_trace_id()
        span_id = trace.pop("span_id", None) or span_id or _new_span_id()
        parent_span_id = (trace.pop("parent_span_id", None)
                          or parent_span_id)
        t0 = time.perf_counter()
        qs = list(get_checkset(checkset).checks)
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

    @app.post("/v1/check")
    async def check_trace(req: TraceCheckRequest, authorization: str | None = Header(None),
                          traceparent: str | None = Header(None)):
        key = _authorize(authorization)
        trace = normalize_trace(req.trace)
        return await _score(key, trace, req.checkset, req.judge,
                            policy_name=req.policy, trace_id=req.trace_id,
                            span_id=req.span_id,
                            parent_span_id=req.parent_span_id,
                            traceparent=traceparent)

    @app.post("/v1/check-batch")
    async def check_batch(req: BatchCheckRequest, authorization: str | None = Header(None),
                          traceparent: str | None = Header(None)):
        key = _authorize(authorization)
        if not req.traces:
            raise HTTPException(422, "traces must be a non-empty list")
        if len(req.traces) > MAX_BATCH:
            raise HTTPException(422, f"at most {MAX_BATCH} traces per upload")
        # Validate the check set once; a bad row is isolated below, not fatal.
        get_checkset(req.checkset)
        # Price the whole batch before running any of it. Never half-run an upload.
        batch_cost = len(req.traces) * len(list(get_checkset(req.checkset).checks))
        meta = store.key_meta(key) or {}
        allowance = meta.get("monthly_allowance", 500)
        if store.used_this_month(key) + batch_cost > allowance:
            store.record_event(key, "quota_collision",
                               {"used": store.used_this_month(key),
                                "allowance": allowance, "requested": batch_cost})
            raise HTTPException(402, _collision_message(store, key))
        run_id = "run_" + uuid.uuid4().hex[:12]
        batch_trace_id = req.trace_id or _new_trace_id()
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

    # ── rubric catalogue ──────────────────────────────────────────────
    routes.checksets.register(app, store)

    # ── monitoring ────────────────────────────────────────────────────
    routes.monitor.register(app, store)

    # ── workspaces, seats, invites ────────────────────────────────────

    def _acting(authorization: str | None) -> tuple[str, dict, str]:
        """(kid, workspace, role) for the caller.

        A request acts on the workspace its key belongs to. During Layer 1 the
        caller IS the key's owner, so the role comes from that member row —
        the check is real, and Layer 2 just supplies a different user.
        """
        key = _authorize(authorization)
        ws = store.workspace_for_key(key)
        if ws is None:
            raise HTTPException(500, "key has no workspace")
        role = "member"
        for m in ws.get("members") or []:
            if m.get("user_id") == key:
                role = m.get("role") or "member"
                break
        return key, ws, role

    def _need(role: str, action: str) -> None:
        try:
            workspaces.require(role, action)
        except workspaces.WorkspaceError as e:
            raise HTTPException(403, str(e)) from None

    @app.get("/v1/workspace")
    async def workspace_detail(authorization: str | None = Header(None)):
        """The caller's workspace: members, seats, keys, plan."""
        _, ws, role = _acting(authorization)
        members = ws.get("members") or []
        pending = store.invites(ws["id"], pending_only=True)
        return {**ws, "role": role,
                "seats": {"used": workspaces.seats_used(members),
                          "limit": workspaces.seat_limit(ws["plan"]),
                          "pending_invites": len(pending)},
                "plan_seats": workspaces.seat_limit(ws["plan"]),
                "invites": pending}

    @app.post("/v1/workspace/invites")
    async def create_invite(payload: dict,
                            authorization: str | None = Header(None)):
        """Invite someone. The seat is reserved at invite time."""
        key, ws, role = _acting(authorization)
        _need(role, "manage_invites")
        email = str(payload.get("email") or "").strip().lower()
        if "@" not in email:
            raise HTTPException(422, "a valid email is required")
        invite_role = str(payload.get("role") or "member")
        try:
            invite = workspaces.new_invite(email, invite_role)
            members = store.workspace_members(ws["id"])
            workspaces.check_seat_available(
                ws["seat_limit"], members,
                [i["email"] for i in store.invites(ws["id"],
                                                   pending_only=True)],
                plan=ws["plan"])
        except workspaces.WorkspaceError as e:
            # 402 for a seat limit (a billing problem), 422 for a bad role
            code = 402 if "seat" in str(e) else 422
            raise HTTPException(code, str(e)) from None
        if any((m.get("email") or "").lower() == email for m in members):
            raise HTTPException(409, f"{email} is already a member")
        rec = store.create_invite(ws["id"], invite)
        store.record_event(key, "member_invited",
                           {"workspace": ws["id"], "email": email,
                            "role": invite_role})
        # the raw token is returned exactly once, like an API key
        return {"id": rec["id"], "email": email, "role": invite_role,
                "expires_at": rec["expires_at"], "token": invite["token"]}

    @app.get("/v1/workspace/invites")
    async def list_invites(authorization: str | None = Header(None)):
        _, ws, role = _acting(authorization)
        _need(role, "view")
        return {"invites": store.invites(ws["id"])}

    @app.delete("/v1/workspace/invites/{invite_id}")
    async def revoke_invite(invite_id: str,
                            authorization: str | None = Header(None)):
        _, ws, role = _acting(authorization)
        _need(role, "manage_invites")
        ok = store.revoke_invite(ws["id"], invite_id)
        if not ok:
            raise HTTPException(404, "no such pending invite")
        return {"revoked": True}

    @app.patch("/v1/workspace/members/{email}")
    async def change_role(email: str, payload: dict,
                          authorization: str | None = Header(None)):
        key, ws, role = _acting(authorization)
        _need(role, "manage_members")
        new_role = str(payload.get("role") or "")
        if new_role not in workspaces.ROLES:
            raise HTTPException(422, f"role must be one of {list(workspaces.ROLES)}")
        members = store.workspace_members(ws["id"])
        if store.member_role(ws["id"], email) is None:
            raise HTTPException(404, f"{email} is not a member")
        try:
            workspaces.check_owner_survives(members, email, new_role)
        except workspaces.WorkspaceError as e:
            raise HTTPException(409, str(e)) from None
        store.set_member_role(ws["id"], email, new_role)
        store.record_event(key, "member_role_changed",
                           {"workspace": ws["id"], "email": email,
                            "role": new_role})
        return {"email": email, "role": new_role}

    @app.delete("/v1/workspace/members/{email}")
    async def remove_member(email: str,
                            authorization: str | None = Header(None)):
        key, ws, role = _acting(authorization)
        _need(role, "manage_members")
        members = store.workspace_members(ws["id"])
        if store.member_role(ws["id"], email) is None:
            raise HTTPException(404, f"{email} is not a member")
        try:
            workspaces.check_owner_survives(members, email, None)
        except workspaces.WorkspaceError as e:
            raise HTTPException(409, str(e)) from None
        store.remove_member(ws["id"], email)
        store.record_event(key, "member_removed",
                           {"workspace": ws["id"], "email": email})
        return {"removed": True, "email": email}

    @app.post("/v1/invites/accept")
    async def accept_invite(payload: dict):
        """Redeem an invite token.

        Token-only, no key: the invitee has no account yet. Accepting
        MATERIALISES the membership — an invite that only marked itself
        accepted left the person invited but not a member.
        """
        token = str(payload.get("token") or "")
        if not token:
            raise HTTPException(422, "token is required")
        rec = store.invite_by_hash(workspaces.hash_token(token))
        if rec is None:
            raise HTTPException(404, "invite not found")
        ok, reason = workspaces.invite_usable(rec)
        if not ok:
            raise HTTPException(410, reason)
        if not store.materialize_invite(rec, payload.get("user_id")):
            raise HTTPException(
                409, "that workspace has no free seat; ask for an upgrade")
        return {"accepted": True, "workspace_id": rec["workspace_id"],
                "email": rec["email"], "role": rec["role"]}

    # ── identity: login, sessions, membership binding ──────────────────

    def _base_url(request: Request) -> str:
        """Public base URL for redirect URIs. Behind a proxy the request URL
        is the internal one, so an explicit setting wins."""
        base = (os.environ.get("AGENTCHECK_BASE_URL") or "").rstrip("/")
        if base:
            return base
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("host") or request.url.netloc
        return f"{scheme}://{host}"

    def _set_session_cookie(response: Response, request: Request, token: str,
                            max_age: int) -> None:
        secure = (_base_url(request).startswith("https")
                  or request.url.scheme == "https")
        response.set_cookie(auth.SESSION_COOKIE, token, max_age=max_age,
                            httponly=True, samesite="lax", secure=secure,
                            path="/")

    def _current_user(request: Request) -> dict | None:
        """The signed-in person, or None. Expired sessions are refused and
        cleared, so a stale cookie cannot act."""
        token = request.cookies.get(auth.SESSION_COOKIE)
        if not token:
            return None
        rec = store.session_by_token(token)
        ok, _reason = auth.session_usable(rec or {})
        if not ok:
            return None
        store.touch_session(token)
        return store.user(rec["user_id"])

    @app.get("/v1/auth/login")
    async def auth_login(request: Request, next: str = "/"):
        """Start a login: signed state in a cookie, then redirect out."""
        if not idp.configured():
            raise HTTPException(
                503, "login is not configured on this deployment")
        try:
            # the return path is signed INTO the state, so the callback never
            # trusts a client-supplied redirect target
            state = auth.sign_state(uuid.uuid4().hex, next_path=next)
            url = idp.authorize_url(
                f"{_base_url(request)}/v1/auth/callback", state)
        except auth.AuthError as e:
            raise HTTPException(503, str(e)) from None
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(auth.STATE_COOKIE, state, max_age=auth.STATE_TTL_SECONDS,
                        httponly=True, samesite="lax",
                        secure=_base_url(request).startswith("https"),
                        path="/")
        return resp

    @app.get("/v1/auth/callback")
    async def auth_callback(request: Request, code: str | None = None,
                            state: str | None = None, error: str | None = None):
        """Finish a login. Verifies state BEFORE exchanging the code."""
        if error:
            raise HTTPException(400, f"provider returned an error: {error}")
        if not code:
            raise HTTPException(400, "missing code")
        expected = request.cookies.get(auth.STATE_COOKIE)
        payload = auth.verify_state(state) if state else None
        if not payload or not expected or state != expected:
            raise HTTPException(400, "invalid or expired login state")
        try:
            profile = idp.exchange(code, f"{_base_url(request)}/v1/auth/callback")
        except auth.AuthError as e:
            raise HTTPException(502, str(e)) from None
        user = store.upsert_user(idp.name, profile["provider_user_id"],
                                 profile["email"], profile.get("name"))
        # Two ways a membership can be waiting: an unclaimed member row, or a
        # pending INVITE (which lives in another table and used to be dropped
        # here — an invited person would sign in and still not be a member).
        claimed = store.bind_memberships(profile["email"], user["id"])
        invited = store.claim_invites(profile["email"], user["id"])
        store.record_event(user["id"], "login",
                           {"provider": idp.name, "claimed": claimed,
                            "invites_claimed": len(invited)})
        session = auth.new_session(user["id"])
        store.create_session(session)
        resp = RedirectResponse(payload["next"], status_code=302)
        _set_session_cookie(resp, request, session["token"],
                            auth.SESSION_TTL_SECONDS)
        resp.delete_cookie(auth.STATE_COOKIE, path="/")
        return resp

    @app.get("/v1/auth/me")
    async def auth_me(request: Request):
        """Who am I, and which workspaces do I belong to."""
        user = _current_user(request)
        if user is None:
            return {"authenticated": False, "provider": idp.name}
        rows = store.workspaces_for_user(user["id"])
        return {"authenticated": True, "provider": idp.name,
                "user": {"id": user["id"], "email": user["email"],
                         "name": user["name"]},
                "workspaces": [{"id": w["id"], "name": w["name"],
                                "role": w.get("role"), "plan": w["plan"]}
                               for w in rows]}

    @app.post("/v1/auth/logout")
    async def auth_logout(request: Request):
        token = request.cookies.get(auth.SESSION_COOKIE)
        removed = store.delete_session(token) if token else False
        resp = Response(content='{"logged_out": true}',
                        media_type="application/json")
        resp.delete_cookie(auth.SESSION_COOKIE, path="/")
        return resp if removed else resp

    @app.get("/v1/workspaces")
    async def my_workspaces(request: Request):
        """Workspaces the signed-in person belongs to, with their role."""
        user = _current_user(request)
        if user is None:
            raise HTTPException(401, "not signed in")
        rows = store.workspaces_for_user(user["id"])
        return {"user": user["id"], "workspaces": [
            {"id": w["id"], "name": w["name"], "plan": w["plan"],
             "role": w.get("role"),
             "seats": {"used": workspaces.seats_used(
                 store.workspace_members(w["id"])),
                 "limit": w["seat_limit"]}}
            for w in rows]}

    @app.get("/v1/workspaces/{wid}")
    async def workspace_by_id(wid: str, request: Request,
                              authorization: str | None = Header(None)):
        """One workspace, for a member (session) or its own key."""
        ws = store.workspace(wid)
        if ws is None:
            raise HTTPException(404, "no such workspace")
        user = _current_user(request)
        role = None
        if user is not None:
            for m in ws["members"]:
                if m.get("user_id") == user["id"]:
                    role = m.get("role")
                    break
        if role is None and authorization:
            key = _authorize(authorization)
            own = store.workspace_for_key(key)
            if own is not None and own["id"] == wid:
                role = "owner" if any(
                    m.get("user_id") == key and m.get("role") == "owner"
                    for m in ws["members"]) else "admin"
        if role is None:
            raise HTTPException(403, "not a member of this workspace")
        pending = store.invites(wid, pending_only=True)
        return {**ws, "role": role,
                "seats": {"used": workspaces.seats_used(ws["members"]),
                          "limit": ws["seat_limit"],
                          "pending_invites": len(pending)},
                "invites": pending}

    # ── billing ────────────────────────────────────────────

    @app.get("/v1/billing/plans")
    def billing_plans():
        """The catalogue. Public: a pricing page needs it before signup."""
        p = billing.get_provider()
        return {"plans": billing.plans_public(), "provider": p.name,
                "configured": p.configured()}

    @app.post("/v1/billing/checkout")
    async def billing_checkout(payload: dict | None = None,
                               authorization: str | None = Header(None)):
        """Start a subscription for the CALLER's key.

        The plan is chosen here and written into the provider's notes, so the
        webhook that returns can be matched back to this key without any
        client-supplied identity.
        """
        key = _authorize(authorization)
        plan_name = str((payload or {}).get("plan") or "pro")
        try:
            billing.plan(plan_name)
        except billing.BillingError as e:
            raise HTTPException(422, str(e)) from None
        provider = billing.get_provider()
        if not provider.configured():
            raise HTTPException(
                503, "billing is not configured on this deployment")
        try:
            sub = provider.create_subscription(key, plan_name)
        except billing.BillingError as e:
            raise HTTPException(502, str(e)) from None
        store.record_event(key, "billing_checkout_started",
                           {"plan": plan_name,
                            "subscription_id": sub.get("id")})
        return {"subscription_id": sub.get("id"), "plan": plan_name,
                "provider": provider.name,
                "key_id": getattr(provider, "key_id", None)}

    @app.post("/v1/billing/webhook")
    async def billing_webhook(request: Request):
        """Provider callback. Unauthenticated BY DESIGN, because the
        signature is the authentication.

        Order matters: verify over the raw bytes first; only then parse. No
        field of an unverified payload is read.
        """
        provider = billing.get_provider()
        body = await request.body()
        signature = request.headers.get("X-Razorpay-Signature")
        if not provider.verify_webhook(body, signature):
            raise HTTPException(401, "invalid webhook signature")
        try:
            event = provider.parse_event(body)
            result = billing.apply_event(store, event)
        except billing.BillingError as e:
            raise HTTPException(400, str(e)) from None
        return {"received": True, **result}

    # ── trust ─────────────────────────────────────────────────────────

    @app.get("/v1/trust")
    async def trust_score_endpoint(authorization: str | None = Header(None),
                    checkset: str | None = None,
                    gate: float = 0.6):
        """Live Trust Score for the caller's stored results.

        Tier 'consistency': no labels needed. When the caller has signed
        out assessments, human agreement folds in as one component.
        """
        key = _authorize(authorization)
        return _trust_for(store, key, checkset, gate)

    @app.get("/v1/trust.svg")
    async def trust_badge(authorization: str | None = Header(None),
                          checkset: str | None = None,
                          gate: float = 0.6):
        """A shareable badge: trust level + score + sample size."""
        key = _authorize(authorization)
        ts = _trust_for(store, key, checkset, gate)
        return Response(content=trust.render_badge(ts), media_type="image/svg+xml")

    routes.policies.register(app, store)

    @app.get("/v1/stream")
    async def decision_stream(request: Request = None,
                              authorization: str | None = Header(None),
                              key: str | None = None):
        """Server-Sent Events: every saved result for this key, live.

        EventSource cannot set headers, so the browser passes the key as a
        query param. Locally that is the localhost convenience; in demo mode
        the demo key works from anywhere (it is capped, not admin).
        """
        if key and (_local(request) or demo_mode):
            key = key
            if store.lookup_key(key) is None:
                raise HTTPException(401, "unknown api key")
        else:
            key = _authorize(authorization)
        q = bus.subscribe(key)

        async def gen():
            try:
                yield sse_format({"kind": "hello", "key": "you"})
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield sse_format(msg)
            finally:
                bus.unsubscribe(key, q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

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

    mount_web(app, store, _authorize)
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
        raise HTTPException(402, _collision_message(store, key))

    hit = cache.get(state, questions)
    if hit is not None:
        cached_answers, cached_model = hit
        store.record(user_key=key, judge=(judge_name or "typesafe"), model=cached_model,
                     request_id=None, input_tokens=0, output_tokens=0,
                     questions=len(questions), server_ms=None, cached=1, ok=1)
        return {"answers": [_answer_dict(a) for a in cached_answers],
                "usage": {"cached": True}, "model": cached_model, "cached": True}

    judge = get_judge(judge_name)
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

    cache.put(state, questions, j.answers, j.model)
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
