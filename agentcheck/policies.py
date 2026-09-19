"""Decision Policies: the gate as an artifact, not a setting.

A policy is an ordered list of rules. First match wins. Each rule maps a
condition on the judge's output (verdict, confidence, severity) to a
decision: approve, human, or block.

This release is observe-and-recommend: the decision is computed, returned,
and logged, but nothing is actioned unless the caller passes enforce=true.
"""

from __future__ import annotations

import operator
import re

DECISIONS = ("approve", "human", "block")
FIELDS = ("verdict", "confidence", "severity")


class PolicyError(ValueError):
    pass


_OPS = {
    "<": operator.lt, "<=": operator.le,
    ">": operator.gt, ">=": operator.ge,
    "==": operator.eq, "!=": operator.ne,
}

_COND = re.compile(r"^(verdict|confidence|severity)\s*"
                   r"(==|!=|>=|<=|>|<)?\s*"
                   r"([A-Za-z_][\w-]*|-?\d+(?:\.\d+)?)$")


def parse_condition(expr):
    """Parse 'verdict == fail', 'confidence < 0.6', 'severity >= 2'."""
    m = _COND.match(str(expr).strip())
    if not m:
        raise PolicyError(f"cannot parse condition {expr!r}; expected "
                          f"field op value, e.g. 'confidence < 0.6'")
    field, op, value = m.group(1), m.group(2) or "==", m.group(3)
    if field == "verdict" and op not in ("==", "!="):
        raise PolicyError(f"verdict only supports == and !=, got {op!r}")
    if field in ("confidence", "severity"):
        try:
            value = float(value)
        except ValueError:
            raise PolicyError(f"{field} needs a number, got {value!r}") from None
    return field, op, value


def match(field, op, value, ctx):
    actual = ctx.get(field)
    if actual is None:
        return False
    if field == "verdict":
        if op not in ("==", "!="):
            raise PolicyError("verdict only supports == and !=")
        return (actual == value) if op == "==" else (actual != value)
    fn = _OPS[op]
    try:
        return bool(fn(float(actual), float(value)))
    except (TypeError, ValueError):
        return False


class Policy:
    """An ordered rule list. First match wins; default is required."""

    def __init__(self, spec):
        if not isinstance(spec, dict):
            raise PolicyError("policy must be a mapping")
        self.name = spec.get("name")
        if not self.name or not re.match(r"^[a-z][\w-]*$", self.name):
            raise PolicyError(f"bad or missing name: {spec.get('name')!r}")
        self.rubric = spec.get("rubric")
        rules = spec.get("rules")
        if not isinstance(rules, list) or not rules:
            raise PolicyError("rules must be a non-empty list")
        self.rules = []
        for r in rules:
            if not isinstance(r, dict) or "if" not in r or "then" not in r:
                raise PolicyError(f"each rule needs 'if' and 'then': {r!r}")
            if r["then"] not in DECISIONS:
                raise PolicyError(f"then must be one of {DECISIONS}, "
                                  f"got {r['then']!r}")
            self.rules.append((parse_condition(r["if"]), r["then"]))
        default = spec.get("default")
        if default not in DECISIONS:
            raise PolicyError(f"default must be one of {DECISIONS}, "
                              f"got {default!r}")
        self.default = default

    def decide(self, verdict, confidence=None, severity=None):
        """Return the decision for one judged item."""
        ctx = {"verdict": verdict, "confidence": confidence,
               "severity": severity}
        for (field, op, value), decision in self.rules:
            if match(field, op, value, ctx):
                return decision
        return self.default


def lint(spec):
    """Return a list of problems (empty = valid)."""
    problems = []
    try:
        Policy(spec)
    except PolicyError as e:
        problems.append(str(e))
    return problems


# ---------------------------------------------------------------------------
# File loading: ./policies, $AGENTCHECK_CHECKPOLICIES, $AGENTCHECK_HOME/policies
# ---------------------------------------------------------------------------

import os
from pathlib import Path

# The example policies that ship with the package.
SHIPPED_DIR = Path(__file__).resolve().parent / "policy_examples"


def policy_dirs():
    """Search order: env var, then $AGENTCHECK_HOME, then cwd, then the
    examples shipped in the package. First match wins, so a user's own
    policy always shadows a shipped example of the same name."""
    dirs = []
    env = os.environ.get("AGENTCHECK_CHECKPOLICIES")
    if env:
        dirs.extend(p for p in env.split(":") if p)
    home = os.environ.get("AGENTCHECK_HOME")
    if home:
        dirs.append(str(Path(home) / "policies"))
    dirs.append(str(Path.cwd() / "policies"))
    dirs.append(str(Path(__file__).resolve().parent / "policy_examples"))
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def load_policy_file(path):
    """Load and validate a policy YAML file. Raises PolicyError on problems."""
    try:
        import yaml
    except ImportError:
        raise PolicyError("YAML policies need PyYAML. Install it with: "
                          "pip install pyyaml") from None
    p = Path(path)
    try:
        text = p.read_text()
    except (OSError, UnicodeDecodeError) as e:
        # UnicodeDecodeError is a ValueError, not an OSError, so it used to
        # escape this handler and 500 the whole policy list.
        raise PolicyError(f"cannot read {p}: {e}") from None
    try:
        spec = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PolicyError(f"{p}: invalid YAML: {e}") from None
    problems = lint(spec or {})
    if problems:
        raise PolicyError(f"{p}: {'; '.join(problems)}")
    return spec


def find_policy(name):
    """Find a policy file by name across the search dirs. Returns the spec."""
    for d in policy_dirs():
        for ext in (".yaml", ".yml"):
            p = Path(d) / f"{name}{ext}"
            if p.exists():
                return load_policy_file(p)
    raise PolicyError(f"no policy named {name!r} in "
                      f"{', '.join(policy_dirs())}")


def all_policies():
    """All policy names on the search path, with lint status."""
    return all_policies_in(policy_dirs())


def all_policies_in(dirs):
    """All policy names in the given directories, with lint status.

    `dirs` is a parameter so the loader can be tested against a directory with
    deliberately broken files.
    """
    out = {}
    for d in dirs:
        dd = Path(d)
        if not dd.is_dir():
            continue
        for p in sorted(dd.iterdir()):
            if p.suffix not in (".yaml", ".yml") or p.name.startswith("."):
                # dotfiles are metadata (macOS "._name.yaml"), not policies
                continue
            name = p.stem
            if name in out:
                continue
            try:
                spec = load_policy_file(p)
                out[name] = {"name": name, "ok": True, "spec": spec,
                             "path": str(p)}
            except PolicyError as e:
                out[name] = {"name": name, "ok": False, "error": str(e),
                             "path": str(p)}
    return out


def simulate(policy, items):
    """Run a policy over judged items (dicts with verdict/confidence/severity).

    Returns the outcome split and the items that flip versus the default.
    """
    counts = {"approve": 0, "human": 0, "block": 0}
    decided = []
    for it in items:
        d = policy.decide(it.get("verdict"), it.get("confidence"),
                          it.get("severity"))
        counts[d] += 1
        decided.append(d)
    n = len(items) or 1
    return {
        "n": len(items),
        "approve": counts["approve"],
        "human": counts["human"],
        "block": counts["block"],
        "approve_pct": round(counts["approve"] / n * 100, 1),
        "human_pct": round(counts["human"] / n * 100, 1),
        "block_pct": round(counts["block"] / n * 100, 1),
        "decisions": decided,
    }
