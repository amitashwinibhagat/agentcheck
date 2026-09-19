"""Shared core for framework adapters. One CheckSession owns the trace
chain: each check parents to the previous span, so multi-step runs
reassemble under one trace id. Adapters differ only in where they get
(tool, args, request) from and when they run the check."""

from __future__ import annotations

import json
import os
import urllib.request


def post_check(base, key, payload, timeout):
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/check",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def last_user_text(messages) -> str:
    """Most recent user message, across OpenAI/Anthropic message shapes."""
    try:
        for m in reversed(messages or []):
            role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
            if role == "user":
                c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
                if isinstance(c, list):  # content parts
                    c = " ".join(
                        (p.get("text", "") if isinstance(p, dict)
                         else getattr(p, "text", None) or str(p))
                        for p in c)
                return str(c or "")[:2000]
    except Exception:
        pass
    return ""


class CheckSession:
    """Trace-chained checks against /v1/check (or an injected checker).

    checker, when given, is check(trace_dict) -> response dict and replaces
    HTTP. A failed check degrades to {"error": ..., "tool": ...} — it never
    raises, so a broken check cannot break the agent's run.
    """

    def __init__(self, key=None, policy=None, checkset="safety", url=None,
                 timeout=30.0, trace_id=None, checker=None):
        self.key = key or os.environ.get("AGENTCHECK_KEY")
        self.policy = policy
        self.checkset = checkset
        self.base = url or os.environ.get("AGENTCHECK_URL",
                                          "http://127.0.0.1:7373")
        self.timeout = timeout
        self.trace_id = trace_id
        self.parent_span_id = None
        self.checker = checker

    def check(self, tool: str, args, request: str = "",
              trace_id=None) -> dict:
        if not isinstance(args, dict):
            args = {"_raw": str(args)[:2000]} if args else {}
        payload = {"trace": {"request": request, "tool": tool, "args": args},
                   "checkset": self.checkset}
        if self.policy:
            payload["policy"] = self.policy
        tid = trace_id or self.trace_id
        if tid:
            payload["trace_id"] = tid
        if self.parent_span_id:
            payload["parent_span_id"] = self.parent_span_id
        if self.checker is not None:
            try:
                d = self.checker(payload["trace"])
            except Exception as e:
                return {"tool": tool, "error": str(e)}
        else:
            if not self.key:
                return {"tool": tool,
                        "error": "no key: pass key= or set AGENTCHECK_KEY"}
            try:
                d = post_check(self.base, self.key, payload, self.timeout)
            except Exception as e:
                return {"tool": tool, "error": str(e)}
        if not isinstance(d, dict):
            return {"tool": tool, "error": "checker returned non-dict"}
        entry = {"tool": tool, **d}
        if tid or d.get("trace_id"):
            self.trace_id = d.get("trace_id") or tid
        if d.get("span_id"):
            self.parent_span_id = d["span_id"]
        return entry
