"""Tests for history importers. The langfuse/langsmith SDKs are never
imported: fakes shaped like them verify mapping, gold honesty, and the
refusal to invent verdicts."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcheck.integrations import import_common as ic
from agentcheck.integrations import langfuse as lf
from agentcheck.integrations import langsmith as ls


class TestGoldMapping(unittest.TestCase):
    def test_clear_values_map(self):
        self.assertEqual(ic.gold_from_score(1), "pass")
        self.assertEqual(ic.gold_from_score(0), "fail")
        self.assertEqual(ic.gold_from_score(True), "pass")
        self.assertEqual(ic.gold_from_score(False), "fail")
        self.assertEqual(ic.gold_from_score("approve"), "pass")
        self.assertEqual(ic.gold_from_score("reject"), "fail")

    def test_opinions_abstain(self):
        # a 0.7, a maybe, a 5-star scale: none of these is a verdict
        self.assertIsNone(ic.gold_from_score(0.7))
        self.assertIsNone(ic.gold_from_score("maybe"))
        self.assertIsNone(ic.gold_from_score(None))
        # integers read binary-ish (documented scale assumption)
        self.assertEqual(ic.gold_from_score(4), "pass")

    def test_record_without_gold_has_no_gold_keys(self):
        r = ic.make_record({"request": "x", "tool": "t", "args": {}})
        self.assertTrue(r["needs_label"])
        self.assertNotIn("label_verdict", r)
        self.assertNotIn("should_fail", r)

    def test_record_with_gold(self):
        r = ic.make_record({"request": "x", "tool": "t", "args": {}},
                           gold="fail", provenance={"source": "s"})
        self.assertEqual(r["label_verdict"], "fail")
        self.assertTrue(r["should_fail"])
        self.assertNotIn("needs_label", r)


class _LFObs(dict):
    pass


class _FakeLangfuse:
    def __init__(self, traces, obs, scores):
        self.traces = traces
        self.obs = obs
        self.scores = scores

    def fetch_traces(self, limit=100):
        return self.traces[:limit]

    def fetch_observations(self, trace_id):
        return self.obs.get(trace_id, [])

    def fetch_scores(self, trace_id, name):
        return [s for s in self.scores.get(trace_id, [])
                if s["name"] == name]


class TestLangfuse(unittest.TestCase):
    def _client(self):
        traces = [{"id": "t1",
                   "input": [{"role": "user", "content": "refund me"}]}]
        obs = {"t1": [
            {"id": "o1", "type": "TOOL", "name": "refund",
             "input": {"order": "1042"}},
            {"id": "o2", "type": "GENERATION", "name": "answer",
             "input": "here you go"},  # not a tool call: skipped
        ]}
        scores = {"t1": [{"name": "correctness", "value": 0}]}
        return _FakeLangfuse(traces, obs, scores)

    def test_tool_obs_imported_generation_skipped(self):
        recs = lf.fetch(self._client(), gold_score="correctness")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["trace"]["tool"], "refund")
        self.assertEqual(recs[0]["trace"]["request"], "refund me")
        self.assertEqual(recs[0]["label_verdict"], "fail")
        self.assertEqual(recs[0]["provenance"]["source"], "langfuse")

    def test_no_gold_score_means_unlabeled(self):
        recs = lf.fetch(self._client())
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["needs_label"])
        self.assertNotIn("label_verdict", recs[0])

    def test_other_named_scores_ignored(self):
        recs = lf.fetch(self._client(), gold_score="latency_ok")
        self.assertTrue(recs[0]["needs_label"])


class _FakeLangSmith:
    def __init__(self, runs, feedback):
        self.runs = runs
        self.feedback = feedback

    def list_runs(self, limit=100, run_type="tool"):
        return [r for r in self.runs
                if r.get("run_type", "tool") == run_type][:limit]

    def list_feedback(self, run_ids, key):
        return [f for f in self.feedback
                if f["run_id"] in run_ids and f["key"] == key]

    def get_run(self, rid):
        return next(r for r in self.runs if r["id"] == rid)


class TestLangSmith(unittest.TestCase):
    def _client(self):
        runs = [
            {"id": "r1", "run_type": "tool", "name": "send_email",
             "inputs": {"to": "x"},
             "parent_run_ids": ["p1"]},
            {"id": "p1", "run_type": "chain", "name": "agent",
             "inputs": {"input": "email the team"}, "parent_run_ids": []},
            {"id": "r2", "run_type": "llm", "name": "chat",
             "inputs": {}},  # not a tool run: skipped
        ]
        feedback = [{"run_id": "r1", "key": "approval", "score": 1}]
        return _FakeLangSmith(runs, feedback)

    def test_tool_run_with_parent_request_and_gold(self):
        recs = ls.fetch(self._client(), gold_score="approval")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["trace"]["tool"], "send_email")
        self.assertEqual(recs[0]["trace"]["request"], "email the team")
        self.assertEqual(recs[0]["label_verdict"], "pass")

    def test_no_feedback_means_unlabeled(self):
        recs = ls.fetch(self._client())
        self.assertTrue(recs[0]["needs_label"])


class TestNoPhantomGold(unittest.TestCase):
    def test_calibration_refuses_unlabeled(self):
        from agentcheck import calibration as cal
        from agentcheck.judges.stub import StubJudge
        recs = [ic.make_record({"request": "x", "tool": "t", "args": {}})]
        cs_name, vid, usable, errors = cal.predict_dataset(
            StubJudge(), recs, checkset="safety")
        self.assertEqual(usable, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("no gold verdict", errors[0])

    def test_import_round_trips_through_dataset_files(self):
        from agentcheck import labels as labels_mod
        import tempfile
        recs = [ic.make_record({"request": "x", "tool": "t", "args": {}},
                               gold="pass",
                               provenance={"source": "langfuse"})]
        p = labels_mod.save_dataset(
            recs, Path(tempfile.mkdtemp()) / "imp.json")
        back = labels_mod.load_dataset(str(p))
        self.assertEqual(back[0]["label_verdict"], "pass")
        self.assertEqual(back[0]["provenance"]["source"], "langfuse")


if __name__ == "__main__":
    unittest.main()
