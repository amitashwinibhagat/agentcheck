"""Quota and answer-cache infrastructure for the metered proxy.

Moved out of proxy.py, which is a route file: these two classes have no
endpoint logic, and burying them at the bottom of 1,300 lines meant every
edit near them looked like an endpoint change. They are imported by the
proxy and by tests, never constructed anywhere else.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agentcheck.store import Store


class QuotaGuard:
    def __init__(self, store: Store, window: float) -> None:
        self.store, self.window = store, window

    def used(self, key: str) -> int:
        return self.store.used_in_window(key, self.window)

    def allow(self, key: str, n: int) -> bool:
        # key here is the post-auth identity (kid), not the raw token.
        row = self.store.lookup_kid(key)
        if row is None:
            return False
        return self.used(key) + n <= row["qpm_limit"]


class AnswerCache:
    """Bounded, LRU-evicting cache.

    An eval server can run for weeks. Without a bound, identical repeated checks
    would grow memory without limit for the lifetime of the process.

    The key includes the JUDGE. It did not, and the hole was real: a request
    that overrode the judge (per-request `judge=`) was served the cached
    answers of whichever judge ran first, then recorded them under the
    requesting judge's name. That is fabricated provenance in a product whose
    whole claim is that provenance is measured, so the judge is part of the
    identity of a cached answer, not metadata about it.
    """

    def __init__(self, max_entries: int = 8192) -> None:
        from collections import OrderedDict
        self._store: OrderedDict[str, tuple] = OrderedDict()
        self._max = max_entries

    @staticmethod
    def _key(state: Any, questions: Any, judge: str | None = None) -> str:
        serial_q = []
        for q in questions:
            if hasattr(q, "to_payload"):
                serial_q.append({"id": q.id, **q.to_payload()})
            else:
                serial_q.append(q)
        blob = json.dumps({"s": state, "q": serial_q, "j": judge or ""},
                          sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, state, questions, judge: str | None = None):
        k = self._key(state, questions, judge)
        v = self._store.get(k)
        if v is not None:
            self._store.move_to_end(k)
        return v

    def put(self, state, questions, answers, model: str | None = None,
            judge: str | None = None) -> None:
        k = self._key(state, questions, judge)
        self._store[k] = (answers, model or "unknown")
        self._store.move_to_end(k)
        while len(self._store) > self._max:
            self._store.popitem(last=False)


WINDOW_SECONDS = 60.0
