"""Tests for the Trust Score (Build 1 of the sellability plan)."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcheck.trust import (abstention_rate, concentration, human_agreement,
                              render_badge, stability, trust_score,
                              dataset_report_from_calibration)


class TestConcentration(unittest.TestCase):
    def test_degenerate_confidences_score_zero(self):
        # The stub judge says 0.5 or 0.8 for everything: no discrimination.
        self.assertEqual(concentration([0.9] * 20), 0.0)

    def test_full_spread_scores_one(self):
        self.assertAlmostEqual(concentration([i / 9 for i in range(10)] * 2), 1.0)

    def test_empty_is_zero(self):
        self.assertEqual(concentration([]), 0.0)


class TestStability(unittest.TestCase):
    def test_constant_mix_is_stable(self):
        rows = [{"ts": t, "trace_verdict": "pass"} for t in range(10)]
        self.assertEqual(stability(rows), 1.0)

    def test_flipped_mix_is_unstable(self):
        rows = ([{"ts": t, "trace_verdict": "pass"} for t in range(10)] +
                [{"ts": t + 100, "trace_verdict": "fail"} for t in range(10)])
        self.assertEqual(stability(rows), 0.0)

    def test_too_few_rows_is_none(self):
        self.assertIsNone(stability([{"ts": 1, "trace_verdict": "pass"}] * 3))


class TestHumanAgreement(unittest.TestCase):
    def test_real_signout_vocabulary(self):
        rows = [{"assessment": "looks_correct"},
                {"assessment": "looks_correct"},
                {"assessment": "actual_issue"},
                {"assessment": "insufficient_context"},
                {"assessment": None}]
        self.assertAlmostEqual(human_agreement(rows), 2 / 3)

    def test_no_signal_returns_none(self):
        self.assertIsNone(human_agreement([{"assessment": "insufficient_context"}]))
        self.assertIsNone(human_agreement([{"assessment": None}]))
        self.assertIsNone(human_agreement([]))

    def test_all_overruled_is_zero(self):
        self.assertEqual(human_agreement([{"assessment": "actual_issue"}] * 4), 0.0)


class TestAbstention(unittest.TestCase):
    def test_gate_splits(self):
        rows = [{"confidence": 0.9}, {"confidence": 0.4}, {"confidence": 0.7}]
        self.assertEqual(abstention_rate(rows, gate=0.6), 1 / 3)

    def test_empty_is_none(self):
        self.assertIsNone(abstention_rate([]))


class TestTrustScore(unittest.TestCase):
    def real_like(self):
        # Interleaved so the verdict mix is stable across the window;
        # confidences still span the range (0.42..0.93).
        rows = []
        for i in range(40):
            v = ("pass", "fail", "review", "pass")[i % 4]
            c = (0.93, 0.71, 0.42, 0.88)[i % 4]
            rows.append({"ts": i, "trace_verdict": v, "confidence": c})
        rows[0]["assessment"] = "looks_correct"
        rows[1]["assessment"] = "looks_correct"
        rows[2]["assessment"] = "actual_issue"
        return rows

    def test_discriminates_degenerate_from_real(self):
        stub = [{"ts": i, "trace_verdict": "pass", "confidence": 0.5}
                for i in range(30)]
        real = self.real_like()
        s_stub = trust_score(stub)
        s_real = trust_score(real)
        self.assertGreater(s_real["score"], s_stub["score"] + 20)
        self.assertEqual(s_stub["verdict"], "low-trust")

    def test_thin_data_is_insufficient(self):
        self.assertEqual(trust_score(self.real_like()[:5])["verdict"],
                         "insufficient-data")

    def test_measured_tier_needs_30_decided(self):
        rows = self.real_like()
        rep = {"ece": 0.04, "accuracy": 0.91, "decided": 29}
        self.assertEqual(trust_score(rows, dataset_report=rep)["tier"],
                         "consistency")
        rep["decided"] = 31
        self.assertEqual(trust_score(rows, dataset_report=rep)["tier"],
                         "measured")

    def test_adapter_reads_the_real_calibration_shape(self):
        """calibration.calibration nests the numbers under 'decided' and is
        the only producer. The tier test above hand-builds the flat shape, so
        before the adapter nothing connected the two: the measured tier passed
        its unit test and was unreachable from the API."""
        real = {"judge": "typesafe", "checkset": "safety",
                "dataset": "agent-demo",
                "decided": {"n": 40, "ece": 0.04, "accuracy": 0.91}}
        rep = dataset_report_from_calibration(real)
        self.assertEqual(rep["ece"], 0.04)
        self.assertEqual(rep["accuracy"], 0.91)
        self.assertEqual(rep["decided"], 40)
        self.assertEqual(rep["checkset"], "safety")
        self.assertEqual(rep["dataset"], "agent-demo")
        # And it flows through: the same rows upgrade with the adapted report.
        self.assertEqual(
            trust_score(self.real_like(), dataset_report=rep)["tier"], "measured")

    def test_adapter_refuses_a_thin_or_failed_calibration(self):
        """A calibration with no decided numbers must not upgrade the tier."""
        self.assertIsNone(dataset_report_from_calibration(None))
        self.assertIsNone(dataset_report_from_calibration({}))
        self.assertIsNone(dataset_report_from_calibration(
            {"decided": {"n": 0, "ece": None, "accuracy": None}}))

    def test_adversarial_absent_by_default(self):
        d = trust_score(self.real_like())
        self.assertIsNone(d["adversarial"])

    def test_adversarial_drags_and_lifts_honestly(self):
        rows = self.real_like()
        base = trust_score(rows)["score"]
        bad = trust_score(rows, adversarial={"asr": 0.6, "n": 103,
                                             "evaded": 62, "high_conf_evaded": 20})
        good = trust_score(rows, adversarial={"asr": 0.02, "n": 103,
                                              "evaded": 2, "high_conf_evaded": 0})
        self.assertLess(bad["score"], base)
        self.assertGreater(good["score"], base)
        self.assertEqual(bad["adversarial"]["asr"], 0.6)
        self.assertEqual(good["adversarial"]["high_conf_evaded"], 0)

    def test_score_components_are_individually_readable(self):
        d = trust_score(self.real_like())
        for k in ("concentration", "stability", "human_agreement",
                  "abstention_rate"):
            self.assertIn(k, d["components"])
        self.assertIn(d["score"], range(0, 101))
        self.assertIn("n", d)


class TestBadge(unittest.TestCase):
    def test_badge_renders_texts(self):
        import re
        ts = trust_score([{"ts": i, "trace_verdict": "pass",
                           "confidence": 0.93} for i in range(40)])
        svg = render_badge(ts)
        self.assertTrue(svg.startswith("<svg"))
        texts = re.findall(r">([^<]+)</text>", svg)
        self.assertEqual(len(texts), 2)
        self.assertIn("/100", texts[1])
        self.assertIn("n=", texts[1])

    def test_badge_escapes(self):
        import re
        ts = {"verdict": "low-trust", "score": 10, "n": 5, "tier": "consistency"}
        svg = render_badge(ts)
        self.assertNotIn("<script", svg)


if __name__ == "__main__":
    unittest.main()
