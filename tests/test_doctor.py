"""doctor: the checks that catch a silently broken self-host.

Each one has cost real debugging time on this project: a stale process on the
port answering with old code, a missing judge key, an unwritable data dir, a
published calibration that is not valid JSON. The contract pinned here is that
every failure comes with the command that fixes it — a red row with no next
step is a dead end.
"""

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from agentcheck.cli import doctor

ENV_KEYS = ("AGENTCHECK_HOME", "OPENAI_API_KEY", "TYPESAFE_API_KEY",
            "AGENTCHECK_JUDGE", "AGENTCHECK_PORT", "AGENTCHECK_DB_URL")


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["AGENTCHECK_HOME"] = self.home
        for k in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "AGENTCHECK_JUDGE",
                  "AGENTCHECK_DB_URL"):
            os.environ.pop(k, None)
        # A free port, so the port check is deterministic rather than dependent
        # on whatever is running on this machine.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.free_port = s.getsockname()[1]
        s.close()
        os.environ["AGENTCHECK_PORT"] = str(self.free_port)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run(self):
        return CliRunner().invoke(doctor, [])

    def test_all_ok_on_a_clean_machine(self):
        r = self._run()
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("all checks passed", r.output)
        self.assertIn("offline stub", r.output)

    def test_a_busy_port_is_reported_with_the_fix(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        os.environ["AGENTCHECK_PORT"] = str(s.getsockname()[1])
        try:
            r = self._run()
        finally:
            s.close()
        self.assertEqual(r.exit_code, 1, r.output)
        self.assertIn("already in use", r.output)
        self.assertIn("lsof", r.output,
                      "a failure without the next command is a dead end")

    def test_an_unparseable_published_calibration_is_caught(self):
        (Path(self.home) / "calibration.json").write_text("{not json")
        r = self._run()
        self.assertEqual(r.exit_code, 1)
        self.assertIn("not valid JSON", r.output)

    def test_a_small_calibration_is_flagged_but_not_a_failure(self):
        (Path(self.home) / "calibration.json").write_text(
            json.dumps({"decided": {"n": 4}}))
        r = self._run()
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("under 30", r.output)

    def test_a_missing_judge_key_is_named_with_the_env_var(self):
        r = self._run()
        self.assertIn("OPENAI_API_KEY", r.output)

    def test_doctor_never_writes_into_the_data_dir(self):
        before = set(p.name for p in Path(self.home).iterdir())
        self._run()
        after = set(p.name for p in Path(self.home).iterdir())
        self.assertEqual(before, after, "doctor must be read-only apart from its probe")


if __name__ == "__main__":
    unittest.main()
