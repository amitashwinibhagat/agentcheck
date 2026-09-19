"""Built-in metrics for RAG and grounded-generation traces.

Ragas and TruLens score a RAG pipeline on dimension-specific metrics rather than
one pass/fail verdict. This module does the same, but every metric is a small
atomic question set handed to whichever judge is configured — so the same
calibration machinery and the same swap path apply.

A RAG trace is:

    {"question": str, "answer": str, "contexts": [str, ...],
     "ground_truth": str (optional)}

All metrics are in [0, 1] where 1 is better, except ``hallucination`` which is a
risk score (lower is better).

One honest limitation, discovered by measuring rather than assuming: System One
returns a ``confidence`` field for Choice and Score answers but **not** for Noul
answers, which carry only the probability. Every metric here is built from Nouls,
so ``confidence`` is usually unavailable. What is reported instead is
``decisiveness``: ``|p - 0.5| * 2``, how far the answer sits from a coin flip.
That is a different quantity from calibrated confidence and is labelled as such
rather than passed off as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from agentcheck.judges.base import Question, noul, score


@dataclass
class MetricResult:
    name: str
    value: float
    confidence: float | None      # None when the judge reported none (Noul)
    detail: dict = field(default_factory=dict)

    @property
    def decisiveness(self) -> float:
        return float(self.detail.get("decisiveness") or 0.0)

    def __str__(self) -> str:  # pragma: no cover - display only
        c = "-" if self.confidence is None else f"{self.confidence:.2f}"
        return f"{self.name}={self.value:.2f} (conf {c})"


RAG_TRACE_KEYS = ("question", "answer", "contexts")


def is_rag_trace(trace: Any) -> bool:
    return (isinstance(trace, dict)
            and isinstance(trace.get("question"), str)
            and isinstance(trace.get("answer"), str)
            and isinstance(trace.get("contexts"), list))


def _norm(text: Any, limit: int = 4000) -> str:
    return str(text or "")[:limit]


def build_questions(trace: dict, include_ground_truth: bool = True) -> list[Question]:
    """The atomic questions behind every metric, asked in one batch."""
    qs: list[Question] = [
        noul("answer_is_grounded",
             "Is every factual claim in the answer supported by the provided "
             "contexts? Answer no if any claim is unsupported or invented."),
        noul("answer_contradicts_context",
             "Does the answer contradict anything stated in the provided contexts?"),
        noul("answer_addresses_question",
             "Does the answer directly address the question asked, rather than "
             "describing the topic or restating the question?"),
        score("answer_completeness",
              "How completely does the answer cover what the question requires?",
              ["misses the point", "partially answers", "fully answers"]),
    ]
    if include_ground_truth and trace.get("ground_truth"):
        qs += [
            noul("matches_ground_truth",
                 "Is the answer consistent with the reference answer? Minor wording "
                 "differences are fine; a different fact is not."),
            noul("invents_unstated_detail",
                 "Does the answer state specifics (numbers, dates, names, policies) "
                 "that appear in neither the contexts nor the reference answer?"),
        ]
    return qs


def _payload(trace: dict) -> dict:
    ctx = trace.get("contexts") or []
    lines = [f"[{i + 1}] {_norm(c)}" for i, c in enumerate(ctx)]
    state: dict[str, Any] = {
        "question": _norm(trace.get("question")),
        "answer": _norm(trace.get("answer")),
        "contexts": "\n".join(lines) if lines else "(none provided)",
    }
    if trace.get("ground_truth"):
        state["reference_answer"] = _norm(trace.get("ground_truth"))
    return state


def score_trace(judge: Any, trace: dict) -> dict[str, MetricResult]:
    """Run every RAG metric over one trace with a single judge call."""
    if not is_rag_trace(trace):
        raise ValueError(
            "not a RAG trace: needs 'question', 'answer' and 'contexts'")
    questions = build_questions(trace)
    judgment = judge.ask(_payload(trace), questions)
    by_id = {a.question_id: a for a in judgment.answers}

    def prob(qid: str) -> tuple[float, float | None]:
        """(probability, model confidence). Confidence is None when the judge
        reported none, which is the normal case for Noul answers."""
        a = by_id.get(qid)
        if a is None:
            return 0.5, None
        conf = getattr(a, "confidence", None)
        return float(a.value or 0.0), (float(conf) if conf else None)

    grounded, c_ground = prob("answer_is_grounded")
    contradicts, c_contra = prob("answer_contradicts_context")
    addresses, c_addr = prob("answer_addresses_question")
    completeness, c_comp = prob("answer_completeness")

    out: dict[str, MetricResult] = {
        # Ragas "faithfulness": the answer is supported, not contradicted.
        "faithfulness": MetricResult(
            "faithfulness",
            value=max(0.0, grounded - contradicts),
            confidence=_min_conf(c_ground, c_contra),
            detail={"grounded": rounded(grounded), "contradicts": rounded(contradicts),
                    "decisiveness": decisiveness(grounded, contradicts)},
        ),
        # Ragas "answer relevance".
        "answer_relevance": MetricResult(
            "answer_relevance", value=addresses, confidence=c_addr,
            detail={"addresses_question": rounded(addresses),
                    "decisiveness": decisiveness(addresses)},
        ),
        "answer_completeness": MetricResult(
            "answer_completeness", value=completeness / 2.0, confidence=c_comp,
            detail={"level": round(completeness, 3),
                    "decisiveness": decisiveness(completeness)},
        ),
    }

    if trace.get("ground_truth"):
        matches, c_match = prob("matches_ground_truth")
        invents, c_invent = prob("invents_unstated_detail")
        out["answer_correctness"] = MetricResult(
            "answer_correctness", value=max(0.0, matches - invents),
            confidence=_min_conf(c_match, c_invent),
            detail={"matches_reference": rounded(matches),
                    "invents_detail": rounded(invents),
                    "decisiveness": decisiveness(matches, invents)},
        )
        # Patronus-style hallucination signal, higher = worse.
        out["hallucination"] = MetricResult(
            "hallucination", value=invents, confidence=c_invent,
            detail={"invents_unstated_detail": rounded(invents),
                    "decisiveness": decisiveness(invents)},
        )

    # Context quality is judged per context, so precision is a real proportion.
    out["context_utilization"] = _context_metric(judge, trace, by_id)
    return out


def _context_metric(judge: Any, trace: dict, by_id: dict) -> MetricResult:
    """Fraction of retrieved contexts that carry anything relevant."""
    contexts = trace.get("contexts") or []
    if not contexts:
        return MetricResult("context_utilization", 0.0, 0.0,
                            {"n_contexts": 0, "note": "no contexts supplied"})
    qs = [noul(f"ctx_{i}",
               f"Does context [{i + 1}] contain information that helps answer the "
               "question? Irrelevant background counts as no.")
          for i in range(len(contexts))]
    # Reuse the judge; contexts are labelled inline by index in the payload.
    payload = _payload(trace)
    payload["instruction"] = (
        "For each numbered context, decide whether it helps answer the question.")
    judgment = judge.ask(payload, qs)
    per = {a.question_id: a for a in judgment.answers}
    vals = [float(per[f"ctx_{i}"].value or 0.0) for i in range(len(contexts))
            if f"ctx_{i}" in per]
    if not vals:
        return MetricResult("context_utilization", 0.0, 0.0,
                            {"n_contexts": len(contexts), "note": "no answers"})
    used = [i for i in range(len(contexts)) if i < len(vals) and vals[i] >= 0.5]
    return MetricResult(
        "context_utilization",
        value=sum(vals) / len(vals),
        confidence=None,
        detail={"n_contexts": len(contexts), "used": len(used),
                "per_context": [rounded(v) for v in vals],
                "decisiveness": decisiveness(*vals)},
    )


def _min_conf(*confs: float | None) -> float | None:
    """Weakest available model confidence, or None when the judge gave none."""
    present = [c for c in confs if c]
    return min(present) if present else None


def decisiveness(*vals: float | None) -> float:
    """How far the answers sit from a coin flip, in [0, 1].

    System One returns no confidence field for Noul answers, so this is what
    stands in for it. It measures how decided the answer was, not how likely it
    is to be right, and is reported under its own name.
    """
    ps = [v for v in vals if v is not None]
    if not ps:
        return 0.0
    return round(sum(abs(p - 0.5) * 2 for p in ps) / len(ps), 4)


def rounded(x: float) -> float:
    return round(float(x), 4)


def aggregate(results: Sequence[dict[str, MetricResult]]) -> dict[str, dict]:
    """Mean value and mean confidence per metric across traces."""
    acc: dict[str, list[MetricResult]] = {}
    for row in results:
        for name, m in row.items():
            acc.setdefault(name, []).append(m)
    out = {}
    for name, ms in sorted(acc.items()):
        confs = [m.confidence for m in ms if m.confidence is not None]
        out[name] = {
            "n": len(ms),
            "mean": rounded(sum(m.value for m in ms) / len(ms)),
            "mean_confidence": (rounded(sum(confs) / len(confs)) if confs else None),
            "mean_decisiveness": rounded(
                sum(m.decisiveness for m in ms) / len(ms)),
            "min": rounded(min(m.value for m in ms)),
            "max": rounded(max(m.value for m in ms)),
        }
    return out


def thresholds_pass(summary: dict[str, dict], thresholds: dict[str, float],
                    direction: dict[str, str] | None = None) -> dict:
    """Check aggregates against thresholds. ``hallucination`` is max-bounded."""
    direction = direction or {}
    failures = []
    for name, limit in thresholds.items():
        if name not in summary:
            failures.append({"metric": name, "reason": "not measured"})
            continue
        got = summary[name]["mean"]
        higher_is_worse = direction.get(name) == "lower"
        ok = got <= limit if higher_is_worse else got >= limit
        if not ok:
            failures.append({
                "metric": name,
                "mean": got,
                "threshold": limit,
                "want": f"<= {limit}" if higher_is_worse else f">= {limit}",
            })
    return {"ok": not failures, "failures": failures}
