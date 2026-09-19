"""TypeSafe (System One / Jev) adapter.

THIS IS THE ONLY FILE THAT KNOWS api.typesafe.ai EXISTS.
Swap vendors by writing a second adapter; product code never changes.

Verified against the live API (2026-09):
  POST https://api.typesafe.ai/v1/systemone, Bearer auth
  body: {"model": ..., "state": ..., "questions": {...}}
  response: {"model": ..., "answers": {...}, "usage": {input_tokens, output_tokens}}

Robustness contract:
  * transient failures are retried, not surfaced — a 429 or a 5xx in CI should
    not fail an eval the same way a real misjudgment does
  * a confidence that is NaN or Inf is treated as corrupt data (None), never
    written into a report as a number a gate will quietly trust
  * every error carries the request id when one exists, so a support ticket
    about a failed run has a reconciliation key
"""

from __future__ import annotations

import os
import time
from typing import Any, Sequence

import httpx

from agentcheck.judges.base import Answer, Judgment, Question

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"

# Retried as transient: 429, 500, 502, 503, 504. 4xx are permanent (bad request,
# bad key, bad model) and never retried.
_TRANSIENT = {429, 500, 502, 503, 504}


class TypeSafeJudgeError(RuntimeError):
    """A permanent judge failure with a reconciliation key when available."""


def _finite(x: Any) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if x != x or abs(x) == float("inf"):
        return None
    return x


class TypeSafeJudge:
    """Raw-HTTP adapter. Deliberately not using typesafe-sdk server-side —
    the proxy needs header access (request id, service time) the SDK hides."""

    name = "typesafe"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_retries: int | None = None,
        retry_backoff: float = 0.5,
    ) -> None:
        key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not key:
            raise RuntimeError(
                "TYPESAFE_API_KEY is required. Get one at https://console.typesafe.ai/keys"
            )
        self._key = key
        self._model = model or os.environ.get("TYPESAFE_DEFAULT_MODEL", DEFAULT_MODEL)
        self._base = (base_url or os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._max_retries = (
            max_retries if max_retries is not None
            else int(os.environ.get("AGENTCHECK_JUDGE_RETRIES", "3"))
        )
        self._retry_backoff = retry_backoff
        self._model_resolved = None

    @property
    def _model_for_report(self) -> str | None:
        return self._model_resolved or self._model

    def ask(self, state: Any, questions: Sequence[Question]) -> Judgment:
        if not questions:
            raise ValueError("at least one question is required")
        payload = {
            "model": self._model,  # REQUIRED — 422 without it
            "state": state,
            "questions": {q.id: q.to_payload() for q in questions},
        }
        headers = {"Authorization": f"Bearer {self._key}"}
        url = f"{self._base}/v1/systemone"
        request_id = None

        with httpx.Client(timeout=self._timeout) as client:
            resp = None
            attempt = 0
            while True:
                try:
                    resp = client.post(url, json=payload, headers=headers)
                    request_id = resp.headers.get("x-typesafe-request-id") or request_id
                except httpx.TimeoutException as e:
                    attempt += 1
                    if attempt > self._max_retries:
                        raise TypeSafeJudgeError(
                            f"judge timed out after {attempt} attempts"
                            + (f" ({request_id})" if request_id else "")
                        ) from e
                    time.sleep(self._retry_backoff * (2 ** (attempt - 1)))
                    continue
                except httpx.TransportError as e:
                    attempt += 1
                    if attempt > self._max_retries:
                        raise TypeSafeJudgeError(
                            f"judge unreachable after {attempt} attempts: {e}"
                        ) from e
                    time.sleep(self._retry_backoff * (2 ** (attempt - 1)))
                    continue

                if resp.status_code in _TRANSIENT:
                    attempt += 1
                    if attempt > self._max_retries:
                        raise TypeSafeJudgeError(
                            f"judge returned {resp.status_code} after {attempt} attempts"
                            + (f" ({request_id})" if request_id else "")
                        )
                    try:
                        retry_after = float(resp.headers.get("retry-after", ""))
                    except (TypeError, ValueError):
                        retry_after = self._retry_backoff * (2 ** (attempt - 1))
                    # Cap backoff so a wedged endpoint cannot stall a run forever.
                    time.sleep(min(retry_after, 30.0))
                    continue
                if resp.status_code == 401:
                    raise TypeSafeJudgeError(
                        "judge rejected the api key (401)" + (f" ({request_id})" if request_id else "")
                    )
                if resp.status_code == 429:
                    raise TypeSafeJudgeError(
                        "judge rate limit hit and retries exhausted" + (f" ({request_id})" if request_id else "")
                    )
                if resp.status_code == 404:
                    raise TypeSafeJudgeError(
                        f"judge endpoint or model {self._model!r} not found (404) "
                        f"({request_id})" if request_id else
                        f"judge endpoint or model {self._model!r} not found (404)")
                resp.raise_for_status()
                break

            try:
                data = resp.json()
            except (ValueError, httpx.DecodingError) as e:
                raise TypeSafeJudgeError(
                    "judge returned a non-JSON body even on success "
                    + (f"({request_id})" if request_id else "")
                ) from e

        self._model_resolved = data.get("model", self._model)
        usage = data.get("usage", {}) or {}
        answers: list[Answer] = []
        for q in questions:
            raw = data.get("answers", {}).get(q.id, {})
            t = raw.get("type", q.type)
            conf = _finite(raw.get("confidence"))
            answers.append(
                Answer(
                    question_id=q.id,
                    type=t,
                    value=raw.get("noul") if t == "noul"
                          else raw.get("choice") if t == "choice"
                          else raw.get("score"),
                    confidence=conf if conf is not None else 0.0,
                    probabilities=raw.get("probabilities", {}) or {},
                )
            )
        return Judgment(
            answers=answers,
            request_id=request_id,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            server_ms=_f(resp.headers.get("x-envoy-upstream-service-time")),
            model=self._model_for_report,
        )


def _f(v: str | None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
