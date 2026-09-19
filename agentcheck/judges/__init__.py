"""Judge registry. Adding a provider = one adapter file + one line here."""

from __future__ import annotations

from typing import Callable

from agentcheck.judges.base import Judge

_BUILDERS: dict[str, Callable[[], Judge]] = {}


def register(name: str, builder: Callable[[], Judge]) -> None:
    _BUILDERS[name] = builder


def get_judge(name: str | None) -> Judge:
    if not name:
        name = "typesafe"
    if name not in _BUILDERS:
        raise KeyError(f"unknown judge {name!r}; registered {sorted(_BUILDERS)}")
    return _BUILDERS[name]()


def available() -> list[str]:
    return sorted(_BUILDERS)


def _register() -> None:
    from agentcheck.judges.stub import StubJudge
    register("stub", StubJudge)
    try:
        from agentcheck.judges.typesafe import TypeSafeJudge
        register("typesafe", TypeSafeJudge)
    except Exception:
        pass


_register()
