"""OpenAI tool-calling wrapper: every tool the model requests gets checked.

    import openai
    from agentcheck.integrations.openai import watch

    client = watch(openai.OpenAI(), key="ac_...", policy="default")
    resp = client.chat.completions.create(model="gpt-4o", messages=[...],
                                          tools=[...])
    # resp is the untouched OpenAI response. Checks ran before it was
    # returned (after the model asked, before your agent executes) and
    # were delivered to on_check. With block=True, a "block" decision
    # raises AgentCheckBlocked instead of returning.

The wrapper is duck-typed: it works with the real SDK and with any fake
that exposes .chat.completions.create and tool_calls shaped like the
real thing. No import of ``openai`` happens here, so this module loads
(and tests run) without the SDK installed.
"""

from __future__ import annotations

import functools
import json

from agentcheck.integrations._core import CheckSession, last_user_text

_last_user_text = last_user_text  # backwards-compat alias


class AgentCheckBlocked(RuntimeError):
    """A tool call the policy says block. .checks holds every check result."""

    def __init__(self, message, checks=None):
        super().__init__(message)
        self.checks = checks or []


def _iter_tool_calls(response):
    """Yield (call_id, name, args) for each requested tool call."""
    try:
        choices = response.choices or []
    except Exception:
        return
    for ch in choices:
        msg = getattr(ch, "message", None)
        calls = getattr(msg, "tool_calls", None) or []
        for tc in calls:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None)
            raw_args = getattr(fn, "arguments", None)
            try:
                args = json.loads(raw_args) if raw_args else {}
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {"_raw": str(raw_args)[:2000]}
            yield (getattr(tc, "id", None), name, args)


def watch(client, key=None, policy=None, checkset="safety", url=None,
          timeout=30.0, block=False, on_check=None, trace_id=None,
          checker=None):
    """Wrap an OpenAI client's chat.completions.create with tool checks.

    checker, when given, is check(trace_dict) -> check-response dict and
    replaces the HTTP call. It exists for tests; production uses the server
    so reliability correction, policies, and metering apply uniformly.
    """
    session = CheckSession(key=key, policy=policy, checkset=checkset,
                           url=url, timeout=timeout, trace_id=trace_id,
                           checker=checker)

    try:
        completions = client.chat.completions
    except Exception as e:
        raise RuntimeError(
            "watch() needs a client with .chat.completions.create") from e
    orig_create = completions.create

    @functools.wraps(orig_create)
    def create(*args, **kwargs):
        messages = kwargs.get("messages")
        if messages is None and len(args) > 1:
            messages = args[1]
        request_text = last_user_text(messages)
        response = orig_create(*args, **kwargs)
        checks = []
        for call_id, name, call_args in _iter_tool_calls(response):
            if not name:
                continue
            d = session.check(name, call_args, request_text)
            entry = {"tool_call_id": call_id, **d}
            checks.append(entry)
        if on_check is not None and checks:
            try:
                on_check(checks)
            except Exception:
                pass  # a callback must not break the run either
        if block:
            blocked = [c for c in checks if c.get("decision") == "block"]
            if blocked:
                raise AgentCheckBlocked(
                    "agentcheck policy blocked "
                    + ", ".join(c.get("tool", "?") for c in blocked),
                    checks=checks)
        return response

    completions.create = create
    client.agentcheck_watched = True
    return client
