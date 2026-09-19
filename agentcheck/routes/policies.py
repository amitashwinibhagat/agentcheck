"""Decision policies found on disk, with lint status."""

from fastapi import Header

from agentcheck import policies
from agentcheck.routes import shared


def register(app, store):
    @app.get("/v1/policies")
    def list_policies(authorization: str | None = Header(None)):
        """Policies found on disk with their lint status."""
        shared.authorize(store, authorization)
        found = policies.all_policies()
        return {"policies": [
            {"name": n, "ok": i["ok"], "error": i.get("error"),
             "rubric": (i.get("spec") or {}).get("rubric"),
             "rules": (i.get("spec") or {}).get("rules"),
             "default": (i.get("spec") or {}).get("default"),
             "path": i["path"]}
            for n, i in sorted(found.items())]}
