"""Local web workspace. Customer keys stay in browser memory, not localStorage."""
from pathlib import Path

from fastapi import Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).parent


class Feedback(BaseModel):
    assessment: str


def mount_web(app, store, authorize):
    app.mount('/assets', StaticFiles(directory=ROOT / 'static'), name='assets')

    @app.get('/', include_in_schema=False)
    def workspace():
        return FileResponse(ROOT / 'static' / 'index.html', headers={'Cache-Control': 'no-store'})

    @app.get('/v1/results')
    def results(authorization: str | None = Header(None),
                verdict: str | None = Query(default=None)):
        key = authorize(authorization)
        if verdict is not None and verdict not in ('pass', 'review', 'fail'):
            raise HTTPException(422, "verdict must be pass, review, or fail")
        return {
            'results': store.results(key, verdict=verdict),
            'counts': store.result_counts(key),
        }

    @app.get('/v1/results/{result_id}')
    def result(result_id: str, authorization: str | None = Header(None)):
        row = store.result(authorize(authorization), result_id)
        if row is None:
            raise HTTPException(404, 'Result not found')
        return row

    @app.patch('/v1/results/{result_id}')
    def feedback(result_id: str, body: Feedback, authorization: str | None = Header(None)):
        if body.assessment not in ('looks_correct', 'actual_issue', 'insufficient_context'):
            raise HTTPException(422, 'Choose a supported assessment')
        if not store.assess(authorize(authorization), result_id, body.assessment):
            raise HTTPException(404, 'Result not found')
        return {'assessment': body.assessment}

    @app.delete('/v1/results/{result_id}', status_code=204)
    def delete(result_id: str, authorization: str | None = Header(None)):
        if not store.delete_result(authorize(authorization), result_id):
            raise HTTPException(404, 'Result not found')
