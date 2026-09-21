"""The rubric catalogue: built-in plus YAML on disk."""

from fastapi import Header

from agentcheck import checks as check_lib
from agentcheck.routes import shared


def yaml_dirs():
    """Where rubrics are looked for, for display in the UI."""
    from agentcheck.checks.yaml_checksets import search_dirs
    return search_dirs()


def _display_path(p) -> str:
    """A search directory as something a caller can act on.

    Absolute paths leak the host layout and the operator's username to any
    authenticated caller. `~/…` and `$AGENTCHECK_HOME/…` are the same
    information phrased as instructions instead of reconnaissance.
    """
    import os
    from pathlib import Path
    home = Path(os.environ.get("AGENTCHECK_HOME") or (Path.home() / ".agentcheck"))
    try:
        rel = Path(p).resolve().relative_to(home.resolve())
        return f"$AGENTCHECK_HOME/{rel}"
    except Exception:
        return str(p).replace(str(Path.home()), "~")


def register(app, store):
    @app.get("/v1/checksets")
    def list_checksets(authorization: str | None = Header(None)):
        """Every rubric available to this process: built-in plus YAML on disk.

        Authenticated on purpose: a rubric is the customer's own evaluation
        criteria and can encode how their business is checked.
        """
        shared.authorize(store, authorization)
        out = []
        for name in check_lib.all_names():
            try:
                out.append(check_lib.describe(name))
            except Exception as e:  # a broken rubric must not break the list
                out.append({"name": name, "error": str(e)})
        return {"checksets": out, "problems": check_lib.dsl.YAML_PROBLEMS,
                "search_paths": [_display_path(p) for p in yaml_dirs()]}

    @app.post("/v1/checksets/reload")
    def reload_checksets(authorization: str | None = Header(None)):
        """Re-scan the rubric directories without restarting the server."""
        shared.authorize(store, authorization)
        check_lib.yaml_sets(reload=True)
        return {"checksets": check_lib.all_names(),
                "problems": check_lib.dsl.YAML_PROBLEMS}
