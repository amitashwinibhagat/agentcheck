"""Anthropic tool-use wrapper: every tool_use block gets checked.

    import anthropic
    from agentcheck.integrations.anthropic import watch

    client = watch(anthropic.Anthropic(), key="ac_...", policy="default")
    resp = client.messages.create(model="claude-sonnet-4-5", max_tokens=512,
                                  messages=[...], tools=[...])
    # resp is the untouched SDK response. With block=True, a "block"
    # decision raises AgentCheckBlocked instead of returning, so the agent
    # never executes the call.

Duck-typed: no ``anthropic`` import here. Works with the real SDK and with
any fake exposing .messages.create and content blocks shaped like
tool_use ({id, name, input} as attrs or dicts).
"""

from __future__ import annotations

import functools

from agentcheck.integrations._core import CheckSession, last_user_text
from agentcheck.integrations.openai import AgentCheckBlocked


def _iter_tool_use(response):
    for block in getattr(response, "content", None) or []:
        btype = (block.get("type") if isinstance(block, dict)
                 else getattr(block, "type", None))
        if btype != "tool_use":
            continue
        get = (lambda k: block.get(k)) if isinstance(block, dict) else (
            lambda k: getattr(block, k, None))
        args = get("input")
        yield (get("id"), get("name"),
               args if isinstance(args, dict) else (args or {}))


def watch(client, key=None, policy=None, checkset="safety", url=None,
          timeout=30.0, block=False, on_check=None, trace_id=None,
          checker=None):
    """Wrap an Anthropic client's messages.create with tool checks."""
    session = CheckSession(key=key, policy=policy, checkset=checkset,
                           url=url, timeout=timeout, trace_id=trace_id,
                           checker=checker)
    try:
        messages_api = client.messages
    except Exception as e:
        raise RuntimeError(
            "watch() needs a client with .messages.create") from e
    orig_create = messages_api.create

    @functools.wraps(orig_create)
    def create(*args, **kwargs):
        messages = kwargs.get("messages")
        if messages is None and args:
            messages = args[-1] if isinstance(args[-1], list) else None
        request_text = last_user_text(messages)
        response = orig_create(*args, **kwargs)
        checks = []
        for call_id, name, call_args in _iter_tool_use(response):
            if not name:
                continue
            d = session.check(name, call_args, request_text)
            checks.append({"tool_use_id": call_id, **d})
        if on_check is not None and checks:
            try:
                on_check(checks)
            except Exception:
                pass
        if block:
            blocked = [c for c in checks if c.get("decision") == "block"]
            if blocked:
                raise AgentCheckBlocked(
                    "agentcheck policy blocked "
                    + ", ".join(c.get("tool", "?") for c in blocked),
                    checks=checks)
        return response

    messages_api.create = create
    client.agentcheck_watched = True
    return client
