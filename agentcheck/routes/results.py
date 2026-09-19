"""The verdict log: listing, reading, assessing, deleting results.

Moved out of web/__init__.py, where five API routes sat beside the static
file server with no stated rule for what lives where. The rule now: web/
serves the static client, routes/ serves the API. mount_web(app) shrank
accordingly.
"""

from fastapi import Header, HTTPException, Query
from pydantic import BaseModel

from agentcheck.routes import shared


class Feedback(BaseModel):
    assessment: str


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
    def feedback(result_id: str, body: Feedback, authorization: str | None = Header(None)):
        if body.assessment not in ('looks_correct', 'actual_issue', 'insufficient_context'):
            raise HTTPException(422, 'Choose a supported assessment')
        if not store.assess(shared.authorize(store, authorization), result_id, body.assessment):
            raise HTTPException(404, 'Result not found')
        return {'assessment': body.assessment}

    @app.delete('/v1/results/{result_id}', status_code=204)
    def delete(result_id: str, authorization: str | None = Header(None)):
        if not store.delete_result(shared.authorize(store, authorization), result_id):
            raise HTTPException(404, 'Result not found')
