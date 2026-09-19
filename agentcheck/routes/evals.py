"""Eval reports and datasets found on disk."""

import json
import os
from pathlib import Path

from fastapi import Header

from agentcheck.evals import datasets as ds
from agentcheck.routes import shared


def register(app, store):
    @app.get("/v1/evals")
    def list_evals(authorization: str | None = Header(None)):
        """Eval reports found under ./evals and $AGENTCHECK_HOME/evals."""
        shared.authorize(store, authorization)
        seen = []
        for d in (Path.cwd() / "evals", Path(os.environ.get("AGENTCHECK_HOME")
                                             or (Path.home() / ".agentcheck")) / "evals"):
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.json")):
                try:
                    rep = json.loads(f.read_text())
                except Exception:
                    continue
                if rep.get("kind") != "agentcheck.eval-report":
                    continue
                seen.append({"path": str(f), "name": rep.get("name"),
                             "ran_at": rep.get("ran_at"), "split": rep.get("split"),
                             "cells": len(rep.get("cells", [])),
                             "report": rep})
        return {"reports": seen}

    @app.get("/v1/datasets")
    def list_datasets(authorization: str | None = Header(None)):
        shared.authorize(store, authorization)
        return {"datasets": ds.inventory(), "root": str(ds.root())}
