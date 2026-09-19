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
    return TestClient(app), key, other, store


def test_workspace_html_served():
    client, _, _, _ = _client()
    r = client.get("/")
    assert r.status_code == 200
    # the queue is the home; the tool-call composer moved into a sheet
    assert "tool-call review" in r.text
    assert 'id="ledger"' in r.text
    assert 'id="sheet-upload"' in r.text
    assert "text/html" in r.headers["content-type"]


def test_bootstrap_gives_local_key():
    client, _, _, _ = _client()
    r = client.get("/v1/bootstrap")
    assert r.status_code == 200, r.text
    assert r.json()["key"].startswith("ac_")


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


def test_batch_rows_share_one_trace_id():
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
