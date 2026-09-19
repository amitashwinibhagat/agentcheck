"""The metered proxy.

Flow:  auth -> quota guard (BEFORE forwarding) -> cache -> judge router -> meter
"""

from __future__ import annotations

import os
import sys

from fastapi import FastAPI

from agentcheck import auth
from agentcheck import tracing
from agentcheck import routes
from agentcheck.judges import get_judge
from agentcheck.limits import AnswerCache, QuotaGuard, WINDOW_SECONDS
from agentcheck.reliability import ReliabilityModel, load_report
from agentcheck.store import Store
from agentcheck.stream import Bus
from agentcheck.web import mount_web


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
    routes.calibration.register(app, store, default_judge)

    routes.redteam.register(app, store, guard, default_judge)
    routes.runs.register(app, store)

    # ── eval reports on disk ──────────────────────────────────────────
    routes.evals.register(app, store)

    # ── RAG metrics ───────────────────────────────────────────────────
    routes.rag.register(app, store, guard, default_judge)
    routes.account.register(app, store, guard, demo_mode, demo_key, ui_key)

    mount_web(app)
    routes.results.register(app, store)
    return app
