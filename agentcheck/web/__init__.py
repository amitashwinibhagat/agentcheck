"""Local web workspace. Customer keys stay in browser memory, not localStorage."""
from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent


def mount_web(app):
    app.mount('/assets', StaticFiles(directory=ROOT / 'static'), name='assets')

    @app.get('/', include_in_schema=False)
    def workspace():
        return FileResponse(ROOT / 'static' / 'index.html', headers={'Cache-Control': 'no-store'})

    @app.get('/start', include_in_schema=False)
    def start():
        # The conversion surface. Kept off `/` so the demo and the local
        # workspace still open on the product, not a brochure.
        return FileResponse(ROOT / 'static' / 'start.html',
                            headers={'Cache-Control': 'no-store'})
