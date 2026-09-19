"""Run (trace) aggregation and the persisted decision.

Two things are load-bearing here:

  * a run is a group of steps sharing a trace_id, and the summary is
    aggregated in SQL so a page does not read every step in the table.
  * the DECISION is persisted. It used to be computed, returned, streamed and
    then dropped — so the log could never answer "what did we tell you to do?"
    after the fact. These tests pin that down.
"""

import tempfile
import unittest
from pathlib import Path

from agentcheck.store import Store

def _client():
    from fastapi.testclient import TestClient
    from agentcheck.proxy import create_app
    store = Store(Path(tempfile.mkdtemp()) / "runs.db")
    key = store.create_key("acct", qpm_limit=60000)
    app = create_app(store, default_judge="stub")
    return TestClient(app), key, store


def _step(client, key, trace_id, request, tool, args=None, policy=None):
    body = {"trace": {"request": request, "tool": tool, "args": args or {}},
            "trace_id": trace_id}
    if policy:
        body["policy"] = policy
    r = client.post("/v1/check", headers={"Authorization": f"Bearer {key}"},
                    json=body)
    assert r.status_code == 200, r.text
    return r.json()


class TestDecisionPersisted(unittest.TestCase):
    def test_decision_survives_the_round_trip(self):
        # the bug this exists for: decision was returned but never stored
        client, key, store = _client()
        d = _step(client, key, "tr_x", "free up disk space", "shell_exec",
                  {"cmd": "rm -rf /"}, policy="demo-strict")
        self.assertEqual(d["trace_verdict"], "fail")
        self.assertEqual(d["decision"], "block")
        steps = client.get("/v1/traces/tr_x",
                           headers={"Authorization": f"Bearer {key}"}).json()["steps"]
        self.assertEqual(steps[0]["decision"], "block")
        self.assertEqual(steps[0]["policy"], "demo-strict")

    def test_no_policy_means_no_decision_not_a_default(self):
        client, key, _ = _client()
        d = _step(client, key, "tr_np", "find the invoice", "search_files")
        self.assertIsNone(d.get("decision"))
        steps = client.get("/v1/traces/tr_np",
                           headers={"Authorization": f"Bearer {key}"}).json()["steps"]
        self.assertIsNone(steps[0]["decision"])

    def test_review_is_routed_to_a_human(self):
        # demo-strict used to approve high-confidence reviews, which made a
        # policy named "strict" quietly approve the ambiguous cases
        client, key, _ = _client()
        d = _step(client, key, "tr_rv", "do something ambiguous", "shell_exec",
                  {}, policy="demo-strict")
        self.assertIn(d["trace_verdict"], ("pass", "review", "fail"))
        if d["trace_verdict"] == "review":
            self.assertEqual(d["decision"], "human")


class TestRunsEndpoint(unittest.TestCase):
    def _seed(self):
        client, key, store = _client()
        h = {"Authorization": f"Bearer {key}"}
        # a blocked run
        _step(client, key, "tr_blocked", "find the config", "read_file",
              {"path": "c.toml"}, policy="demo-strict")
        _step(client, key, "tr_blocked", "free up space", "shell_exec",
              {"cmd": "rm -rf /"}, policy="demo-strict")
        # a clean run
        _step(client, key, "tr_clean", "find the invoice", "search_files")
        _step(client, key, "tr_clean", "open it", "read_file")
        return client, key, store, h

    def test_groups_and_orders_newest_first(self):
        client, _, _, h = self._seed()
        d = client.get("/v1/runs", headers=h).json()
        self.assertEqual(d["n"], 2)
        self.assertEqual([r["trace_id"] for r in d["runs"]],
                         ["tr_clean", "tr_blocked"])
        self.assertEqual(d["blocked"], 1)

    def test_summary_fields(self):
        client, _, _, h = self._seed()
        runs = {r["trace_id"]: r for r in client.get("/v1/runs", headers=h).json()["runs"]}
        blocked = runs["tr_blocked"]
        self.assertEqual(blocked["step_count"], 2)
        self.assertEqual(blocked["failed"], 1)
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["tools"], ["read_file", "shell_exec"])
        self.assertGreaterEqual(blocked["duration_ms"], 0)
        self.assertFalse(runs["tr_clean"]["blocked"])

    def test_filter_blocked(self):
        client, _, _, h = self._seed()
        d = client.get("/v1/runs?only=blocked", headers=h).json()
        self.assertEqual([r["trace_id"] for r in d["runs"]], ["tr_blocked"])

    def test_filter_by_tool(self):
        client, _, _, h = self._seed()
        d = client.get("/v1/runs?tool=shell_exec", headers=h).json()
        self.assertEqual([r["trace_id"] for r in d["runs"]], ["tr_blocked"])
        self.assertEqual(
            client.get("/v1/runs?tool=nope", headers=h).json()["runs"], [])

    def test_bad_filter_is_422(self):
        client, _, _, h = self._seed()
        self.assertEqual(client.get("/v1/runs?only=nope", headers=h).status_code, 422)

    def test_limit_is_respected(self):
        client, _, _, h = self._seed()
        self.assertEqual(len(client.get("/v1/runs?limit=1", headers=h).json()["runs"]), 1)

    def test_requires_auth(self):
        client, _, _, _ = self._seed()
        self.assertEqual(client.get("/v1/runs").status_code, 401)

    def test_runs_are_per_key(self):
        client, key, store, h = self._seed()
        other = store.create_key("other", qpm_limit=60000)
        d = client.get("/v1/runs",
                       headers={"Authorization": f"Bearer {other}"}).json()
        self.assertEqual(d["runs"], [])

    def test_legacy_rows_without_trace_id_are_not_runs(self):
        client, key, store, h = self._seed()
        kid = store.lookup_key(key)["kid"]
        store.save_result(kid, {"request": "old style", "tool": "search",
                                "args": {}},
                          {"trace_verdict": "pass", "confidence": 0.9,
                           "severity": 0, "checks": {}, "usage": {},
                           "model": "stub", "cached": False})
        d = client.get("/v1/runs", headers=h).json()
        self.assertEqual(d["n"], 2)  # the traceless row is not a run


if __name__ == "__main__":
    unittest.main()
