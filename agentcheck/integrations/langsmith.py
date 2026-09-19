"""LangSmith history -> AgentCheck dataset records.

No ``langsmith`` import happens here: pass a client shaped like the SDK
(or the real one). Real SDK path: ``Client().list_runs(...)`` for tool runs
plus ``list_feedback()``; env needed in production: LANGSMITH_API_KEY /
LANGSMITH_ENDPOINT.

Mapping:
  runs with run_type "tool": name -> tool, inputs -> args.
    request = nearest ancestor human input (walked via parent_run_ids when
    the client exposes get_run, else the run's own inputs).
  feedback with the --gold-score key -> human gold via gold_from_score.
"""

from __future__ import annotations

from agentcheck.integrations.import_common import (
    gold_from_score, make_record, summarize)


def _text(inputs) -> str:
    if isinstance(inputs, str):
        return inputs[:2000]
    if isinstance(inputs, dict):
        for k in ("input", "query", "question", "prompt", "content",
                  "messages"):
            v = inputs.get(k)
            if isinstance(v, str) and v.strip():
                return v[:2000]
            if isinstance(v, list):
                for m in reversed(v):
                    if isinstance(m, dict) and m.get("type") == "human":
                        return str(m.get("content", ""))[:2000]
                for m in v:
                    if isinstance(m, dict) and m.get("content"):
                        return str(m["content"])[:2000]
    return ""


def _run_get(run, k):
    return run.get(k) if isinstance(run, dict) else getattr(run, k, None)


def _ancestor_request(client, run) -> str:
    """Nearest human input up the chain; empty when nothing found."""
    own = _text(_run_get(run, "inputs"))
    parents = _run_get(run, "parent_run_ids") or []
    if not parents:
        return own
    get_run = getattr(client, "get_run", None)
    if get_run is None:
        return own
    try:
        for pid in parents:
            p = get_run(pid)
            t = _text(_run_get(p, "inputs"))
            if t:
                return t
    except Exception:
        pass
    return own


def fetch(client, limit: int = 100, gold_score: str | None = None) -> list[dict]:
    """Pull tool runs; return dataset records with honest provenance."""
    records = []
    try:
        runs = client.list_runs(limit=limit, run_type="tool")
    except TypeError:
        runs = client.list_runs(limit=limit)
    for run in runs or []:
        if str(_run_get(run, "run_type") or "tool") != "tool":
            continue
        rid = _run_get(run, "id")
        request = _ancestor_request(client, run) or "(no input captured)"
        gold = None
        if gold_score:
            try:
                for fb in client.list_feedback(run_ids=[rid], key=gold_score):
                    gold = gold_from_score(_run_get(fb, "score"))
                    if gold:
                        break
            except Exception:
                gold = None
        records.append(make_record(
            {"request": request,
             "tool": str(_run_get(run, "name") or "unknown_tool"),
             "args": _run_get(run, "inputs") or {}},
            gold=gold,
            provenance={"source": "langsmith",
                        "run_id": str(rid),
                        "feedback_key": gold_score if gold else None}))
    return records


def report(records: list[dict]) -> dict:
    out = summarize(records)
    out["source"] = "langsmith"
    return out
