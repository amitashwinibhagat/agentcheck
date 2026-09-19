"""Judge interface — the one abstraction that keeps you from being owned by a vendor.

Every judgment provider (TypeSafe, a local model, another LLM) implements this.
Product code talks to a Judge, never to api.typesafe.ai.

The Choice/Score criteria inconsistency is normalised here: build one Question
object, the adapter worries about whether the wire format wants a map or a list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, Sequence, Mapping, Any

QuestionType = Literal["noul", "choice", "score"]


@dataclass(frozen=True)
class Question:
    """A single atomic judgment. Maps 1:1 onto a TypeSafe primitive."""

    id: str
    type: QuestionType
    instructions: str
    # Noul: ignored. Choice: {name: description}. Score: ordered levels, low→high.
    criteria: Mapping[str, str] | Sequence[str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Serialise to the TypeSafe wire format, normalising the criteria shape."""
        body: dict[str, Any] = {
            "type": self.type,
            "instructions": self.instructions,
        }
        if self.type == "choice":
            body["criteria"] = dict(self.criteria)  # map
        elif self.type == "score":
            body["criteria"] = list(self.criteria)  # list — different from Choice
        return body


@dataclass(frozen=True)
class Answer:
    """A normalised answer. `confidence` is always present; `probability` only for noul."""

    question_id: str
    type: QuestionType
    value: float | str  # noul: 0..1 probability, choice: label, score: numeric
    confidence: float
    probabilities: Mapping[Any, float] = field(default_factory=dict)

    @property
    def noul(self) -> float:
        assert self.type == "noul"
        return float(self.value)  # type: ignore[arg-type]

    @property
    def choice(self) -> str:
        assert self.type == "choice"
        return str(self.value)

    @property
    def score(self) -> float:
        assert self.type == "score"
        return float(self.value)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Judgment:
    """One judge call: many questions answered in parallel, plus the metering data."""

    answers: Sequence[Answer]
    request_id: str | None  # reconciliation key against a vendor invoice
    input_tokens: int
    output_tokens: int
    server_ms: float | None  # vendor's own reported service time
    model: str
    cached: bool = False


class Judge(Protocol):
    """A provider that answers atomic questions about state."""

    name: str

    def ask(self, state: Any, questions: Sequence[Question]) -> Judgment: ...


# --- builders: the only constructors product code should need -----------------

def noul(qid: str, instructions: str) -> Question:
    return Question(id=qid, type="noul", instructions=instructions)


def choice(qid: str, instructions: str, options: Mapping[str, str]) -> Question:
    return Question(id=qid, type="choice", instructions=instructions, criteria=options)


def score(qid: str, instructions: str, levels: Sequence[str]) -> Question:
    return Question(id=qid, type="score", instructions=instructions, criteria=levels)
