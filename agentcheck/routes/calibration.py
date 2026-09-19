"""On-demand calibration over a labeled dataset."""

import asyncio

from fastapi import Header, HTTPException

from agentcheck import calibration as cal
from agentcheck.evals import datasets as ds
from agentcheck.routes import shared


def register(app, store, default_judge):
    @app.get("/v1/calibration")
    async def calibration_report(authorization: str | None = Header(None),
                                 dataset: str = "seed",
                                 checkset: str = "safety",
                                 gate: float = 0.6, bins: int = 10):
        shared.authorize(store, authorization)
        shared.get_checkset(checkset)
        try:
            rows = ds.load(dataset)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        if not rows:
            raise HTTPException(422, f"dataset {dataset!r} is empty")
        loop = asyncio.get_running_loop()
        try:
            rep = await loop.run_in_executor(
                None, lambda: cal.calibration(default_judge, rows,
                                             checkset=checkset, gate=gate,
                                             bins=bins))
        except Exception as e:
            raise HTTPException(502, f"calibration failed: {e}")
        rep["dataset"] = dataset
        rep["read"] = cal.verdict_for_ece(rep["decided"]["ece"],
                                         rep["decided"]["n"])
        return rep
