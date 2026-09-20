"""Judge registry. Adding a provider = one adapter file + one line here."""

from __future__ import annotations

import os
from typing import Callable

from agentcheck.judges.base import Judge

_BUILDERS: dict[str, Callable[[], Judge]] = {}


def register(name: str, builder: Callable[[], Judge]) -> None:
    _BUILDERS[name] = builder


def get_judge(name: str | None) -> Judge:
    if not name:
        name = default_name()
    if name not in _BUILDERS:
        raise KeyError(f"unknown judge {name!r}; registered {sorted(_BUILDERS)}")
    return _BUILDERS[name]()


def available() -> list[str]:
    return sorted(_BUILDERS)


def default_name() -> str:
    """Which judge a keyless configuration SHOULD use.

    BYOK order: whoever's key is present wins. TypeSafe is the hosted
    default; an OpenAI-compatible key is the bring-your-own path; the stub
    answers last so a keyless container still boots (and says so)."""
    env = (os.environ.get("AGENTCHECK_JUDGE") or "").strip()
    if env:
        return env
    if os.environ.get("TYPESAFE_API_KEY"):
        return "typesafe"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "stub"


def _register() -> None:
    from agentcheck.judges.stub import StubJudge
    register("stub", StubJudge)
    try:
        from agentcheck.judges.typesafe import TypeSafeJudge
        register("typesafe", TypeSafeJudge)
    except Exception:
        pass
    try:
        from agentcheck.judges.openai import OpenAIJudge
        register("openai", OpenAIJudge)
    except Exception:
        pass


_register()
