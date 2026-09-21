"""Tests for the live decision stream + observe SDK (Build 3)."""

import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agentcheck.proxy import create_app
from agentcheck.store import Store
from agentcheck.stream import Bus, result_payload, sse_format


def _client():
    tmp = Path(__file__).parent / "tmp_stream.db"
    if tmp.exists():
        tmp.unlink()
    store = Store(tmp)
    key = store.create_key("a", qpm_limit=100000)
    app = create_app(store, default_judge="stub")
    return TestClient(app), key, store


class TestBus(unittest.TestCase):
    def test_publish_reaches_subscriber(self):
        bus = Bus()
        q = bus.subscribe("k")
        bus.publish("k", {"kind": "result", "id": "x"})
        self.assertEqual(q.get_nowait()["id"], "x")

    def test_no_cross_key_leak(self):
        bus = Bus()
        q = bus.subscribe("k1")
        bus.publish("k2", {"kind": "result"})
        self.assertTrue(q.empty())

    def test_slow_subscriber_drops_oldest(self):
        bus = Bus(max_queue=2)
        q = bus.subscribe("k")
        for i in range(5):
            bus.publish("k", {"i": i})
        got = []
        while not q.empty():
            got.append(q.get_nowait()["i"])
        self.assertEqual(got, [3, 4])  # oldest dropped, newest kept


class TestSseFormat(unittest.TestCase):
    def test_event_name_present(self):
        out = sse_format({"kind": "result", "id": 1})
        self.assertIn("event: result", out)
        self.assertIn("data: ", out)

    def test_payload_shape(self):
        p = result_payload({"id": "rs_1", "trace_verdict": "fail",
                            "confidence": 0.9, "decision": "block",
                            "policy": "p"})
        self.assertEqual(p["kind"], "result")
        self.assertEqual(p["verdict"], "fail")
        self.assertEqual(p["decision"], "block")


class TestStreamEndpoint(unittest.TestCase):
    def test_stream_requires_auth(self):
        client, _, _ = _client()
        r = client.get("/v1/stream")
        self.assertEqual(r.status_code, 401)

    def test_stream_sends_hello_with_key_param(self):
        client, key, _ = _client()
        # Drive the StreamingResponse's iterator directly: the stream is
        # infinite, so reading it over HTTP would never end.
        import asyncio
        from starlette.requests import Request

        route = [r for r in client.app.routes
                 if getattr(r, "path", "") == "/v1/stream"][0]

        scope = {"type": "http", "method": "GET", "path": "/v1/stream",
                 "query_string": f"key={key}".encode(),
                 # A real client always sends Host; is_local requires it to be
                 # a localhost name now (a public name is not a same-box
                 # browser, and that predicate gates the key hand-out).
                 "headers": [(b"host", b"localhost:7373")],
                 "client": ("127.0.0.1", 1)}
        req = Request(scope)

        async def first_event():
            resp = await route.endpoint(request=req, key=key)
            return await resp.body_iterator.__anext__()

        chunk = asyncio.run(first_event())
        self.assertIn("event: hello", chunk)

    def test_stream_rejects_unknown_key_from_localhost(self):
        client, _, _ = _client()
        r = client.get("/v1/stream?key=ac_bogus")
        self.assertEqual(r.status_code, 401)


class TestTiming(unittest.TestCase):
    def test_check_reports_timing(self):
        client, key, _ = _client()
        r = client.post("/v1/check", headers={"Authorization": f"Bearer {key}"},
                        json={"trace": {"request": "t", "tool": "read_file",
                                        "args": {}}})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("timing_ms", d)
        self.assertIn("judge", d["timing_ms"])
        self.assertIn("total", d["timing_ms"])
        self.assertGreaterEqual(d["timing_ms"]["total"],
                                d["timing_ms"]["judge"])


class TestObserve(unittest.TestCase):
    def test_observe_inline(self):
        from agentcheck import observe
        client, key, _ = _client()
        # point the SDK at the test transport via a real server is hard;
        # instead verify the error path and the result object shape
        with observe(tool="t", request="r", key=None) as obs:
            pass
        self.assertIn("no key", obs.error)

    def test_bad_key_is_error_not_raise(self):
        from agentcheck import observe
        with observe(tool="t", request="r", key="ac_wrong",
                     url="http://127.0.0.1:9", timeout=2) as obs:
            pass
        self.assertTrue(obs.error)


if __name__ == "__main__":
    unittest.main()
