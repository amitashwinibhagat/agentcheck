"""Contract tests for the LangChain / Anthropic / LlamaIndex adapters.
No framework SDK is ever imported: fakes shaped like each one verify the
shared core contract (check, chain, block, never break the run)."""

import json
import unittest

from agentcheck.integrations._core import CheckSession
from agentcheck.integrations.anthropic import watch as watch_anthropic
from agentcheck.integrations.langchain import (
    AgentCheckBlocked as LCBlocked, AgentCheckCallbackHandler)
from agentcheck.integrations.llama_index import AgentCheckHandler


def _checker(log, decisions=None):
    def checker(trace):
        log.append(trace)
        d = (decisions or {}).get(trace["tool"], "approve")
        return {"trace_verdict": "fail" if d == "block" else "pass",
                "confidence": 0.9, "decision": d,
                "trace_id": "tr_s", "span_id": f"sp_{len(log)}"}
    return checker


class TestCore(unittest.TestCase):
    def test_spans_chain(self):
        log = []
        s = CheckSession(checker=_checker(log))
        a = s.check("t1", {}, "req")
        b = s.check("t2", {}, "req")
        self.assertEqual(a["trace_id"], "tr_s")
        self.assertEqual(s.parent_span_id, "sp_2")
        self.assertEqual(len(log), 2)

    def test_checker_failure_is_error_entry(self):
        def boom(trace):
            raise RuntimeError("down")

        s = CheckSession(checker=boom)
        self.assertEqual(s.check("t", {}, "")["error"], "down")

    def test_non_dict_checker_is_error_entry(self):
        s = CheckSession(checker=lambda trace: [1, 2])
        self.assertIn("error", s.check("t", {}, ""))

    def test_missing_key_is_error_entry(self):
        s = CheckSession(key=None)
        self.assertIn("no key", s.check("t", {}, "")["error"])

    def test_non_dict_args_wrapped(self):
        log = []
        s = CheckSession(checker=_checker(log))
        s.check("t", "raw string", "")
        self.assertEqual(log[0]["args"], {"_raw": "raw string"})


class TestLangChain(unittest.TestCase):
    def test_tool_start_checks_with_metadata_context(self):
        log = []
        h = AgentCheckCallbackHandler(checker=_checker(log))
        h.on_tool_start({"name": "send_email"}, '{"to": "x"}',
                        run_id="r1",
                        metadata={"agentcheck_request": "email priya",
                                  "agentcheck_trace_id": "tr_lc"})
        self.assertEqual(log[0]["tool"], "send_email")
        self.assertEqual(log[0]["args"], {"to": "x"})
        self.assertEqual(log[0]["request"], "email priya")
        self.assertEqual(h.checks[0]["trace_id"], "tr_s")
        self.assertEqual(h.checks[0]["run_id"], "r1")

    def test_input_falls_back_to_request(self):
        log = []
        h = AgentCheckCallbackHandler(checker=_checker(log))
        h.on_tool_start({"name": "search"}, "find cats", run_id="r1")
        self.assertEqual(log[0]["request"], "find cats")

    def test_block_raises_otherwise_records(self):
        h = AgentCheckCallbackHandler(
            checker=_checker([], {"wire_transfer": "block"}), block=True)
        with self.assertRaises(LCBlocked):
            h.on_tool_start({"name": "wire_transfer"}, "{}", run_id="r1")
        h2 = AgentCheckCallbackHandler(
            checker=_checker([], {"wire_transfer": "block"}))
        h2.on_tool_start({"name": "wire_transfer"}, "{}", run_id="r1")
        self.assertEqual(h2.checks[0]["decision"], "block")

    def test_end_attaches_outcome_unknown_run_tolerated(self):
        h = AgentCheckCallbackHandler(checker=_checker([]))
        h.on_tool_start({"name": "search"}, "{}", run_id="r1")
        h.on_tool_end("42 results", run_id="r1")
        self.assertEqual(h.checks[0]["output_preview"], "42 results")
        h.on_tool_end("x", run_id="nope")  # must not raise
        h.on_tool_error(ValueError("bad"), run_id="nope")

    def test_broken_checker_never_breaks_callback(self):
        def boom(trace):
            raise RuntimeError("down")

        h = AgentCheckCallbackHandler(checker=boom)
        h.on_tool_start({"name": "search"}, "{}", run_id="r1")
        self.assertIn("error", h.checks[0])


class _ABlock:
    def __init__(self, btype, **kw):
        self.type = btype
        for k, v in kw.items():
            setattr(self, k, v)


class _AResp:
    def __init__(self, blocks):
        self.content = blocks


class _AMessages:
    def __init__(self, resp):
        self.resp = resp

    def create(self, *args, **kwargs):
        return self.resp


class _AClient:
    def __init__(self, resp):
        self.messages = _AMessages(resp)


class TestAnthropic(unittest.TestCase):
    def test_tool_use_checked_text_skipped(self):
        log = []
        resp = _AResp([_ABlock("text", text="thinking"),
                       _ABlock("tool_use", id="t1", name="refund",
                               input={"order": "9"})])
        c = _AClient(resp)
        watch_anthropic(c, checker=_checker(log))
        out = c.messages.create(
            messages=[{"role": "user", "content": "refund me"}])
        self.assertIs(out, resp)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["tool"], "refund")
        self.assertEqual(log[0]["request"], "refund me")

    def test_block_raises(self):
        from agentcheck.integrations.openai import AgentCheckBlocked
        resp = _AResp([_ABlock("tool_use", id="t1", name="wire_transfer",
                               input={})])
        c = _AClient(resp)
        watch_anthropic(c, checker=_checker([], {"wire_transfer": "block"}),
                        block=True)
        with self.assertRaises(AgentCheckBlocked):
            c.messages.create(messages=[])


class TestLlamaIndex(unittest.TestCase):
    def test_function_event_checked(self):
        log = []
        h = AgentCheckHandler(checker=_checker(log))
        h.on_event_start("FUNCTION_CALL",
                         payload={"function_name": "search",
                                  "function_args": {"q": "x"}},
                         event_id="e1")
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["tool"], "search")
        self.assertEqual(h.checks[0]["event_id"], "e1")

    def test_other_events_ignored(self):
        log = []
        h = AgentCheckHandler(checker=_checker(log))
        h.on_event_start("LLM", payload={"x": 1}, event_id="e1")
        self.assertEqual(log, [])

    def test_undeterminable_tool_skipped_not_invented(self):
        log = []
        h = AgentCheckHandler(checker=_checker(log))
        h.on_event_start("TOOL_CALL", payload={"fuzzy": "???"},
                         event_id="e1")
        self.assertEqual(log, [])
        self.assertEqual(h.checks, [])

    def test_block_raises(self):
        from agentcheck.integrations.llama_index import AgentCheckBlocked
        h = AgentCheckHandler(checker=_checker([], {"rm": "block"}),
                              block=True)
        with self.assertRaises(AgentCheckBlocked):
            h.on_event_start("FUNCTION_CALL",
                             payload={"function_name": "rm"},
                             event_id="e1")


if __name__ == "__main__":
    unittest.main()
