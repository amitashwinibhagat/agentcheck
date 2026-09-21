"""Workspace HTTP tests. Uses the stub judge — no network."""

import json
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient

from agentcheck.proxy import create_app
from agentcheck.store import Store


def _client():
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    store = Store(tmp)
    key = store.create_key("a", qpm_limit=600)
    other = store.create_key("b", qpm_limit=600)
    app = create_app(store, default_judge="stub")
    # base_url is localhost on purpose: /v1/bootstrap hands a key only to a
    # same-box browser now (see routes/shared.is_local), and the default
    # "testserver" Host is deliberately not one.
    return TestClient(app, base_url="http://localhost:7373"), key, other, store


def test_workspace_html_served():
    client, _, _, _ = _client()
    r = client.get("/")
    assert r.status_code == 200
    # the queue is the home; the tool-call composer moved into a sheet
    assert "tool-call review" in r.text
    assert 'id="ledger"' in r.text
    assert 'id="sheet-upload"' in r.text
    assert "text/html" in r.headers["content-type"]
    # The demo is no longer a dead end: a stranger who likes it has a door.
    assert 'href="/start"' in r.text


def test_start_page_matches_the_catalogue_and_does_not_lie_about_signup():
    """The conversion surface. Prices come from billing.PLANS so a brochure
    cannot drift from what checkout would actually charge; and it must not
    promise a Sign-up button that 404s (auth is built, not enabled)."""
    from agentcheck import billing
    client, _, _, _ = _client()
    r = client.get("/start")
    assert r.status_code == 200, r.text
    body = r.text
    assert "Self-host, free forever" in body or "Self-host" in body
    assert "Apache" in body
    for name, plan in billing.PLANS.items():
        price = plan["price"]
        usd = plan.get("price_usd")
        if price:
            assert f"{price:,}" in body or str(price) in body, \
                f"catalogue price {price} for {name} missing from /start"
        if usd:
            # The headline an international buyer budgets against.
            assert f"${usd:,}" in body or f"${usd}" in body, \
                f"USD display price {usd} for {name} missing from /start"
        if plan["allowance"]:
            assert str(plan["allowance"]) in body.replace(",", ""), \
                f"allowance {plan['allowance']} for {name} missing from /start"
    assert "Custom" in body and "Enterprise" in body
    # Honesty: hosted self-serve is not open. A CTA that 404s is worse than none.
    assert "not yet enabled" in body.lower() or "not enabled" in body.lower()
    assert 'href="/v1/auth/login"' not in body
    # A marketing page must not be a back door into the product. The logo
    # stays on /start; CTAs go to self-host / GitHub, never to `/`.
    assert 'href="/"' not in body, "/start must not dump a stranger into the app"


def test_bootstrap_gives_local_key():
    client, _, _, _ = _client()
    r = client.get("/v1/bootstrap")
    assert r.status_code == 200, r.text
    assert r.json()["key"].startswith("ac_")


def test_a_public_host_never_hands_out_a_key():
    """The hole: a public instance serving /v1/bootstrap with demo mode lit.

    It handed a working key to anyone, so a stranger could read the whole log
    and spend the judge budget with no sign-in. Two independent guards are
    pinned here: demo mode is off unless explicitly enabled, and a non-local
    caller is refused even then — the hand-out is for a same-box browser (the
    Docker healthcheck), never for the internet.
    """
    from fastapi.testclient import TestClient
    from agentcheck.proxy import create_app
    from agentcheck.store import Store

    store = Store(Path(tempfile.mkdtemp()) / "pub.db")
    store.create_key("a", qpm_limit=600)
    # A real public peer, not the test transport's "testclient".
    public = ("203.0.113.7", 51234)
    client = TestClient(create_app(store, default_judge="stub", demo_mode=False),
                        base_url="https://prod.example", client=public)
    r = client.get("/v1/bootstrap")
    assert r.status_code == 403, f"a public host handed out a key: {r.text}"
    assert "key" not in r.text


def test_demo_mode_is_public_by_design_and_that_is_why_hosts_run_demo_0():
    """The opt-in exception, stated so nobody 'fixes' it by accident.

    AGENTCHECK_DEMO=1 exists to hand a capped key to anyone — that is what a
    demo box is. The security property is that it is OFF by default and that
    the deployed hosts set it explicitly, not that demo mode is unreachable.
    """
    from fastapi.testclient import TestClient
    from agentcheck.proxy import create_app
    from agentcheck.store import Store

    store = Store(Path(tempfile.mkdtemp()) / "dm.db")
    store.create_key("a", qpm_limit=600)
    public = ("203.0.113.7", 51234)
    demo = TestClient(create_app(store, default_judge="stub", demo_mode=True),
                      base_url="https://demo.example", client=public)
    r = demo.get("/v1/bootstrap")
    assert r.status_code == 200, "demo mode is supposed to hand out a key"
    body = r.json()
    assert body.get("demo") is True
    # ...but it is the capped demo key, not the operator's browser key.
    assert store.key_meta(body["key"])["monthly_allowance"] <= 40000


def test_a_proxied_request_is_never_local_even_from_loopback():
    """The bypass that a peer-address check cannot see.

    Behind Caddy with host networking (or nginx, or a `-p 127.0.0.1:7373:7373`
    publish) every public request arrives from loopback, so `is_local` would
    say yes and hand the internet a key. Proxy headers are the tell.
    """
    from fastapi.testclient import TestClient
    from agentcheck.proxy import create_app
    from agentcheck.store import Store

    store = Store(Path(tempfile.mkdtemp()) / "px.db")
    store.create_key("a", qpm_limit=600)
    app = create_app(store, default_judge="stub", demo_mode=False)
    loopback = ("127.0.0.1", 40000)

    # Same-box browser: allowed, that is the documented convenience.
    ok = TestClient(app, base_url="http://localhost:7373", client=loopback)
    assert ok.get("/v1/bootstrap").status_code == 200

    # Loopback peer, but the request came through a proxy: refused.
    for header in ({"X-Forwarded-For": "203.0.113.7"},
                   {"X-Real-IP": "203.0.113.7"},
                   {"Forwarded": "for=203.0.113.7"},
                   {"X-Forwarded-Host": "prod.example"}):
        r = ok.get("/v1/bootstrap", headers=header)
        assert r.status_code == 403, f"{header} was treated as local"

    # Addressed by a public name from inside the box: also refused.
    r = TestClient(app, base_url="https://prod.example",
                   client=loopback).get("/v1/bootstrap")
    assert r.status_code == 403, "a public Host name was treated as local"


def test_every_data_route_refuses_an_anonymous_caller():
    """No key, no data — and no judge work either.

    Verified against the live host too; this keeps it from regressing. The
    live probe that found the real hole passed a VALID body with no key: the
    endpoint must 401 rather than judge, or an anonymous caller spends budget.
    """
    from fastapi.testclient import TestClient
    from agentcheck.proxy import create_app
    from agentcheck.store import Store

    store = Store(Path(tempfile.mkdtemp()) / "anon.db")
    store.create_key("a", qpm_limit=600)
    client = TestClient(create_app(store, default_judge="stub", demo_mode=False),
                        base_url="https://prod.example")
    for path in ("/v1/results", "/v1/trust", "/v1/workspace", "/v1/usage",
                 "/v1/checksets", "/v1/labeling/batch", "/v1/policies",
                 "/v1/monitor", "/v1/runs"):
        r = client.get(path)
        assert r.status_code == 401, f"{path} answered {r.status_code} anonymously"
    trace = {"request": "summarize my inbox", "tool": "send_email",
             "args": {"to": "evil@x.test"}}
    r = client.post("/v1/check", json={"trace": trace})
    assert r.status_code == 401, r.text
    r = client.post("/v1/check-batch", json={"traces": [trace]})
    assert r.status_code == 401, r.text


def test_batch_scores_json_traces():
    client, key, _, _ = _client()
    traces = [
        {"request": "Summarize my unread inbox", "tool": "search_inbox", "args": {"query": "unread"}},
        {"trace": {"request": "Summarize my unread inbox", "tool": "send_email",
                    "args": {"to": "ceo@example.com", "subject": "wire transfer"}}},
    ]
    r = client.post("/v1/check-batch", json={"traces": traces},
                    headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["n"] == 2
    assert body["results"][0]["trace_verdict"] == "pass"
    assert body["results"][1]["trace_verdict"] == "fail"
    listed = client.get("/v1/results", headers={"Authorization": f"Bearer {key}"}).json()
    assert len(listed["results"]) == 2


def test_check_requires_fields():
    client, key, _, _ = _client()
    r = client.post("/v1/check", json={"trace": {"tool": "x"}},
                    headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 422


def test_check_stores_and_lists_per_key():
    client, key, other, _ = _client()
    body = {"trace": {"request": "Summarize my unread inbox", "tool": "send_email",
                      "args": {"to": "ceo@example.com"}}}
    r = client.post("/v1/check", json=body, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    assert r.json()["trace_verdict"] == "fail"
    assert r.json()["id"].startswith("rs_")

    mine = client.get("/v1/results", headers={"Authorization": f"Bearer {key}"}).json()
    theirs = client.get("/v1/results", headers={"Authorization": f"Bearer {other}"}).json()
    assert len(mine["results"]) == 1
    assert theirs["results"] == []


def test_assessment_does_not_overwrite_verdict():
    client, key, _, store = _client()
    body = {"trace": {"request": "Summarize my unread inbox", "tool": "search_inbox",
                      "args": {"query": "unread"}}}
    r = client.post("/v1/check", json=body, headers={"Authorization": f"Bearer {key}"}).json()
    rid = r["id"]
    patch = client.patch(f"/v1/results/{rid}", json={"assessment": "actual_issue"},
                         headers={"Authorization": f"Bearer {key}"})
    assert patch.status_code == 200
    got = client.get(f"/v1/results/{rid}", headers={"Authorization": f"Bearer {key}"}).json()
    assert got["trace_verdict"] == "pass"
    assert got["assessment"] == "actual_issue"


def test_cross_account_result_is_404():
    client, key, other, _ = _client()
    body = {"trace": {"request": "hi", "tool": "search_inbox", "args": {}}}
    rid = client.post("/v1/check", json=body, headers={"Authorization": f"Bearer {key}"}).json()["id"]
    r = client.get(f"/v1/results/{rid}", headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 404


def test_usage_is_per_key():
    client, key, other, _ = _client()
    body = {"trace": {"request": "hi", "tool": "search_inbox", "args": {}}}
    client.post("/v1/check", json=body, headers={"Authorization": f"Bearer {key}"})
    a = client.get("/v1/usage", headers={"Authorization": f"Bearer {key}"}).json()
    b = client.get("/v1/usage", headers={"Authorization": f"Bearer {other}"}).json()
    assert a["totals"]["calls"] >= 1
    assert b["totals"]["calls"] == 0


def test_judge_failure_is_not_a_pass(monkeypatch):
    from agentcheck.judges import stub as stub_mod

    def boom(self, state, questions):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(stub_mod.StubJudge, "ask", boom)
    client, key, _, _ = _client()
    r = client.post("/v1/check",
                    json={"trace": {"request": "x", "tool": "y", "args": {}}},
                    headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 502
    assert "not a pass" in r.json()["detail"].lower()


def test_platform_endpoints():
    client, key, other, _ = _client()
    h = {"Authorization": f"Bearer {key}"}

    # rubric catalogue is public to any valid key
    r = client.get("/v1/checksets", headers=h)
    assert r.status_code == 200, r.text
    names = {c["name"] for c in r.json()["checksets"]}
    assert {"safety", "refund-policy"} <= names, names
    safety = next(c for c in r.json()["checksets"] if c["name"] == "safety")
    assert safety["builtin"] is True and safety["verdict_check"] == "verdict"
    refund = next(c for c in r.json()["checksets"] if c["name"] == "refund-policy")
    assert refund["verdict_check"] == "decision", refund

    # a non-default rubric routes the verdict through its own check id
    body = {"trace": {"request": "Refund my order", "tool": "refund",
                      "args": {"order": "1042"}},
            "checkset": "refund-policy"}
    r = client.post("/v1/check", json=body, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["checkset"] == "refund-policy"
    verdict = r.json()["checks"]["decision"]["value"]
    assert verdict in {"approve", "escalate", "deny", "wrong_tool"}, verdict

    # the same trace under a different rubric must NOT dedupe onto the first
    r2 = client.post("/v1/check", json={"trace": body["trace"],
                                        "checkset": "safety"}, headers=h)
    assert r2.status_code == 200
    assert r2.json()["checkset"] == "safety", "rubric must be part of trace identity"
    assert r2.json()["id"] != r.json()["id"], "different rubric must not reuse a result"

    # monitor + drift
    assert client.get("/v1/monitor", headers=h).status_code == 200
    assert client.get("/v1/monitor?bucket=day&days=30", headers=h).status_code == 200
    assert client.get("/v1/monitor?bucket=fortnight", headers=h).status_code == 422
    assert client.get("/v1/monitor/drift", headers=h).status_code == 200

    # calibration over the bundled seed
    r = client.get("/v1/calibration?dataset=seed&checkset=safety", headers=h)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["decided"]["ece"] is not None and "read" in rep
    assert client.get("/v1/calibration?dataset=nope", headers=h).status_code == 404
    assert client.get("/v1/calibration?checkset=nope", headers=h).status_code == 422

    # datasets + evals listings are empty-but-valid here
    assert client.get("/v1/datasets", headers=h).status_code == 200
    assert client.get("/v1/evals", headers=h).status_code == 200
    assert client.get("/v1/redteam/families").json()["families"]

    # unauthorised calls are refused everywhere
    for path in ("/v1/checksets", "/v1/monitor", "/v1/datasets", "/v1/evals"):
        assert client.get(path).status_code == 401, path


def test_redteam_endpoint_runs_and_reports():
    client, key, _, _ = _client()
    h = {"Authorization": f"Bearer {key}"}
    r = client.post("/v1/redteam?checkset=safety&families=destructive", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    from agentcheck import redteam as rt
    assert d["n"] == len(rt.corpus(["destructive"])), d
    assert d["checkset"] == "safety"
    assert 0.0 <= d["asr"] <= 1.0, d["asr"]
    assert set(d["by_family"]) == {"destructive"}, d["by_family"]
    assert d["evaded"] == [a for a in d["attacks"] if a["evaded"]]
    for a in d["attacks"]:
        assert a["trace"]["request"] and a["trace"]["tool"]
    # an unknown family is a client error, not a 500
    assert client.post("/v1/redteam?families=nope", headers=h).status_code == 422


def test_redteam_run_feeds_trust_score():
    client, key, _, store = _client()
    h = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/trust", headers=h).json()["adversarial"] is None
    r = client.post("/v1/redteam?checkset=safety&families=money", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    ts = client.get("/v1/trust", headers=h).json()
    adv = ts["adversarial"]
    assert adv is not None, ts
    from agentcheck import redteam as rt
    assert adv["n"] == d["n"] == len(rt.corpus(["money"]))
    assert adv["asr"] == d["asr"]
    # latest_event is what the endpoint reads (identity, not token)
    kid = store.lookup_key(key)["kid"]
    assert store.latest_event(kid, "redteam_run")["n"] == d["n"]
    # and a bad rubric likewise
    assert client.post("/v1/redteam?checkset=nope", headers=h).status_code == 422


def test_trace_run_groups_steps_oldest_first():
    client, key, _, _ = _client()
    h = {"Authorization": f"Bearer {key}"}
    parent = None
    spans = []
    for i, tool in enumerate(["search", "read_file", "send_email"]):
        body = {"trace": {"request": f"s{i}", "tool": tool, "args": {}},
                "trace_id": "tr_run1"}
        if parent:
            body["parent_span_id"] = parent
        r = client.post("/v1/check", json=body, headers=h)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["trace_id"] == "tr_run1"
        assert d["span_id"].startswith("sp_")
        parent = d["span_id"]
        spans.append(parent)
    v = client.get("/v1/traces/tr_run1", headers=h)
    assert v.status_code == 200, v.text
    d = v.json()
    assert d["n"] == 3
    assert [s["tool"] for s in d["steps"]] == ["search", "read_file", "send_email"]
    assert [s["span_id"] for s in d["steps"]] == spans
    assert d["steps"][1]["parent_span_id"] == spans[0]
    assert d["steps"][0]["parent_span_id"] is None
    assert client.get("/v1/traces/tr_missing", headers=h).status_code == 404


def test_check_mints_trace_identity_when_absent():
    client, key, _, _ = _client()
    h = {"Authorization": f"Bearer {key}"}
    r = client.post("/v1/check", headers=h,
                    json={"trace": {"request": "solo", "tool": "search",
                                    "args": {}}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["trace_id"].startswith("tr_")
    assert d["span_id"].startswith("sp_")
    assert d["parent_span_id"] is None


def test_batch_honours_an_explicit_trace_id():
    """A caller who passes one trace_id is saying "these rows are one trace"."""
    client, key, _, _ = _client()
    h = {"Authorization": f"Bearer {key}"}
    traces = [{"request": f"b{i}", "tool": "search", "args": {}} for i in range(3)]
    r = client.post("/v1/check-batch", headers=h,
                    json={"traces": traces, "trace_id": "tr_batch1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["trace_id"] == "tr_batch1"
    assert {row["trace_id"] for row in d["results"]} == {"tr_batch1"}
    v = client.get("/v1/traces/tr_batch1", headers=h)
    assert v.json()["n"] == 3


def test_batch_rows_are_independent_traces_by_default():
    """An upload of unrelated calls must not read as one giant run.

    The batch used to stamp a single generated trace_id on every row, so 40
    independent calls appeared in Runs as one 40-step run, and the dock's run
    strip listed all 40 as if they were a sequence. The run_id is what groups
    an upload; the trace_id says "a sequence of steps".
    """
    client, key, _, _ = _client()
    h = {"Authorization": f"Bearer {key}"}
    traces = [{"request": f"b{i}", "tool": "search", "args": {"q": i}}
              for i in range(3)]
    r = client.post("/v1/check-batch", headers=h, json={"traces": traces})
    assert r.status_code == 200, r.text
    rows = r.json()["results"]
    assert len({row["trace_id"] for row in rows}) == 3, \
        "independent calls must be independent traces"
    assert len({row["run_id"] for row in rows}) == 1, "one upload is one run"
    # ...and a row that carries its own trace_id still groups with its peers.
    traces = [{"request": "a", "tool": "search", "args": {"q": 1}, "trace_id": "tr_x"},
              {"request": "b", "tool": "search", "args": {"q": 2}, "trace_id": "tr_x"},
              {"request": "c", "tool": "search", "args": {"q": 3}, "trace_id": "tr_y"}]
    rows = client.post("/v1/check-batch", headers=h,
                       json={"traces": traces}).json()["results"]
    assert [row["trace_id"] for row in rows] == ["tr_x", "tr_x", "tr_y"]


def test_catalog_has_at_least_21_and_stub_matches_labels():
    import json
    from agentcheck.judges.stub import classify
    examples = json.loads(
        (Path(__file__).resolve().parents[1] / "agentcheck/web/static/examples.json").read_text()
    )
    assert len(examples) >= 21
    ids = [e["id"] for e in examples]
    assert len(ids) == len(set(ids))
    for ex in examples:
        got = classify({"request": ex["request"], "tool": ex["tool"], "args": ex["args"]})
        assert got == ex["expect"], f"{ex['id']}: expected {ex['expect']}, got {got}"


def test_published_calibration_upgrades_trust_tier(monkeypatch):
    """The measured tier must be reachable through the API it is sold on.

    `calibrate --publish` writes calibration.json for the reliability model,
    but /v1/trust read nothing, so the tier could not leave 'consistency'
    however many labels were supplied. Wired now; this exercises the path the
    product uses — publish a report, ask /v1/trust."""
    client, key, _, store = _client()
    headers = {"Authorization": f"Bearer {key}"}
    # Point AGENTCHECK_HOME at the temp store so the published file is found.
    monkeypatch.setenv("AGENTCHECK_HOME", str(store.path.parent))
    client.post("/v1/check-batch",
                json={"traces": [{"request": f"r{i}", "tool": "search_inbox",
                                  "args": {"q": i}} for i in range(12)]},
                headers=headers)

    before = client.get("/v1/trust", headers=headers).json()
    assert before["tier"] == "consistency", before

    # The exact shape `agentcheck calibrate --publish` writes.
    (store.path.parent / "calibration.json").write_text(json.dumps({
        "judge": "typesafe", "checkset": "safety", "dataset": "agent-demo",
        "decided": {"n": 40, "ece": 0.04, "accuracy": 0.91},
    }))

    after = client.get("/v1/trust", headers=headers).json()
    assert after["tier"] == "measured", after
    assert after["dataset"] == "agent-demo"
    assert after["ece"] == 0.04

    # A calibration for a different rubric must not upgrade this one.
    other = client.get("/v1/trust?checkset=refund-policy", headers=headers).json()
    assert other["tier"] == "consistency", other


def test_rag_metrics_endpoint_scores_a_trace():
    """POST /v1/metrics/rag had zero coverage, which is how a refactor broke
    its request model without a single test failing: FastAPI could no longer
    resolve `req`, so every call 422'd as 'missing query req'. Pins both the
    shape rejection and a successful score under the stub judge."""
    client, key, _, _ = _client()
    h = {"Authorization": f"Bearer {key}"}
    r = client.post("/v1/metrics/rag", json={"state": {"request": "x"}}, headers=h)
    assert r.status_code == 422, r.text
    trace = {"question": "What is the refund window?",
             "answer": "30 days.",
             "contexts": ["Refunds are available within 30 days of purchase."]}
    r = client.post("/v1/metrics/rag", json={"state": trace}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "metrics" in body and body["usage"]["questions"] > 0


def test_policies_endpoint_lists_the_shipped_policies():
    """No test covered /v1/policies, so when a refactor dropped its register()
    call the endpoint 404'd and the suite stayed green. The browser test found
    it; this makes it a unit-level failure next time."""
    client, key, _, _ = _client()
    r = client.get("/v1/policies", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    names = {p["name"] for p in r.json()["policies"]}
    assert names, "at least one policy must be listed"


def test_every_declared_route_is_actually_registered():
    """Cross-check the source against the running app.

    A route module can be imported, parse fine, and never be wired up — which
    is exactly how /v1/policies went missing: the module existed and the
    decorator existed, but nothing called register(). Comparing what the
    modules declare with what the app exposes catches that, plus a decorator
    silently overwritten by a duplicate path.
    """
    import re
    from pathlib import Path

    from agentcheck.proxy import create_app

    declared = set()
    for f in sorted(Path("agentcheck/routes").glob("*.py")):
        for verb, path in re.findall(
                r"@app\.(get|post|patch|delete|put)\(\s*['\"]([^'\"]+)",
                f.read_text()):
            declared.add((verb.upper(), path))
    assert len(declared) > 30, f"route scan found only {len(declared)}; the regex broke"

    store = Store(Path(tempfile.mkdtemp()) / "routes.db")
    app = create_app(store, default_judge="stub")
    registered = {(m, r.path) for r in app.routes
                  for m in (getattr(r, "methods", None) or set())}

    missing = sorted(declared - registered)
    assert not missing, f"declared but never registered: {missing}"
