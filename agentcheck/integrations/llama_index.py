"""LlamaIndex callback handler: function/tool events get checked.

    from agentcheck.integrations.llama_index import AgentCheckHandler

    handler = AgentCheckHandler(key="ac_...", policy="default")
    Settings.callback_manager = CallbackManager([handler])

Duck-typed: no ``llama_index`` import here, and tolerant of payload shape
drift across versions. An event is checked when its type name mentions a
function or tool call; anything else passes through silently. When the
tool cannot be determined, the event is skipped (never invented).
"""

from __future__ import annotations

from agentcheck.integrations._core import CheckSession


class AgentCheckBlocked(RuntimeError):
    """A tool call the policy says block. .checks holds every check result."""

    def __init__(self, message, checks=None):
        super().__init__(message)
        self.checks = checks or []


def _event_name(event_type) -> str:
    try:
        return str(getattr(event_type, "value", event_type)).lower()
    except Exception:
        return ""


def _tool_from_payload(payload: dict):
    """(name, args) or (None, None) when the payload says nothing usable."""
    if not isinstance(payload, dict):
        return None, None
    for k in ("function_name", "tool_name", "name"):
        v = payload.get(k)
        if isinstance(v, str) and v:
            name = v
            break
    else:
        tool = payload.get("tool")
        name = getattr(tool, "name", None) if tool is not None else None
        if not isinstance(name, str) or not name:
            fn = payload.get("function_call")
            name = getattr(fn, "name", None) if fn is not None else None
        if not isinstance(name, str) or not name:
            return None, None
    for k in ("function_args", "arguments", "args", "kwargs", "input"):
        v = payload.get(k)
        if isinstance(v, dict):
            return name, v
        if isinstance(v, str) and v.strip():
            import json
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    return name, parsed
            except Exception:
                pass
            return name, {"_raw": v[:2000]}
    return name, {}


class AgentCheckHandler:
    """Check function-call events on start; never invent a tool name."""

    def __init__(self, key=None, policy=None, checkset="safety", url=None,
                 timeout=30.0, block=False, on_check=None, trace_id=None,
                 checker=None):
        self.session = CheckSession(key=key, policy=policy,
                                    checkset=checkset, url=url,
                                    timeout=timeout, trace_id=trace_id,
                                    checker=checker)
        self.block = block
        self.on_check = on_check
        self.checks = []

    def on_event_start(self, event_type, payload=None, event_id="",
                       parent_id="", **kwargs):
        ename = _event_name(event_type)
        if "function" not in ename and "tool" not in ename:
            return event_id
        name, args = _tool_from_payload(payload or {})
        if not name:
            return event_id
        entry = self.session.check(name, args, request=str(payload)[:2000])
        entry["event_id"] = event_id
        self.checks.append(entry)
        if self.on_check is not None:
            try:
                self.on_check([entry])
            except Exception:
                pass
        if self.block and entry.get("decision") == "block":
            raise AgentCheckBlocked(
                f"agentcheck policy blocked {name}", checks=[entry])
        return event_id

    def on_event_end(self, event_type, payload=None, event_id="", **kwargs):
        return event_id
