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
        """Eval reports found under ./evals and $AGENTCHECK_HOME/evals.

        Paths are reported RELATIVE to their root. Absolute paths disclose the
        host's layout and the operator's username — reconnaissance for the next
        attempt, and nothing a caller needs to act on.
        """
        shared.authorize(store, authorization)
        seen = []
        for label, d in (("cwd", Path.cwd() / "evals"),
                         ("home", Path(os.environ.get("AGENTCHECK_HOME")
                                        or (Path.home() / ".agentcheck")) / "evals")):
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.json")):
                try:
                    rep = json.loads(f.read_text())
                except Exception:
                    continue
                if rep.get("kind") != "agentcheck.eval-report":
                    continue
                seen.append({"path": f"{label}/{f.name}", "name": rep.get("name"),
                             "ran_at": rep.get("ran_at"), "split": rep.get("split"),
                             "cells": len(rep.get("cells", [])),
                             "report": rep})
        return {"reports": seen}

    @app.get("/v1/datasets")
    def list_datasets(authorization: str | None = Header(None)):
        """Registered datasets: names and counts, never host paths."""
        shared.authorize(store, authorization)
        return {"datasets": ds.inventory()}
