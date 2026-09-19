"""Tests for the two behaviors that protect the product:
  1. Choice/Score criteria normalisation (would 422 if wrong)
  2. Quota guard blocks BEFORE forwarding (protects the pilot)
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentcheck.judges.base import Question, choice, noul, score  # noqa: E402
from agentcheck.store import Store  # noqa: E402


def test_choice_criteria_is_map_score_is_list():
    """The wire format is inconsistent; Question.to_payload must hide that."""
    c = choice("dept", "which team?", {"a": "desc a", "b": "desc b"})
    s = score("sev", "how bad?", ["low", "mid", "high"])
    n = noul("ok", "is it fine?")

    assert c.to_payload()["criteria"] == {"a": "desc a", "b": "desc b"}, \
        "Choice.criteria must serialise to a MAP"
    assert s.to_payload()["criteria"] == ["low", "mid", "high"], \
        "Score.criteria must serialise to a LIST (422 if a map)"
    assert "criteria" not in n.to_payload(), "Noul takes no criteria"


def test_quota_guard_blocks_before_forwarding():
    """The guard must reject an over-quota call without ever calling the judge.
    A leaked call here is a real bill (or a revoked pilot)."""
    tmp = tempfile.mkdtemp()
    store = Store(Path(tmp) / "t.db")
    k = store.create_key("u", qpm_limit=8)
    k = store.lookup_key(k)["kid"]

    # simulate one forwarded call: 5 questions logged as consumed
    store.record(user_key=k, judge="typesafe", model="jev", request_id="req_x",
                 input_tokens=10, output_tokens=2, questions=5, server_ms=90.0,
                 cached=0, ok=1)

    from agentcheck.limits import QuotaGuard
    guard = QuotaGuard(store, window=60.0)
    assert guard.allow(k, 3) is True, "5+3 of 8 should still fit"
    assert guard.allow(k, 4) is False, "5+4 exceeds 8 — must block before forwarding"


def test_cache_key_is_content_addressed():
    from agentcheck.limits import AnswerCache
    cache = AnswerCache()
    state = {"tool": "x", "args": {"a": 1}}
    qs = [noul("q", "is it fine?")]
    assert cache.get(state, qs) is None
    cache.put(state, qs, ["stub"], "jev-test")
    answers, model = cache.get(state, qs)
    assert answers == ["stub"]
    assert model == "jev-test", "cache must remember the real model, not 'cached'"
    # different state must not collide
    assert cache.get({"tool": "other"}, qs) is None


def test_meter_records_request_id_for_reconciliation():
    """request_id is the only key that reconciles your logs to a vendor invoice."""
    tmp = tempfile.mkdtemp()
    store = Store(Path(tmp) / "t.db")
    k = store.create_key("u")
    k = store.lookup_key(k)["kid"]
    store.record(user_key=k, judge="typesafe", model="jev", request_id="req_abc",
                 input_tokens=100, output_tokens=20, questions=5, server_ms=50.0,
                 cached=0, ok=1)
    conn = sqlite3.connect(Path(tmp) / "t.db")
    row = conn.execute("SELECT request_id, input_tokens, output_tokens FROM metering").fetchone()
    conn.close()
    assert row == ("req_abc", 100, 20)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("\nall tests passed")
