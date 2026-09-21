"""agentcheck — calibrated verification for agent tool calls."""

# Read from the installed distribution so the version cannot drift from
# pyproject.toml (it did: a literal here said 0.1.0 while the package was
# 0.2.0, and `doctor` reported the stale one). Two names are tried because the
# distribution was renamed to agentcheck-verify and an older editable install
# is still called agentcheck; reporting whichever is actually installed is the
# honest answer. The fallback covers a source checkout that was never
# installed at all.
try:  # pragma: no cover - trivial
    from importlib.metadata import PackageNotFoundError, version as _dist_version

    def _resolve_version() -> str:
        for dist in ("agentcheck-verify", "agentcheck"):
            try:
                return _dist_version(dist)
            except PackageNotFoundError:
                continue
        return "0.0.0+local"

    __version__ = _resolve_version()
except Exception:  # pragma: no cover
    __version__ = "0.0.0+unknown"

from agentcheck import checks  # noqa: F401
from agentcheck.judges import get_judge  # noqa: F401
from agentcheck.observe import observe  # noqa: F401
