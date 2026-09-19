"""Tests for Decision Policies (Build 2)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcheck.policies import (Policy, PolicyError, lint, load_policy_file,
                                 simulate, policy_dirs)


SPEC = {
    "name": "t",
    "rules": [
        {"if": "verdict == fail", "then": "block"},
        {"if": "verdict == review", "then": "human"},
        {"if": "confidence < 0.6", "then": "human"},
        {"if": "severity >= 2", "then": "human"},
    ],
    "default": "approve",
}


class TestParse(unittest.TestCase):
    def test_all_ops(self):
        p = Policy(SPEC)
        self.assertEqual(p.decide("fail", 0.9, 1), "block")
        self.assertEqual(p.decide("review", 0.9, 1), "human")
        self.assertEqual(p.decide("pass", 0.4, 1), "human")
        self.assertEqual(p.decide("pass", 0.9, 3), "human")
        self.assertEqual(p.decide("pass", 0.9, 1), "approve")

    def test_missing_fields_fall_through(self):
        p = Policy(SPEC)
        # no confidence/severity: conf and sev rules can't match
        self.assertEqual(p.decide("pass", None, None), "approve")
        self.assertEqual(p.decide(None, 0.9, None), "approve")

    def test_default_required(self):
        with self.assertRaises(PolicyError):
            Policy({"name": "x", "rules": SPEC["rules"]})

    def test_bad_then(self):
        with self.assertRaises(PolicyError):
            Policy({**SPEC, "default": "maybe"})

    def test_bad_condition(self):
        with self.assertRaises(PolicyError):
            Policy({**SPEC, "rules": [{"if": "verdict = fail", "then": "block"}],
                    "default": "approve"})

    def test_verdict_range_ops_rejected(self):
        with self.assertRaises(PolicyError):
            Policy({**SPEC, "rules": [{"if": "verdict > fail", "then": "block"}],
                    "default": "approve"})

    def test_bad_name(self):
        with self.assertRaises(PolicyError):
            Policy({**SPEC, "name": "9bad"})

    def test_empty_rules(self):
        with self.assertRaises(PolicyError):
            Policy({"name": "x", "rules": [], "default": "approve"})


class TestLint(unittest.TestCase):
    def test_good(self):
        self.assertEqual(lint(SPEC), [])

    def test_bad(self):
        self.assertTrue(lint({**SPEC, "default": "x"}))


class TestFiles(unittest.TestCase):
    def test_shipped_policies_load(self):
        root = Path(__file__).resolve().parent.parent
        for f in (root / "agentcheck" / "policy_examples").glob("*.yaml"):
            spec = load_policy_file(f)
            self.assertIn(spec["name"], ("demo-strict", "refund-safety"))

    def test_policy_dirs_includes_cwd(self):
        d = policy_dirs()
        self.assertTrue(any(d.endswith("policies") for d in d if d))


class TestSimulate(unittest.TestCase):
    def test_split_and_counts(self):
        p = Policy(SPEC)
        items = ([{"verdict": "fail", "confidence": 0.9, "severity": 1}] * 5 +
                 [{"verdict": "pass", "confidence": 0.9, "severity": 1}] * 3 +
                 [{"verdict": "pass", "confidence": 0.4, "severity": 1}] * 2)
        r = simulate(p, items)
        self.assertEqual(r["n"], 10)
        self.assertEqual(r["block"], 5)
        self.assertEqual(r["human"], 2)
        self.assertEqual(r["approve"], 3)
        self.assertAlmostEqual(r["block_pct"], 50.0)

    def test_empty_items(self):
        r = simulate(Policy(SPEC), [])
        self.assertEqual(r["n"], 0)
        self.assertEqual(r["approve_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
