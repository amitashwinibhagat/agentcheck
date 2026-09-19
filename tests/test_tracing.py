"""Tests for OTel export. The SDK lives in the test env; production treats
it as optional (every helper is a no-op without it)."""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agentcheck import tracing


def _provider_with(exporter):
    from opentelemetry import trace as _t
    from opentelemetry.sdk import trace as _s
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    provider = _t.get_tracer_provider()
    if not isinstance(provider, _s.TracerProvider):
        provider = _s.TracerProvider()
        _t.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def _client(key="k"):
    from fastapi.testclient import TestClient
    from agentcheck.proxy import create_app
    from agentcheck.store import Store
    store = Store(Path(tempfile.mkdtemp()) / "tr.db")
    k = store.create_key("t", qpm_limit=60000)
    return TestClient(create_app(store, default_judge="stub")), k


class TestTracing(unittest.TestCase):
    def test_check_emits_span_with_verdict_attrs(self):
        from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
        spans = []

        class Mem(SpanExporter):
            def export(self, s):
                spans.extend(s)
                return SpanExportResult.SUCCESS

            def shutdown(self):
                pass

        _provider_with(Mem())
        self.assertTrue(tracing.configure())
        client, key = _client()
        r = client.post("/v1/check", headers={"Authorization": f"Bearer {key}"},
                        json={"trace": {"request": "hi", "tool": "search",
                                        "args": {}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(spans), 1)
        s = spans[0]
        self.assertEqual(s.name, "agentcheck.check")
        a = dict(s.attributes or {})
        self.assertEqual(a["agentcheck.tool"], "search")
        self.assertIn(a["agentcheck.verdict"], ("pass", "review", "fail"))
        self.assertEqual(a["agentcheck.trace_id"], r.json()["trace_id"])
        # a verdict is data, never an error status
        from opentelemetry.trace import StatusCode
        self.assertEqual(s.status.status_code, StatusCode.UNSET)

    def test_traceparent_joins_caller_trace(self):
        from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
        spans = []

        class Mem(SpanExporter):
            def export(self, s):
                spans.extend(s)
                return SpanExportResult.SUCCESS

            def shutdown(self):
                pass

        _provider_with(Mem())
        client, key = _client()
        tp = ("00-4bf92f3577b34da6a3ce929d0e0e4736-"
              "00f067aa0ba902b7-01")
        r = client.post("/v1/check", headers={"Authorization": f"Bearer {key}",
                                              "traceparent": tp},
                        json={"trace": {"request": "hi", "tool": "search",
                                        "args": {}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(spans), 1)
        parent = spans[0].parent
        self.assertIsNotNone(parent)
        self.assertEqual(f"{parent.trace_id:032x}",
                         "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(f"{parent.span_id:016x}", "00f067aa0ba902b7")

    def test_otlp_http_round_trip_to_stub_collector(self):
        bodies = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers["Content-Length"])
                bodies.append(self.rfile.read(n))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertTrue(tracing.configure(
                endpoint=f"http://127.0.0.1:{srv.server_port}/v1/traces"))
            client, key = _client()
            r = client.post("/v1/check",
                            headers={"Authorization": f"Bearer {key}"},
                            json={"trace": {"request": "hi", "tool": "search",
                                            "args": {}}})
            self.assertEqual(r.status_code, 200, r.text)
            from opentelemetry import trace as _t
            from opentelemetry.sdk import trace as _s
            provider = _t.get_tracer_provider()
            assert isinstance(provider, _s.TracerProvider)
            provider.force_flush(timeout_millis=10000)
        finally:
            srv.shutdown()
        self.assertTrue(bodies, "collector saw no OTLP export")
        self.assertTrue(any(b"agentcheck.check" in b for b in bodies),
                        "span name missing from OTLP payload")

    def test_dead_collector_never_breaks_checks(self):
        # exporter points at a closed port; the check must still succeed
        self.assertTrue(tracing.configure(
            endpoint="http://127.0.0.1:9/v1/traces"))
        client, key = _client()
        r = client.post("/v1/check", headers={"Authorization": f"Bearer {key}"},
                        json={"trace": {"request": "hi", "tool": "search",
                                        "args": {}}})
        self.assertEqual(r.status_code, 200, r.text)

    def test_helpers_tolerate_none(self):
        tracing.set_attrs(None, {"a": 1})
        tracing.end(None)
        tracing.set_attrs(None, None)


if __name__ == "__main__":
    unittest.main()
