"""Postgres backend contract. Runs only when AGENTCHECK_TEST_PGURL is set
(e.g. a throwaway instance); SQLite remains the default and is covered by
the rest of the suite. The point: the SAME store code paths work on both
engines, so multi-machine deploys stop sharing a SQLite file.
"""

import os
import unittest

PGURL = os.environ.get("AGENTCHECK_TEST_PGURL", "")


@unittest.skipUnless(PGURL, "needs AGENTCHECK_TEST_PGURL")
class TestPostgres(unittest.TestCase):
    def test_full_loop(self):
        from agentcheck.store import Store
        import uuid
        s = Store(PGURL)
        self.assertEqual(s.dialect, "postgres")
        raw = s.create_key("pg-test")
        kid = s.lookup_key(raw)["kid"]
        r1 = "r_" + uuid.uuid4().hex[:8]
        s.record(user_key=kid, judge="stub", model="stub",
                 request_id=r1, input_tokens=1, output_tokens=1,
                 questions=5, server_ms=1.0, cached=0, ok=1)
        self.assertEqual(s.used_this_month(kid), 5)
        tid = "tr_" + uuid.uuid4().hex[:8]
        r = s.save_result(
            kid, {"request": "hi", "tool": "search", "args": {}},
            {"trace_verdict": "pass", "confidence": 0.9, "severity": 0,
             "checks": {}, "usage": {}, "model": "stub", "cached": False},
            trace_id=tid, span_id="sp_pg")
        self.assertEqual(len(s.trace_steps(kid, tid)), 1)
        s.record_event(kid, "redteam_run", {"asr": 0.05, "n": 100})
        self.assertEqual(s.latest_event(kid, "redteam_run")["n"], 100)
        self.assertTrue(s.assess(kid, r["id"], "looks_correct"))
        f = s.funnel()
        self.assertGreaterEqual(f["stages"]["signed_up"], 1)

    def test_server_endpoints_on_pg(self):
        from fastapi.testclient import TestClient
        from agentcheck.proxy import create_app
        from agentcheck.store import Store
        import uuid
        s = Store(PGURL)
        key = s.ensure_named_key("pg-demo", qpm_limit=60000,
                                 monthly_allowance=50000)
        c = TestClient(create_app(s, default_judge="stub"))
        h = {"Authorization": f"Bearer {key}"}
        tid = "tr_" + uuid.uuid4().hex[:8]
        r = c.post("/v1/check", headers=h, json={
            "trace": {"request": "x", "tool": "search", "args": {}},
            "trace_id": tid})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            c.get(f"/v1/traces/{tid}", headers=h).json()["n"], 1)


if __name__ == "__main__":
    unittest.main()
