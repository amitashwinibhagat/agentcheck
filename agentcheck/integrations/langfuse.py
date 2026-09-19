"""Langfuse history -> AgentCheck dataset records.

No ``langfuse`` import happens here: pass a client shaped like the SDK
(or the real one) and this works. The real SDK path is
``Langfuse().fetch_traces()`` / ``fetch_observations()`` / ``fetch_scores()``;
env needed in production: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY /
LANGFUSE_HOST.

Mapping:
  trace input (first user content found) -> request
  TOOL observations (or spans whose name looks like a tool call)
    name -> tool, input -> args
  scores named --gold-score (CLI) -> human gold via gold_from_score.
    Scores with any other name are ignored, never coerced.
"""

from __future__ import annotations

from agentcheck.integrations.import_common import (
    gold_from_score, make_record, summarize)


def _user_text(data) -> str:
    """First user-content string found in common Langfuse input shapes."""
    if isinstance(data, str):
        return data[:2000]
    if isinstance(data, list):
        for m in data:
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, list):
                    c = " ".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in c)
                return str(c)[:2000]
        for m in data:
            if isinstance(m, dict) and m.get("content"):
                return str(m["content"])[:2000]
    if isinstance(data, dict):
        for k in ("input", "query", "question", "prompt", "content"):
            if data.get(k):
                return _user_text(data[k])
    return ""


def _obs_tool(obs) -> str | None:
    """Observation -> tool name, or None when it is not a tool call."""
    get = (lambda k: obs.get(k)) if isinstance(obs, dict) else (
        lambda k: getattr(obs, k, None))
    kind = str(get("type") or "").upper()
    name = str(get("name") or "")
    if kind == "TOOL":
        return name or "unknown_tool"
    if kind == "SPAN" and name and any(
            t in name.lower() for t in ("tool", "function", "call")):
        return name
    return None


def _obs_args(obs) -> dict:
    get = (lambda k: obs.get(k)) if isinstance(obs, dict) else (
        lambda k: getattr(obs, k, None))
    for k in ("input", "args", "arguments", "parameters"):
        v = get(k)
        if isinstance(v, dict):
            return v
    return {}


def fetch(client, limit: int = 100, gold_score: str | None = None) -> list[dict]:
    """Pull traces; return dataset records with honest provenance."""
    records = []
    traces = client.fetch_traces(limit=limit)
    for tr in traces:
        get = (lambda k: tr.get(k)) if isinstance(tr, dict) else (
            lambda k: getattr(tr, k, None))
        tid = get("id")
        request = _user_text(get("input"))
        try:
            observations = client.fetch_observations(trace_id=tid)
        except Exception:
            observations = []
        gold = None
        if gold_score:
            try:
                for s in client.fetch_scores(trace_id=tid, name=gold_score):
                    get_s = (lambda k: s.get(k)) if isinstance(s, dict) else (
                        lambda k: getattr(s, k, None))
                    gold = gold_from_score(get_s("value"))
                    if gold:
                        break
            except Exception:
                gold = None
        for obs in observations or []:
            tool = _obs_tool(obs)
            if not tool:
                continue
            get_o = (lambda k: obs.get(k)) if isinstance(obs, dict) else (
                lambda k: getattr(obs, k, None))
            records.append(make_record(
                {"request": request or "(no input captured)",
                 "tool": tool, "args": _obs_args(obs)},
                gold=gold,
                provenance={"source": "langfuse",
                            "trace_id": tid,
                            "observation_id": get_o("id"),
                            "gold_score": gold_score if gold else None}))
    return records


def report(records: list[dict]) -> dict:
    out = summarize(records)
    out["source"] = "langfuse"
    return out
