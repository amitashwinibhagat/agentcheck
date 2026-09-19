"""Live decision stream over Server-Sent Events."""

import asyncio

from fastapi import Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from agentcheck.routes import shared
from agentcheck.stream import sse_format


def register(app, store, bus, demo_mode):
    @app.get("/v1/stream")
    async def decision_stream(request: Request = None,
                              authorization: str | None = Header(None),
                              key: str | None = None):
        """Server-Sent Events: every saved result for this key, live.

        EventSource cannot set headers, so the browser passes the key as a
        query param. Locally that is the localhost convenience; in demo mode
        the demo key works from anywhere (it is capped, not admin).
        """
        if key and (shared.is_local(request) or demo_mode):
            key = key
            if store.lookup_key(key) is None:
                raise HTTPException(401, "unknown api key")
        else:
            key = shared.authorize(store, authorization)
        q = bus.subscribe(key)

        async def gen():
            try:
                yield sse_format({"kind": "hello", "key": "you"})
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield sse_format(msg)
            finally:
                bus.unsubscribe(key, q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
