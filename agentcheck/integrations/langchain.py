"""LangChain callback handler: every tool execution gets checked.

    from agentcheck.integrations.langchain import AgentCheckCallbackHandler

    handler = AgentCheckCallbackHandler(key="ac_...", policy="default")
    agent = create_tool_calling_agent(llm, tools, prompt)
    AgentExecutor(agent=agent, tools=tools,
                  callbacks=[handler]).invoke({"input": "..."})

Duck-typed: no ``langchain`` import here. To satisfy type checks, mix in
the real base: ``class Mine(AgentCheckCallbackHandler, BaseCallbackHandler)``.
Sync methods only — LangChain runs sync handlers fine in async flows.

Trace identity: pass trace_id explicitly, or stamp per-invocation metadata
``{"agentcheck_trace_id": ...}``; the human ask rides in
``{"agentcheck_request": ...}``. Without them, the tool input is the
request context and the session chains spans automatically.
"""

from __future__ import annotations

import json

from agentcheck.integrations._core import CheckSession


class AgentCheckBlocked(RuntimeError):
    """A tool call the policy says block. .checks holds every check result."""

    def __init__(self, message, checks=None):
        super().__init__(message)
        self.checks = checks or []


class AgentCheckCallbackHandler:
    """Check on tool start (before execution), record outcome on tool end."""

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
        self._pending = {}

    @staticmethod
    def _tool_name(serialized) -> str:
        if isinstance(serialized, dict):
            return str(serialized.get("name") or serialized.get("id")
                       or "unknown_tool")
        return str(getattr(serialized, "name", None) or "unknown_tool")

    @staticmethod
    def _tool_args(input_str) -> dict:
        if isinstance(input_str, dict):
            return input_str
        try:
            v = json.loads(input_str) if input_str else {}
            return v if isinstance(v, dict) else {"_raw": str(input_str)[:2000]}
        except Exception:
            return {"_raw": str(input_str or "")[:2000]}

    def on_tool_start(self, serialized, input_str, *, run_id,  # noqa: N803
                      parent_run_id=None, tags=None, metadata=None,
                      **kwargs):
        meta = metadata or {}
        tool = self._tool_name(serialized)
        request = str(meta.get("agentcheck_request")
                      or (input_str if isinstance(input_str, str)
                          else json.dumps(input_str, default=str)))[:2000]
        entry = self.session.check(
            tool, self._tool_args(input_str), request,
            trace_id=meta.get("agentcheck_trace_id"))
        entry["run_id"] = str(run_id)
        self._pending[str(run_id)] = entry
        self.checks.append(entry)
        if self.on_check is not None:
            try:
                self.on_check([entry])
            except Exception:
                pass
        if self.block and entry.get("decision") == "block":
            raise AgentCheckBlocked(
                f"agentcheck policy blocked {tool}", checks=[entry])

    def on_tool_end(self, output, *, run_id, **kwargs):
        entry = self._pending.pop(str(run_id), None)
        if entry is not None:
            try:
                entry["output_preview"] = str(output)[:500]
            except Exception:
                pass

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._pending.pop(str(run_id), None)
