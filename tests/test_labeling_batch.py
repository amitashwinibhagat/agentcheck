"""The labeling batch: a sample you can actually measure with.

Thirty sign-outs is the price of a measured tier, so the batch has to spend
them on information rather than on whatever the log happens to list first.
These tests pin the properties that makes true:

  * every failure is included — a batch with no failures cannot show
    over-confidence
  * passes are spread across confidence bands, not clustered at one value
  * distinct tools are preferred, so 30 labels cover more than one code path
  * already-labeled rows are never in a batch (labeling is a to-do list)
  * the same queue yields the same batch (a plan that reshuffles is unusable)
"""

import unittest

from agentcheck import labeling


def row(i, verdict, conf, tool="sql_execute", assessment=None):
    return {"id": f"r{i}", "trace_verdict": verdict, "confidence": conf,
            "tool": tool, "ts": 1000 + i, "assessment": assessment}


class TestSelection(unittest.TestCase):
    def test_every_failure_is_in_the_batch(self):
        rows = [row(i, "pass", 0.9, "sql_execute") for i in range(40)]
        rows += [row(100 + i, "fail", 0.9, "send_email") for i in range(4)]
        plan = labeling.select_batch(rows, size=30)
        ids = set(plan["ids"])
        self.assertTrue(all(f"r{100 + i}" in ids for i in range(4)),
                        "a failure left out is information thrown away")
        self.assertEqual(plan["coverage"]["verdicts"]["fail"], 4)

    def test_reviews_are_included_too(self):
        rows = [row(i, "pass", 0.7) for i in range(20)]
        rows += [row(50, "review", 0.6), row(51, "review", 0.65)]
        plan = labeling.select_batch(rows, size=10)
        self.assertEqual(plan["coverage"]["verdicts"].get("review"), 2)

    def test_passes_span_confidence_bands(self):
        # 30 passes, all at 0.9, and 10 spread across lower bands.
        rows = [row(i, "pass", 0.95, "sql_execute") for i in range(30)]
        for j, conf in enumerate([0.62, 0.72, 0.82, 0.92] * 3):
            rows.append(row(100 + j, "pass", conf, f"tool{j % 4}"))
        plan = labeling.select_batch(rows, size=30)
        bands = plan["coverage"]["bands"]
        self.assertGreaterEqual(len(bands), 3,
                                f"passes clustered in one band: {bands}")

    def test_distinct_tools_are_preferred(self):
        rows = []
        for t, tool in enumerate(["sql_execute", "shell_exec", "read_file"]):
            rows += [row(t * 100 + i, "pass", 0.9, tool) for i in range(10)]
        plan = labeling.select_batch(rows, size=9)
        self.assertEqual(len(plan["coverage"]["tools"]), 3)
        # Round-robin: the first three picks are three different tools.
        picked = [r for r in rows if r["id"] in plan["ids"][:3]]
        self.assertEqual(len({p["tool"] for p in picked}), 3)

    def test_labeled_rows_are_never_offered(self):
        rows = [row(i, "pass", 0.9, assessment="looks_correct") for i in range(20)]
        rows += [row(100, "fail", 0.9)]
        plan = labeling.select_batch(rows, size=30)
        self.assertEqual(plan["ids"], ["r100"])
        self.assertEqual(plan["unlabeled"], 1)

    def test_deterministic_for_the_same_queue(self):
        rows = [row(i, "pass" if i % 5 else "fail", 0.6 + (i % 4) / 10,
                    f"tool{i % 6}") for i in range(60)]
        self.assertEqual(labeling.select_batch(rows)["ids"],
                         labeling.select_batch(rows)["ids"])

    def test_size_is_respected_and_never_exceeded(self):
        rows = [row(i, "pass", 0.9) for i in range(50)]
        self.assertEqual(len(labeling.select_batch(rows, size=7)["ids"]), 7)
        # Fewer rows than the size: everything eligible is offered once.
        few = [row(i, "pass", 0.9) for i in range(3)]
        self.assertEqual(len(labeling.select_batch(few, size=30)["ids"]), 3)

    def test_store_rows_say_verdict_and_public_rows_say_trace_verdict(self):
        # Both shapes exist in this codebase; reading only one silently made
        # every row "unknown" and disabled the stratification without erroring.
        store_shape = [{"id": "a", "verdict": "fail", "confidence": 0.9,
                        "tool": "t", "ts": 1, "assessment": None}]
        public_shape = [{"id": "b", "trace_verdict": "fail", "confidence": 0.9,
                         "tool": "t", "ts": 1, "assessment": None}]
        self.assertEqual(labeling.select_batch(store_shape)["coverage"]["verdicts"],
                         labeling.select_batch(public_shape)["coverage"]["verdicts"])


if __name__ == "__main__":
    unittest.main()
