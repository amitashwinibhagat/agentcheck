"""The verdict log: listing, reading, assessing, deleting results.

Moved out of web/__init__.py, where five API routes sat beside the static
file server with no stated rule for what lives where. The rule now: web/
serves the static client, routes/ serves the API. mount_web(app) shrank
accordingly.
"""

from fastapi import Header, HTTPException, Query, Request
from pydantic import BaseModel

from agentcheck.routes import shared


class Feedback(BaseModel):
    assessment: str
    #: The abstain gate for the running calibration. Sent by the client so the
    #: HUD matches the tier the user is building.
    gate: float = 0.6


def register(app, store):
    @app.get('/v1/results')
    def results(authorization: str | None = Header(None),
                verdict: str | None = Query(default=None)):
        key = shared.authorize(store, authorization)
        if verdict is not None and verdict not in ('pass', 'review', 'fail'):
            raise HTTPException(422, "verdict must be pass, review, or fail")
        return {
            'results': store.results(key, verdict=verdict),
            'counts': store.result_counts(key),
        }

    @app.get('/v1/results/{result_id}')
    def result(result_id: str, authorization: str | None = Header(None)):
        row = store.result(shared.authorize(store, authorization), result_id)
        if row is None:
            raise HTTPException(404, 'Result not found')
        return row

    @app.patch('/v1/results/{result_id}')
    def feedback(result_id: str, body: Feedback, request: Request,
                 authorization: str | None = Header(None)):
        if body.assessment not in ('looks_correct', 'actual_issue', 'insufficient_context'):
            raise HTTPException(422, 'Choose a supported assessment')
        key = shared.authorize(store, authorization)
        if not store.assess(key, result_id, body.assessment):
            raise HTTPException(404, 'Result not found')
        # Hand back the calibration so far. Labeling 30 calls is the path to a
        # measured tier, and a number that only appears at item 30 gives no
        # reason to keep going; the running ECE is the progress bar.
        try:
            from agentcheck import calibration as cal
            rows = store.signoffs(key)
            rep = cal.report_from_signoffs(
                rows, getattr(request.app.state, "judge", "unknown"),
                checkset="safety" if not rows else (rows[0].get("checkset") or "safety"),
                gate=body.gate)
            counts = store.signoff_counts(key, gate=body.gate)
            running = {
                "decided": rep["decided"]["n"],
                "signal": counts["signal"],
                "needed": 30,
                "remaining": max(30 - rep["decided"]["n"], 0),
                "ece": rep["decided"]["ece"],
                "accuracy": rep["decided"]["accuracy"],
                "mean_confidence": rep["decided"]["mean_confidence"],
                "read": rep["read"],
            }
        except Exception:
            # The assessment is saved; a running number must never fail it.
            running = None
        return {'assessment': body.assessment, 'calibration': running}

    @app.delete('/v1/results/{result_id}', status_code=204)
    def delete(result_id: str, authorization: str | None = Header(None)):
        if not store.delete_result(shared.authorize(store, authorization), result_id):
            raise HTTPException(404, 'Result not found')
