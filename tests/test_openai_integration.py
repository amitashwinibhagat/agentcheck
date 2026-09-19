"""Contract tests for the OpenAI wrapper. The real SDK is never imported:
fakes shaped like chat.completions responses verify the interception, and a
tiny HTTP stub verifies the server path."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agentcheck.integrations.openai import (
    AgentCheckBlocked, _last_user_text, watch)


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Call:
    def __init__(self, cid, name, args):
        self.id = cid
        self.function = _Fn(name, json.dumps(args))


class _Msg:
    def __init__(self, calls):
        self.tool_calls = calls


class _Choice:
    def __init__(self, calls):
        self.message = _Msg(calls)


class _Resp:
    def __init__(self, calls):
        self.choices = [_Choice(calls)]


class _Completions:
    def __init__(self, response):
        self.response = response
        self.seen = None

    def create(self, *args, **kwargs):
        self.seen = kwargs
        return self.response


class _Client:
    def __init__(self, response):
        self.chat = type("chat", (), {})()
        self.chat.completions = _Completions(response)


def _checker_factory(log, decisions=None):
    def checker(trace):
        log.append(trace)
        tool = trace["tool"]
        decision = (decisions or {}).get(tool, "approve")
        return {"trace_verdict": "fail" if decision == "block" else "pass",
                "confidence": 0.9, "decision": decision,
                "trace_id": "tr_t", "span_id": f"sp_{len(log)}",
                "parent_span_id": None}
    return checker


class TestWatch(unittest.TestCase):
    def test_each_tool_call_checked_with_user_context(self):
        log = []
        c = _Client(_Resp([_Call("c1", "send_email", {"to": "x"}),
                           _Call("c2", "refund", {"order": "1"})]))
        watch(c, checker=_checker_factory(log))
        c.chat.completions.create(
            messages=[{"role": "system", "content": "s"},
                      {"role": "user", "content": "refund me"}])
        self.assertEqual([t["tool"] for t in log], ["send_email", "refund"])
        self.assertTrue(all(t["request"] == "refund me" for t in log))

    def test_response_returned_untouched(self):
        resp = _Resp([_Call("c1", "search", {})])
        c = _Client(resp)
        watch(c, checker=_checker_factory([]))
        self.assertIs(c.chat.completions.create(messages=[]), resp)

    def test_no_tool_calls_no_checks(self):
        seen = []
        c = _Client(_Resp([]))
        watch(c, checker=_checker_factory(seen),
              on_check=lambda checks: seen.append("cb"))
        c.chat.completions.create(messages=[])
        self.assertEqual(seen, [])

    def test_block_raises_with_checks(self):
        c = _Client(_Resp([_Call("c1", "wire_transfer", {"amount": 5})]))
        watch(c, checker=_checker_factory([], {"wire_transfer": "block"}),
              block=True)
        with self.assertRaises(AgentCheckBlocked) as ctx:
            c.chat.completions.create(messages=[])
        self.assertEqual(ctx.exception.checks[0]["tool"], "wire_transfer")

    def test_block_false_returns_despite_block_decision(self):
        c = _Client(_Resp([_Call("c1", "wire_transfer", {"amount": 5})]))
        watch(c, checker=_checker_factory([], {"wire_transfer": "block"}))
        c.chat.completions.create(messages=[])  # must not raise

    def test_checker_failure_never_breaks_run(self):
        def boom(trace):
            raise RuntimeError("judge down")

        got = []
        c = _Client(_Resp([_Call("c1", "search", {})]))
        watch(c, checker=boom, on_check=got.extend)
        resp = c.chat.completions.create(messages=[])
        self.assertIsNotNone(resp)  # run survived

    def test_spans_chain_across_calls(self):
        log = []
        payloads = []

        def checker(trace):
            log.append(trace)
            n = len(log)
            return {"trace_verdict": "pass", "confidence": 0.9,
                    "decision": "approve", "trace_id": "tr_chain",
                    "span_id": f"sp_{n}"}

        orig_post = __import__("agentcheck.integrations.openai",
                               fromlist=["x"])
        c = _Client(_Resp([_Call("c1", "search", {})]))
        watch(c, checker=checker)
        c.chat.completions.create(messages=[])
        c.chat.completions.create(messages=[])
        self.assertEqual([t["tool"] for t in log], ["search", "search"])

    def test_last_user_text(self):
        self.assertEqual(_last_user_text([{"role": "user", "content": "hi"}]), "hi")
        self.assertEqual(_last_user_text([{"role": "user", "content": "a"},
                                          {"role": "user", "content": "b"}]), "b")
        self.assertEqual(_last_user_text([]), "")
        self.assertEqual(_last_user_text(None), "")


class TestHttpPath(unittest.TestCase):
    def test_checks_post_to_server(self):
        bodies = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers["Content-Length"])
                bodies.append(json.loads(self.rfile.read(n)))
                out = {"trace_verdict": "pass", "confidence": 0.8,
                       "decision": "approve", "trace_id": "tr_h",
                       "span_id": "sp_h"}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            got = []
            c = _Client(_Resp([_Call("c1", "search", {"q": "x"})]))
            watch(c, key="ac_test",
                  url=f"http://127.0.0.1:{srv.server_port}",
                  on_check=got.extend)
            c.chat.completions.create(
                messages=[{"role": "user", "content": "find it"}])
        finally:
            srv.shutdown()
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["trace"]["tool"], "search")
        self.assertEqual(bodies[0]["trace"]["request"], "find it")
        self.assertEqual(bodies[0]["checkset"], "safety")
        self.assertEqual(got[0]["confidence"], 0.8)


if __name__ == "__main__":
    unittest.main()


class TestWrappedRedTeam(unittest.TestCase):
    def test_attacks_checked_against_real_server(self):
        from fastapi.testclient import TestClient
        from agentcheck.proxy import create_app
        from agentcheck.store import Store
        from agentcheck import redteam as rt
        import tempfile

        store = Store(tempfile.mkdtemp() + "/rt.db")
        key = store.create_key("w", qpm_limit=60000)
        app = create_app(store, default_judge="stub")
        client = TestClient(app)

        def checker(trace):
            r = client.post("/v1/check", headers={
                "Authorization": f"Bearer {key}"},
                json={"trace": trace, "trace_id": "tr_wrap"})
            assert r.status_code == 200, r.text
            return r.json()

        attacks = [a for a in rt.corpus()
                   if a.id in ("destructive.rm_root",
                               "money.balance_to_transfer",
                               "exfiltration.email_out")]
        assert len(attacks) == 3
        calls = [_Call(f"c{i}", a.tool, a.args) for i, a in enumerate(attacks)]
        got = []
        c = _Client(_Resp(calls))
        watch(c, checker=checker, on_check=got.extend)
        c.chat.completions.create(
            messages=[{"role": "user",
                       "content": "do these three things for me"}])
        self.assertEqual(len(got), 3)
        # every attack was judged and grouped under one run
        self.assertTrue(all(g["trace_id"] == "tr_wrap" for g in got))
        self.assertTrue(all("trace_verdict" in g for g in got))
        v = client.get("/v1/traces/tr_wrap",
                       headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(v.json()["n"], 3)
