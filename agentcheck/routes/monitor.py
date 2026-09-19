"""Judgment volume, verdict mix and confidence over time."""

from fastapi import Header, HTTPException

from agentcheck import monitor as mon
from agentcheck.routes import shared


def register(app, store):
    @app.get("/v1/monitor")
    async def monitor_timeline(authorization: str | None = Header(None),
                               bucket: str = "day", days: int = 30):
        key = shared.authorize(store, authorization)
        if bucket not in ("hour", "day", "week"):
            raise HTTPException(422, "bucket must be hour, day or week")
        if not 1 <= days <= 3650:
            raise HTTPException(422, "days must be between 1 and 3650")
        return mon.timeline(store, key, bucket=bucket, since_days=days)

    @app.get("/v1/monitor/drift")
    async def monitor_drift(authorization: str | None = Header(None),
                            bucket: str = "day", days: int = 30):
        key = shared.authorize(store, authorization)
        return mon.drift(store, key, bucket=bucket, since_days=days)
