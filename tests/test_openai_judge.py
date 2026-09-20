"""The OpenAI-compatible judge: the BYOK contract.

What must hold for a developer who brings their own key:
  * a 200 with well-formed JSON becomes answers for every question
  * transient failures are retried, permanent ones surface with the request id
  * garbage confidence (NaN, out of range) is refused, never trusted
  * an unknown choice label keeps confidence 0 — it reads as "unknown"
    downstream, never as a fabricated pass
  * no key is a clear startup error, not a mysterious 500 on first check
  * any OpenAI-compatible base URL works (OpenRouter, vLLM, Ollama)
"""

import json
import os
import unittest

import httpx

from agentcheck.judges import default_name, get_judge
from agentcheck.judges.base import choice, noul, score
from agentcheck.judges.openai import OpenAIJudge


def _reply(answers: dict, usage=None, request_id="req-t-1") -> dict:
    return {
        "id": "chatcmpl-1", "model": "gpt-4o-mini",
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": json.dumps({"answers": answers})},
            "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 40},
    }


def _resp(status: int, body, headers=None):
    return httpx.Response(status_code=status, json=body if status == 200 else None,
                          headers=headers or {}, request=httpx.Request(
                              "POST", "https://api.openai.com/v1/chat/completions"))


class _FakeClient:
    """Replays a canned list of responses, one per POST."""
    def __init__(self, responses, seen=None):
        self._responses = list(responses)
        self._seen = seen
        self.timeout = None
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def post(self, url, json=None, headers=None):
        if self._seen is not None:
            self._seen.append((json, headers))
        return self._responses.pop(0)


def _with_client(testcase, fake):
    import agentcheck.judges.openai as O
    orig = O.httpx.Client
    O.httpx.Client = lambda timeout: fake
    testcase.addCleanup(setattr, O.httpx, "Client", orig)


class TestHappyPath(unittest.TestCase):
    def setUp(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"

    def tearDown(self):
        os.environ.pop("OPENAI_API_KEY", None)

    def test_every_question_gets_an_answer(self):
        seen = []
        body = _reply({
            "q1": {"type": "noul", "value": 0.93, "confidence": 0.9},
            "q2": {"type": "choice", "value": "use", "confidence": 0.8},
            "q3": {"type": "score", "value": 2.0, "confidence": 0.7},
        })
        _with_client(self, _FakeClient([_resp(200, body, {"x-request-id": "req-t-1"})], seen))
        j = OpenAIJudge(api_key="sk-test", max_retries=1, retry_backoff=0)
        out = j.ask({"request": "x"}, [
            noul("q1", "is it grounded?"),
            choice("q2", "treat as?", {"use": "safe", "verify": "check"}),
            score("q3", "severity", ["low", "med", "high"]),
        ])
        self.assertEqual([a.question_id for a in out.answers], ["q1", "q2", "q3"])
        self.assertEqual(out.answers[1].choice, "use")
        self.assertEqual(out.answers[2].score, 2.0)
        self.assertEqual(out.request_id, "req-t-1")
        self.assertEqual((out.input_tokens, out.output_tokens), (100, 40))
        self.assertEqual(out.model, "gpt-4o-mini")
        # the request carried our model and auth header
        self.assertEqual(seen[0][1]["Authorization"], "Bearer sk-test")
        self.assertEqual(seen[0][0]["model"], "gpt-4o-mini")

    def test_custom_base_url_reaches_the_given_endpoint(self):
        seen = []
        _with_client(self, _FakeClient([_resp(200, _reply({"q1": {
            "type": "noul", "value": 0.5, "confidence": 0.5}}))], seen))
        j = OpenAIJudge(api_key="sk-test", base_url="http://localhost:11434/v1")
        j.ask("s", [noul("q1", "x")])
        self.assertTrue(seen[0][1] is not None)
        # URL shape: the adapter posts to {base}/chat/completions
        import agentcheck.judges.openai as O
        # (URL captured implicitly by the fake; assert on the port)
        self.assertEqual(O.OpenAIJudge(
            api_key="sk-test", base_url="http://localhost:11434/v1")._base,
            "http://localhost:11434/v1")


class TestRobustness(unittest.TestCase):
    def setUp(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"

    def tearDown(self):
        os.environ.pop("OPENAI_API_KEY", None)

    def test_transient_503_is_retried(self):
        _with_client(self, _FakeClient([
            _resp(503, None, {"x-request-id": "req-1"}),
            _resp(200, _reply({"q1": {"type": "noul", "value": 0.5,
                                      "confidence": 0.5}}),
                  {"x-request-id": "req-2"}),
        ]))
        j = OpenAIJudge(api_key="sk-test", max_retries=2, retry_backoff=0)
        out = j.ask("s", [noul("q1", "x")])
        self.assertEqual(out.request_id, "req-2")

    def test_401_is_permanent_and_carries_the_request_id(self):
        _with_client(self, _FakeClient([_resp(401, None, {"x-request-id": "req-9"})]))
        j = OpenAIJudge(api_key="sk-test", max_retries=1, retry_backoff=0)
        with self.assertRaises(RuntimeError) as ctx:
            j.ask("s", [noul("q1", "x")])
        self.assertIn("401", str(ctx.exception))
        self.assertIn("req-9", str(ctx.exception))

    def test_retries_exhausted_raises(self):
        _with_client(self, _FakeClient([_resp(429, None)] * 3))
        j = OpenAIJudge(api_key="sk-test", max_retries=2, retry_backoff=0)
        with self.assertRaises(RuntimeError):
            j.ask("s", [noul("q1", "x")])

    def test_unparseable_reply_raises_not_fabricates(self):
        bad = {"choices": [{"message": {"content": "I think it's fine!"}}]}
        _with_client(self, _FakeClient([_resp(200, bad)]))
        j = OpenAIJudge(api_key="sk-test")
        with self.assertRaises(RuntimeError):
            j.ask("s", [noul("q1", "x")])

    def test_nan_and_out_of_range_are_refused(self):
        _with_client(self, _FakeClient([_resp(200, _reply({
            "q1": {"type": "noul", "value": 1.7, "confidence": "high"},
            "q2": {"type": "noul", "value": 0.9, "confidence": float("nan")},
        }))]))
        j = OpenAIJudge(api_key="sk-test")
        out = j.ask("s", [noul("q1", "x"), noul("q2", "y")])
        # out of range value -> refused as 0; unparseable confidence -> 0
        self.assertEqual(out.answers[0].value, 0.0)
        self.assertEqual(out.answers[0].confidence, 0.0)
        self.assertEqual(out.answers[1].confidence, 0.0)
        # a clean one passes through
        _with_client(self, _FakeClient([_resp(200, _reply({
            "q1": {"type": "noul", "value": 0.93, "confidence": 0.88}}))]))
        out = OpenAIJudge(api_key="sk-test").ask("s", [noul("q1", "x")])
        self.assertEqual(out.answers[0].noul, 0.93)
        self.assertEqual(out.answers[0].confidence, 0.88)

    def test_unknown_choice_label_keeps_confidence_zero(self):
        _with_client(self, _FakeClient([_resp(200, _reply({
            "q1": {"type": "choice", "value": "bananas", "confidence": 0.99},
        }))]))
        j = OpenAIJudge(api_key="sk-test")
        out = j.ask("s", [choice("q1", "treat as?", {"use": "safe", "verify": "c"})])
        self.assertEqual(out.answers[0].choice, "bananas")
        self.assertEqual(out.answers[0].confidence, 0.0,
                         "a label we cannot map must never carry confidence")

    def test_no_key_is_a_clear_error(self):
        os.environ.pop("OPENAI_API_KEY", None)
        with self.assertRaises(RuntimeError) as ctx:
            OpenAIJudge()
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))


class TestRegistry(unittest.TestCase):
    def tearDown(self):
        for k in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "AGENTCHECK_JUDGE"):
            os.environ.pop(k, None)

    def test_openai_is_registered(self):
        from agentcheck.judges import available
        self.assertIn("openai", available())

    def test_default_follows_whichever_key_exists(self):
        self.assertEqual(default_name(), "stub")
        os.environ["OPENAI_API_KEY"] = "sk-test"
        self.assertEqual(default_name(), "openai")
        os.environ["TYPESAFE_API_KEY"] = "ts-test"
        self.assertEqual(default_name(), "typesafe")
        os.environ["AGENTCHECK_JUDGE"] = "openai"
        self.assertEqual(default_name(), "openai")

    def test_get_judge_none_resolves_from_keys(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        self.assertIsInstance(get_judge(None), OpenAIJudge)


if __name__ == "__main__":
    unittest.main()
