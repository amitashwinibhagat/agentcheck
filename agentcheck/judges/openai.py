"""OpenAI-compatible judge adapter — the BYOK door.

Brings-your-own-key for anyone who already has an OpenAI key, and to any
OpenAI-compatible endpoint (OPENAI_BASE_URL): OpenRouter, Together, vLLM,
Ollama. The most common objection to a hosted judge is "I don't want another
vendor account"; this adapter answers it. Same role as typesafe.py: the only
file that knows this vendor's wire format.

Design, mirroring the TypeSafe adapter's robustness contract:
  * raw httpx, not the openai SDK — the proxy needs header access
    (request id, service time) SDKs tend to hide, and one fewer dependency
  * one chat completion for ALL questions of a call (batching like TypeSafe),
    JSON mode, temperature 0
  * transient failures (429, 5xx) are retried with capped backoff; 4xx are
    permanent and surface with the request id
  * a confidence that is NaN/Inf or outside 0..1 becomes 0.0-corrupt-handling
    per the shared rule: never write a number a gate will quietly trust.
    NOTE: an LLM's self-reported confidence is NOT calibration — measuring
    that gap is AgentCheck's product, so we pass it through honestly rather
    than pretending it is an empirical accuracy.
  * a choice answer that does not match any criterion keeps the raw label
    with confidence 0.0: downstream it reads as "unknown" (review), never as
    a fabricated pass
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Sequence

import httpx

from agentcheck.judges.base import Answer, Judgment, Question

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

_TRANSIENT = {429, 500, 502, 503, 504}

_SYSTEM = (
    "You are a precise evaluation judge. You will be given the state of an "
    "agent call and a list of questions. Answer EVERY question. Reply with "
    "ONLY a JSON object of the shape:\n"
    '{"answers": {"<question-id>": {"type": "<noul|choice|score>", '
    '"value": <number-or-string>, "confidence": <0..1>}}}\n'
    "For type noul, value is a probability between 0 and 1. For type choice, "
    "value is EXACTLY one of the provided labels. For type score, value is a "
    "number within the provided scale. Confidence is how sure you are."
)


class OpenAIJudgeError(RuntimeError):
    """A permanent judge failure with a reconciliation key when available."""


def _finite(x: Any) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if x != x or abs(x) == float("inf"):
        return None
    return x


def _conf(x: Any) -> float:
    """Confidence is a probability or it is nothing (0.0). Never a gate-trusted
    number smuggled in as 1.7 or 'high'."""
    c = _finite(x)
    return c if c is not None and 0.0 <= c <= 1.0 else 0.0


class OpenAIJudge:
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_retries: int | None = None,
        retry_backoff: float = 0.5,
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY is required for the openai judge "
                "(or set AGENTCHECK_JUDGE to another judge)"
            )
        self._key = key
        self._model = model or os.environ.get("OPENAI_JUDGE_MODEL", DEFAULT_MODEL)
        self._base = (base_url or os.environ.get("OPENAI_BASE_URL")
                      or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._max_retries = (
            max_retries if max_retries is not None
            else int(os.environ.get("AGENTCHECK_JUDGE_RETRIES", "3"))
        )
        self._retry_backoff = retry_backoff

    @property
    def _model_for_report(self) -> str:
        return self._model

    def _question_block(self, q: Question) -> dict[str, Any]:
        block: dict[str, Any] = {"type": q.type, "instructions": q.instructions}
        if q.type == "choice":
            block["labels"] = dict(q.criteria)
        elif q.type == "score":
            block["scale"] = list(q.criteria)
        return block

    def ask(self, state: Any, questions: Sequence[Question]) -> Judgment:
        if not questions:
            raise ValueError("at least one question is required")
        user = {
            "state": state,
            "questions": {q.id: self._question_block(q) for q in questions},
        }
        url = f"{self._base}/chat/completions"
        headers = {"Authorization": f"Bearer {self._key}"}
        body = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": json.dumps(user)}],
        }
        request_id = None

        with httpx.Client(timeout=self._timeout) as client:
            resp = None
            attempt = 0
            while True:
                try:
                    resp = client.post(url, json=body, headers=headers)
                    request_id = resp.headers.get("x-request-id") or request_id
                except httpx.TimeoutException as e:
                    attempt += 1
                    if attempt > self._max_retries:
                        raise OpenAIJudgeError(
                            f"judge timed out after {attempt} attempts"
                            + (f" ({request_id})" if request_id else "")
                        ) from e
                    time.sleep(self._retry_backoff * (2 ** (attempt - 1)))
                    continue
                except httpx.TransportError as e:
                    attempt += 1
                    if attempt > self._max_retries:
                        raise OpenAIJudgeError(
                            f"judge unreachable after {attempt} attempts: {e}"
                        ) from e
                    time.sleep(self._retry_backoff * (2 ** (attempt - 1)))
                    continue

                if resp.status_code in _TRANSIENT:
                    attempt += 1
                    if attempt > self._max_retries:
                        raise OpenAIJudgeError(
                            f"judge returned {resp.status_code} after "
                            f"{attempt} attempts"
                            + (f" ({request_id})" if request_id else "")
                        )
                    try:
                        retry_after = float(resp.headers.get("retry-after", ""))
                    except (TypeError, ValueError):
                        retry_after = self._retry_backoff * (2 ** (attempt - 1))
                    time.sleep(min(retry_after, 30.0))
                    continue
                if resp.status_code == 401:
                    raise OpenAIJudgeError(
                        "judge rejected the api key (401)"
                        + (f" ({request_id})" if request_id else "")
                    )
                resp.raise_for_status()
                break

            try:
                data = resp.json()
            except (ValueError, httpx.DecodingError) as e:
                raise OpenAIJudgeError(
                    "judge returned a non-JSON body even on success "
                    + (f"({request_id})" if request_id else "")
                ) from e

        try:
            content = data["choices"][0]["message"]["content"]
            raw = json.loads(content)
            answers_raw = raw.get("answers", {}) or {}
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise OpenAIJudgeError(
                f"judge reply was not parseable as judged answers ({e})"
                + (f" ({request_id})" if request_id else "")
            ) from e

        usage = data.get("usage", {}) or {}
        answers: list[Answer] = []
        for q in questions:
            a = answers_raw.get(q.id, {}) or {}
            t = q.type
            value = a.get("value")
            if t == "noul":
                v = _finite(value)
                if v is None or not 0.0 <= v <= 1.0:
                    v, c = 0.0, 0.0  # out of range: refuse, never guess
                else:
                    c = _conf(a.get("confidence"))
                answers.append(Answer(question_id=q.id, type=t, value=v,
                                      confidence=c))
            elif t == "choice":
                label = str(value) if value is not None else ""
                known = str(label).strip() in {str(k).strip() for k in q.criteria}
                answers.append(Answer(question_id=q.id, type=t, value=label,
                                      confidence=_conf(a.get("confidence"))
                                      if known else 0.0))
            else:  # score
                v = _finite(value)
                answers.append(Answer(question_id=q.id, type=t,
                                      value=v if v is not None else 0.0,
                                      confidence=_conf(a.get("confidence"))
                                      if v is not None else 0.0))
        return Judgment(
            answers=answers,
            request_id=request_id,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            server_ms=None,  # OpenAI does not report upstream service time
            model=self._model_for_report,
        )
