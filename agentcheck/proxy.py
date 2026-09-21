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
from agentcheck.judges import default_name, get_judge
from agentcheck.limits import AnswerCache, QuotaGuard, WINDOW_SECONDS
from agentcheck.reliability import ReliabilityModel, load_report
from agentcheck.store import Store
from agentcheck.stream import Bus
from agentcheck.web import mount_web


def create_app(store: Store, default_judge: str | None = None,
               cache: "AnswerCache | None" = None,
               bus: "Bus | None" = None,
               demo_mode: bool | None = None,
               auth_provider=None) -> FastAPI:
    # The API surface is not advertised by default. `/docs`, `/redoc` and
    # `/openapi.json` were public on every host — the earlier audit missed them
    # because it swept `/v1` routes only — and they enumerate every endpoint,
    # including the operator-facing ones. A local developer wants them; a
    # deployment should opt in with AGENTCHECK_DOCS=1.
    docs_on = (os.environ.get("AGENTCHECK_DOCS", "").strip()
               in ("1", "true", "yes"))
    app = FastAPI(
        title="agentcheck", version=__import__("agentcheck").__version__,
        docs_url="/docs" if docs_on else None,
        redoc_url="/redoc" if docs_on else None,
        openapi_url="/openapi.json" if docs_on else None,
    )
    # Identity provider: injected in tests, resolved from AGENTCHECK_AUTH in
    # production. NullAuthProvider refuses clearly when unconfigured.
    idp = auth_provider if auth_provider is not None else auth.get_provider()
    # Resolve the judge ONCE, at startup. Building it lazily meant a missing
    # API key surfaced as a 500 on the first check (and killed the request)
    # instead of a startup warning — the opposite of what the Dockerfile and
    # .env.example promise. Falling back to the offline stub keeps a
    # key-less container useful, loudly.
    #
    # The name MUST be resolved here, not left as None: the cache is keyed by
    # judge and the meter records it, so an unresolved None was labelled
    # "typesafe" downstream and reported stub work as TypeSafe's.
    resolved = (default_judge or "").strip() or default_name()
    try:
        get_judge(resolved)
    except Exception as e:
        print(f"[agentcheck] judge {resolved!r} unavailable ({e}); "
              f"falling back to the offline stub judge", file=sys.stderr)
        resolved = "stub"
    default_judge = resolved
    app.state.judge = resolved
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

    # ── decision policies ─────────────────────────────────────────────
    routes.policies.register(app, store)

    routes.stream.register(app, store, bus, demo_mode)

    # ── calibration ───────────────────────────────────────────────────
    routes.calibration.register(app, store, default_judge)

    # ── choosing what to label (the measured-tier path) ───────────────
    routes.labeling.register(app, store)

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
