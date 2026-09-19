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
