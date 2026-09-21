"""The labeling loop: sign-out labels become a real calibration.

The claim this file pins: a user's own sign-outs are enough to measure the
judge — no dataset file, no second judge pass — and the resulting report is
honest about what it is (and refuses to exist when there is nothing to
measure against).

  * `looks_correct` means the judge was right, `actual_issue` means it was
    wrong, `insufficient_context` carries no signal (dropped, never counted)
  * the gate decides abstention, exactly as in a live predict pass
  * a report over too few decided items still reports its numbers but cannot
    upgrade the trust tier (trust.trust_score enforces the 30-item floor)
  * publishing with zero labels is refused rather than writing a fabricated
    calibration
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from agentcheck import calibration as cal
from agentcheck.store import Store

SECRET = "test-secret-please-rotate"


def _client():
    from fastapi.testclient import TestClient
    from agentcheck.proxy import create_app
    store = Store(Path(tempfile.mkdtemp()) / "signoff.db")
    key = store.create_key("acct", qpm_limit=60000)
    app = create_app(store, default_judge="stub")
    return TestClient(app, base_url="https://app.test"), key, store


def _rows(spec):
    """spec: list of (assessment, confidence) from a signed-out judgment."""
    return [{"assessment": a, "confidence": c, "verdict": "fail" if c > 0.8 else "pass"}
            for a, c in spec]


class TestReportFromSignoffs(unittest.TestCase):
    def test_labels_map_to_truth_and_gate_to_abstention(self):
        # 3 right, 1 wrong, all high confidence -> ECE is large and positive
        # (over-confident), accuracy 0.75. Four items is under the 30 the tier
        # needs, so the read stays insufficient-data: honest, not flattering.
        rows = _rows([("looks_correct", 0.9)] * 3 + [("actual_issue", 0.9)])
        rep = cal.report_from_signoffs(rows, "stub", gate=0.6)
        self.assertEqual(rep["decided"]["n"], 4)
        self.assertAlmostEqual(rep["decided"]["accuracy"], 0.75, places=3)
        self.assertAlmostEqual(rep["decided"]["mean_confidence"], 0.9, places=3)
        self.assertGreater(rep["decided"]["ece"], 0.1)
        self.assertEqual(rep["source"], "signoffs")
        self.assertEqual(rep["read"], "insufficient-data")

    def test_an_overconfident_judge_is_miscalibrated_at_volume(self):
        # 32 decided items, right 50% of the time while claiming 0.9.
        rows = _rows([("looks_correct", 0.9)] * 16 + [("actual_issue", 0.9)] * 16)
        rep = cal.report_from_signoffs(rows, "stub", gate=0.6)
        self.assertEqual(rep["decided"]["n"], 32)
        self.assertAlmostEqual(rep["decided"]["accuracy"], 0.5, places=3)
        self.assertEqual(rep["read"], "miscalibrated")

    def test_insufficient_context_is_not_a_label(self):
        rows = _rows([("insufficient_context", 0.9), ("insufficient_context", 0.7)])
        rep = cal.report_from_signoffs(rows, "stub", gate=0.6)
        self.assertEqual(rep["decided"]["n"], 0, "no signal must not be counted")
        self.assertIsNone(rep["decided"]["accuracy"])

    def test_low_confidence_signouts_are_abstentions_not_decisions(self):
        rows = _rows([("looks_correct", 0.4), ("looks_correct", 0.9)])
        rep = cal.report_from_signoffs(rows, "stub", gate=0.6)
        self.assertEqual(rep["decided"]["n"], 1)
        self.assertEqual(rep["abstained"]["n"], 1)

    def test_a_perfectly_calibrated_judge_reads_well_calibrated(self):
        # Right 80% of the time while claiming 0.8: that IS calibration, and the
        # ECE should be ~0. (Getting it backwards -- right 50% at 0.8 -- is
        # 0.3 of error, which the test above pins.)
        rows = _rows([("looks_correct", 0.8)] * 8 + [("actual_issue", 0.8)] * 2)
        rep = cal.report_from_signoffs(rows, "stub", gate=0.6)
        self.assertAlmostEqual(rep["decided"]["accuracy"], 0.8, places=3)
        self.assertLess(rep["decided"]["ece"], 0.05)

    def test_nan_confidence_is_dropped_not_trusted(self):
        rows = [{"assessment": "looks_correct", "confidence": float("nan")},
                {"assessment": "looks_correct", "confidence": 0.9}]
        rep = cal.report_from_signoffs(rows, "stub")
        self.assertEqual(rep["n"], 1, "a corrupt confidence is not an item")


class TestStoreCounts(unittest.TestCase):
    def test_counts_and_decided_respect_the_gate(self):
        _, _, store = _client()
        key = store.create_key("t")  # raw token, used as user_key by the API
        kid = store.lookup_key(key)["kid"]
        for i, (a, c) in enumerate([("looks_correct", 0.9), ("actual_issue", 0.9),
                                    ("insufficient_context", 0.9), ("looks_correct", 0.3)]):
            # Distinct args: identical traces dedupe to one row by design, and a
            # test that signs the same call out four times measures nothing.
            rid = store.save_result(kid,
                                    {"request": "x", "tool": "t", "args": {"i": i}},
                                    {"trace_verdict": "pass", "confidence": c,
                                     "severity": 1, "checks": {}},
                                    checkset="safety")
            store.assess(kid, rid if isinstance(rid, str) else rid["id"], a)
        counts = store.signoff_counts(kid, gate=0.6)
        self.assertEqual(counts["total"], 4, "every signed-out row")
        self.assertEqual(counts["signal"], 3, "insufficient_context carries no signal")
        self.assertEqual(counts["decided"], 2, "below-gate sign-outs are not decided")
        self.assertEqual(len(store.signoffs(kid)), 4)
        # None means "every key in this store" — the local CLI path.
        self.assertGreaterEqual(len(store.signoffs(None)), 3)


class TestPublishEndpoint(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTCHECK_SECRET"] = SECRET
        self.home = tempfile.mkdtemp()
        os.environ["AGENTCHECK_HOME"] = self.home

    def tearDown(self):
        for k in ("AGENTCHECK_SECRET", "AGENTCHECK_HOME"):
            os.environ.pop(k, None)

    def _sign_out(self, client, key, n, assessment="looks_correct"):
        ids = []
        for i in range(n):
            r = client.post("/v1/check", json={"trace": {
                "request": f"read file {i}", "tool": "read_file",
                "args": {"path": f"/tmp/{i}"}}},
                headers={"Authorization": f"Bearer {key}"})
            ids.append(r.json()["id"])
        for rid in ids:
            client.patch(f"/v1/results/{rid}", json={"assessment": assessment},
                         headers={"Authorization": f"Bearer {key}"})
        return ids

    def test_publishing_nothing_is_refused(self):
        client, key, _ = _client()
        r = client.post("/v1/calibration/signoffs/publish",
                        json={}, headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("no decided sign-outs", r.text)
        self.assertEqual([f for f in Path(self.home).iterdir()
                          if f.name.startswith("calibration")], [],
                         "a refusal must not write a calibration file")

    def test_publish_writes_the_model_and_moves_the_tier(self):
        client, key, _ = _client()
        h = {"Authorization": f"Bearer {key}"}
        self._sign_out(client, key, 32)
        before = client.get("/v1/trust", headers=h).json()
        self.assertEqual(before["tier"], "consistency")
        self.assertEqual(before["signoffs"]["decided"], 32)
        self.assertEqual(before["signoffs"]["needed"], 30)
        self.assertEqual(before["signoffs"]["remaining"], 0)

        p = client.post("/v1/calibration/signoffs/publish", json={}, headers=h)
        self.assertEqual(p.status_code, 200, p.text)
        body = p.json()
        self.assertEqual(body["tier_now"], "measured")
        # Per WORKSPACE now: one instance-wide file let a publish move every
        # tenant's tier. See tests/test_adversarial.py::TestCalibrationIsPerTenant.
        published = [f for f in Path(self.home).iterdir()
                     if f.name.startswith("calibration-")]
        self.assertEqual(len(published), 1, sorted(p.name for p in Path(self.home).iterdir()))
        after = client.get("/v1/trust", headers=h).json()
        self.assertEqual(after["tier"], "measured")
        self.assertEqual(after["dataset"], "your sign-outs")
        self.assertIsNotNone(after["ece"])

    def test_a_small_report_reports_but_does_not_upgrade(self):
        client, key, _ = _client()
        h = {"Authorization": f"Bearer {key}"}
        self._sign_out(client, key, 4)
        p = client.post("/v1/calibration/signoffs/publish", json={}, headers=h)
        self.assertEqual(p.status_code, 200)
        self.assertEqual(p.json()["tier_now"], "consistency",
                         "under 30 decided items must not buy a measured tier")
        after = client.get("/v1/trust", headers=h).json()
        self.assertEqual(after["tier"], "consistency")


if __name__ == "__main__":
    unittest.main()
