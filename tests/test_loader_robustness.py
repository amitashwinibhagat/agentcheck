"""Loader robustness: one bad file must never hide the rest.

The regression these pin down: a macOS AppleDouble file (`._foo.yaml`) landed
beside `foo.yaml` inside a deployed image. It matches the `*.yaml` glob but is
binary, so `read_text()` raised UnicodeDecodeError. `discover()` caught only
`RubricError`, so the exception escaped and `/v1/checksets` returned **500** —
which is why the Rubrics view said "No rubrics available." on a live instance
while the CLI happily listed eight.

Two independent protections are asserted here:

  1. dotfiles are metadata, never rubrics or policies, and are skipped
  2. anything unreadable is a reported problem, not a crash
"""

import tempfile
import unittest
from pathlib import Path

from agentcheck.checks import yaml_checksets as yc
from agentcheck import policies

GOOD_RUBRIC = """name: audit-thing
checks:
  - id: verdict
    type: choice
    instructions: Is it fine?
    criteria:
      pass: fine
      fail: not fine
"""

GOOD_POLICY = """name: audit-policy
rules:
  - if: "verdict == fail"
    then: block
default: approve
"""

# what an AppleDouble file actually looks like: a binary header, not text
BINARY = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X\x00\x00\x00\x02\x00\x00\xa3\xb2"


def _dir():
    return Path(tempfile.mkdtemp())


class TestRubricDiscovery(unittest.TestCase):
    def test_binary_dotfile_is_skipped_silently(self):
        d = _dir()
        (d / "audit-thing.yaml").write_text(GOOD_RUBRIC)
        (d / "._audit-thing.yaml").write_bytes(BINARY)
        found, problems = yc.discover_in([d])
        self.assertIn("audit-thing", found)
        self.assertEqual(problems, [], problems)

    def test_binary_non_dotfile_is_reported_not_fatal(self):
        # a genuinely broken file the user dropped in: we lose that one rubric
        # and say so, but every other rubric still loads
        d = _dir()
        (d / "audit-thing.yaml").write_text(GOOD_RUBRIC)
        (d / "broken.yaml").write_bytes(BINARY)
        found, problems = yc.discover_in([d])
        self.assertIn("audit-thing", found)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("broken.yaml", problems[0])

    def test_invalid_yaml_is_reported(self):
        d = _dir()
        (d / "audit-thing.yaml").write_text(GOOD_RUBRIC)
        (d / "half.yaml").write_text("name: [unclosed\n")
        found, problems = yc.discover_in([d])
        self.assertIn("audit-thing", found)
        self.assertEqual(len(problems), 1, problems)

    def test_duplicate_names_keep_the_first_and_report(self):
        d = _dir()
        (d / "a.yaml").write_text(GOOD_RUBRIC)
        (d / "b.yaml").write_text(GOOD_RUBRIC)
        found, problems = yc.discover_in([d])
        self.assertEqual(list(found), ["audit-thing"])
        self.assertEqual(len(problems), 1)
        self.assertIn("duplicate", problems[0])

    def test_builtin_name_is_not_overridden(self):
        d = _dir()
        (d / "safety.yaml").write_text("name: safety\nchecks:\n"
                                       "  - id: v\n    type: choice\n"
                                       "    instructions: x\n"
                                       "    criteria: {a: b}\n")
        found, problems = yc.discover_in([d], builtins={"safety"})
        self.assertNotIn("safety", found)
        self.assertTrue(any("built-in" in p for p in problems), problems)


class TestPolicyDiscovery(unittest.TestCase):
    def test_lists_policies_from_a_directory(self):
        d = _dir()
        (d / "audit-policy.yaml").write_text(GOOD_POLICY)
        got = policies.all_policies_in([d])
        self.assertIn("audit-policy", got)
        self.assertTrue(got["audit-policy"]["ok"])

    def test_binary_dotfile_is_skipped(self):
        d = _dir()
        (d / "audit-policy.yaml").write_text(GOOD_POLICY)
        (d / "._audit-policy.yaml").write_bytes(BINARY)
        got = policies.all_policies_in([d])
        self.assertEqual(sorted(got), ["audit-policy"], sorted(got))

    def test_unreadable_policy_is_an_error_entry_not_a_crash(self):
        d = _dir()
        (d / "audit-policy.yaml").write_text(GOOD_POLICY)
        (d / "broken.yaml").write_bytes(BINARY)
        got = policies.all_policies_in([d])
        self.assertTrue(got["audit-policy"]["ok"])
        self.assertFalse(got["broken"]["ok"])
        self.assertIn("cannot read", got["broken"]["error"])


class TestTheRealThing(unittest.TestCase):
    """The shipped bundle must always load, from any directory."""

    def test_shipped_rubrics_all_load(self):
        found, problems = yc.discover_in([yc.SHIPPED_DIR])
        self.assertGreaterEqual(len(found), 7, sorted(found))
        self.assertEqual(problems, [], problems)

    def test_shipped_policies_all_load(self):
        got = policies.all_policies_in([policies.SHIPPED_DIR])
        self.assertGreaterEqual(len(got), 2, sorted(got))
        for name, entry in got.items():
            self.assertTrue(entry["ok"], f"{name}: {entry.get('error')}")


if __name__ == "__main__":
    unittest.main()
