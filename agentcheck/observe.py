"""Client SDK: wrap an agent's tool call and get the decision inline.

    from agentcheck import observe

    with observe(tool="send_email", request="summarize inbox",
                 args={"to": "a@b.c"}, policy="refund-safety") as obs:
        send_email(...)          # your agent does the thing
    # obs.verdict, obs.confidence, obs.decision are populated on exit

The check runs when the block EXITS so it sees what actually happened.
Errors inside the block do not block the check: the trace still gets judged
(a failed tool call is exactly what you want on the record).
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error


class ObserveResult:
    def __init__(self):
        self.verdict = None
        self.confidence = None
        self.severity = None
        self.decision = None
        self.policy = None
        self.error = None
        self.result_id = None
        self.timing = None
        self.trace_id = None
        self.span_id = None
        self.parent_span_id = None

    def __repr__(self):
        return (f"ObserveResult(verdict={self.verdict!r}, "
                f"confidence={self.confidence!r}, decision={self.decision!r})")


class ObserveError(RuntimeError):
    pass


def observe(tool: str, request: str, args: dict | None = None,
            policy: str | None = None, checkset: str = "safety",
            key: str | None = None, url: str | None = None,
            timeout: float = 30.0, trace_id: str | None = None,
            span_id: str | None = None,
            parent_span_id: str | None = None):
    """Context manager. Scores the trace on exit; never raises on judge
    failure (the error is on .error), because a failed check must not
    break the agent's run."""
    import contextlib

    @contextlib.contextmanager
    def ctx():
        res = ObserveResult()
        yield res
        payload = {
            "trace": {"request": request, "tool": tool, "args": args or {}},
            "checkset": checkset,
        }
        if trace_id:
            payload["trace_id"] = trace_id
        if span_id:
            payload["span_id"] = span_id
        if parent_span_id:
            payload["parent_span_id"] = parent_span_id
        if policy:
            payload["policy"] = policy
        k = key or os.environ.get("AGENTCHECK_KEY")
        base = url or os.environ.get("AGENTCHECK_URL", "http://127.0.0.1:7373")
        if not k:
            res.error = "no key: pass key= or set AGENTCHECK_KEY"
            return
        req = urllib.request.Request(
            base.rstrip("/") + "/v1/check",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {k}",
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail")
            except Exception:
                detail = str(e)
            res.error = f"HTTP {e.code}: {detail}"
            return
        except Exception as e:  # network down must not break the agent
            res.error = str(e)
            return
        res.verdict = d.get("trace_verdict")
        res.confidence = d.get("confidence")
        res.severity = d.get("severity")
        res.decision = d.get("decision")
        res.policy = d.get("policy")
        res.result_id = d.get("id")
        res.timing = d.get("timing_ms")
        res.trace_id = d.get("trace_id")
        res.span_id = d.get("span_id")
        res.parent_span_id = d.get("parent_span_id")

    return ctx()
