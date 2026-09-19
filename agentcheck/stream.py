"""In-process pub/sub for live decision streaming.

Single-process only (the workspace is one uvicorn worker). Subscribers get
every saved result for their key as it lands.
"""

from __future__ import annotations

import asyncio
import json
import time


class Bus:
    def __init__(self, max_queue: int = 256):
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._max = max_queue

    def subscribe(self, user_key: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max)
        self._subs.setdefault(user_key, set()).add(q)
        return q

    def unsubscribe(self, user_key: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(user_key)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(user_key, None)

    def publish(self, user_key: str, payload: dict) -> None:
        """Non-blocking: slow subscribers drop oldest (queue is bounded)."""
        for q in list(self._subs.get(user_key, ())):
            msg = {"ts": time.time(), **payload}
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def subscriber_count(self, user_key: str | None = None) -> int:
        if user_key is None:
            return sum(len(s) for s in self._subs.values())
        return len(self._subs.get(user_key, ()))


def result_payload(saved: dict) -> dict:
    """The slim per-result event the stream emits."""
    return {
        "kind": "result",
        "id": saved.get("id"),
        "verdict": saved.get("trace_verdict"),
        "confidence": saved.get("confidence"),
        "severity": saved.get("severity"),
        "decision": saved.get("decision"),
        "policy": saved.get("policy"),
        "request": saved.get("request"),
        "tool": saved.get("tool"),
        "checkset": saved.get("checkset"),
        "duplicate": saved.get("duplicate", False),
        "trace_id": saved.get("trace_id"),
        "span_id": saved.get("span_id"),
        "parent_span_id": saved.get("parent_span_id"),
    }


def sse_format(msg: dict) -> str:
    kind = msg.get("kind", "message")
    return f"event: {kind}\ndata: {json.dumps(msg, default=str)}\n\n"
