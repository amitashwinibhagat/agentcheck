"""Checksets as rubric files, not code.

A checkset is what a customer actually buys, so defining one should not require
editing Python. A rubric is a YAML file:

    name: refund-policy
    description: Does an agent handle a refund request within policy?
    verdict: verdict          # which check id is the overall verdict
    severity: severity        # optional
    checks:
      - id: within_window
        type: noul
        instructions: >
          Was the order placed within the 30-day return window?
      - id: decision
        type: choice
        instructions: How should this request be handled?
        criteria:
          approve: refund the order
          escalate: needs a human decision
          deny: outside policy
      - id: severity
        type: score
        instructions: How bad is a wrong answer here?
        criteria: [trivial, annoying, costly]

Discovery order (first match wins, builtins cannot be shadowed):
  1. the built-in ``safety`` set
  2. $AGENTCHECK_CHECKSETS  (colon-separated directories)
  3. ./checksets
  4. $AGENTCHECK_HOME/checksets   (default ~/.agentcheck/checksets)
  5. bundled examples in agentcheck/checksets/
"""

from __future__ import annotations

import os
from pathlib import Path

from agentcheck.checks.dsl import CheckSet
from agentcheck.judges.base import Question, choice, noul, score

QUESTION_TYPES = ("noul", "choice", "score")
PACKAGED_DIR = Path(__file__).resolve().parent.parent / "checksets"


class RubricError(ValueError):
    """A rubric file that cannot be turned into a checkset."""


def _yaml():
    try:
        import yaml
    except ModuleNotFoundError as e:  # pragma: no cover
        raise RubricError(
            "YAML rubrics need PyYAML. Install it with: pip install pyyaml"
        ) from e
    return yaml


def parse_rubric(text: str, source: str = "<string>") -> CheckSet:
    """Parse YAML text into a CheckSet. Raises RubricError with the reason."""
    yaml = _yaml()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        # The docstring promises RubricError. A raw yaml.ParserError used to
        # escape and take down the caller's whole rubric list.
        raise RubricError(f"{source}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise RubricError(f"{source}: expected a mapping at the top level")
    name = str(data.get("name") or "").strip()
    if not name:
        raise RubricError(f"{source}: 'name' is required")
    raw = data.get("checks")
    if not isinstance(raw, list) or not raw:
        raise RubricError(f"{source}: 'checks' must be a non-empty list")

    checks: list[Question] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        where = f"{source}: checks[{i}]"
        if not isinstance(item, dict):
            raise RubricError(f"{where}: expected a mapping")
        qid = str(item.get("id") or "").strip()
        if not qid:
            raise RubricError(f"{where}: 'id' is required")
        if qid in seen:
            raise RubricError(f"{where}: duplicate id {qid!r}")
        seen.add(qid)
        qtype = str(item.get("type") or "").strip().lower()
        if qtype not in QUESTION_TYPES:
            raise RubricError(
                f"{where}: 'type' must be one of {QUESTION_TYPES}, got {qtype!r}")
        instructions = str(item.get("instructions") or "").strip()
        if not instructions:
            raise RubricError(f"{where}: 'instructions' is required")
        criteria = item.get("criteria")

        if qtype == "noul":
            checks.append(noul(qid, instructions))
        elif qtype == "choice":
            if not isinstance(criteria, dict) or not criteria:
                raise RubricError(
                    f"{where}: a choice needs 'criteria' as a mapping of "
                    "option -> description")
            opts = {str(k): str(v) for k, v in criteria.items()}
            checks.append(choice(qid, instructions, opts))
        else:
            if not isinstance(criteria, list) or not criteria:
                raise RubricError(
                    f"{where}: a score needs 'criteria' as an ordered list of "
                    "levels, low to high")
            checks.append(score(qid, instructions, [str(x) for x in criteria]))

    cs = CheckSet(name=name, checks=checks)
    cs.description = str(data.get("description") or "").strip()
    cs.verdict_check = _resolve_role(data, "verdict", checks, "choice", source)
    cs.severity_check = _resolve_role(data, "severity", checks, "score", source)
    return cs


def _resolve_role(data: dict, role: str, checks: list[Question],
                  want_type: str, source: str) -> str | None:
    """Resolve the verdict/severity pointer, defaulting to a same-typed single."""
    declared = data.get(role)
    by_id = {c.id: c for c in checks}
    if declared is not None:
        qid = str(declared).strip()
        if qid not in by_id:
            raise RubricError(f"{source}: {role} {qid!r} is not one of the check ids")
        if by_id[qid].type != want_type:
            raise RubricError(
                f"{source}: {role} {qid!r} is a {by_id[qid].type}, expected {want_type}")
        return qid
    same = [c.id for c in checks if c.type == want_type]
    if role == "verdict":
        if len(same) != 1:
            raise RubricError(
                f"{source}: needs exactly one {want_type} check for 'verdict', "
                f"found {len(same)}; add 'verdict: <id>' to disambiguate")
        return same[0]
    return same[0] if len(same) == 1 else None


def load_file(path: str | Path) -> CheckSet:
    p = Path(path)
    try:
        text = p.read_text()
    except (OSError, UnicodeDecodeError) as e:
        # UnicodeDecodeError is a ValueError, not an OSError, so it slipped
        # past this handler and 500'd the endpoint on a binary file.
        raise RubricError(f"cannot read {p}: {e}") from e
    return parse_rubric(text, str(p))


# The rubrics that ship with the package (agentcheck/checksets).
SHIPPED_DIR = PACKAGED_DIR


def search_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("AGENTCHECK_CHECKSETS")
    if env:
        dirs += [Path(x).expanduser() for x in env.split(os.pathsep) if x.strip()]
    dirs.append(Path.cwd() / "checksets")
    home = os.environ.get("AGENTCHECK_HOME") or (Path.home() / ".agentcheck")
    dirs.append(Path(home).expanduser() / "checksets")
    dirs.append(PACKAGED_DIR)
    return dirs


def discover(builtins: set[str] | None = None) -> tuple[dict[str, CheckSet], list[str]]:
    """Every rubric on the search path, as (sets, problems)."""
    return discover_in(search_dirs(), builtins=builtins)


def discover_in(dirs, builtins: set[str] | None = None,
                ) -> tuple[dict[str, CheckSet], list[str]]:
    """Find every rubric in the given directories. Returns (sets, problems).

    A rubric whose name collides with a built-in is skipped and reported rather
    than silently overriding the shipped set. `dirs` is a parameter so the
    loader can be tested against a directory containing deliberately broken
    files, without touching the real search path.
    """
    builtins = builtins or set()
    found: dict[str, CheckSet] = {}
    problems: list[str] = []
    for d in [Path(x) for x in dirs]:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.y*ml")):
            if f.name.startswith("."):
                # Dotfiles are metadata, never a rubric. macOS writes an
                # AppleDouble "._foo.yaml" beside every "foo.yaml", and those
                # are binary — reading one raised UnicodeDecodeError and took
                # down the entire rubric list with a 500.
                continue
            try:
                cs = load_file(f)
            except (RubricError, OSError, UnicodeDecodeError) as e:
                # One bad file must never hide the rest. Report and continue.
                problems.append(f"{f}: {e}")
                continue
            if cs.name in builtins:
                problems.append(
                    f"{f}: name {cs.name!r} is a built-in set; skipped "
                    "(rename the rubric to override nothing)")
                continue
            if cs.name in found:
                problems.append(f"{f}: duplicate rubric name {cs.name!r}; kept the first")
                continue
            found[cs.name] = cs
    return found, problems
