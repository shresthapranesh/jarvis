"""In-app server log viewer.

Two localhost-only endpoints back the ``/logs`` page in the web UI:

* ``GET /server-logs`` returns a snapshot of the recent in-memory log buffer.
* ``GET /server-logs/stream`` streams every new log record over SSE,
  prefixed by one ``backfill`` event that carries the current snapshot.

The path is ``/server-logs`` (not ``/logs``) so it doesn't collide with
the SPA route at ``/logs`` — same disambiguation as ``/task-runs`` vs
the frontend ``/tasks`` page.

Records are produced by ``core.log_setup.BroadcastHandler``, which is
attached to the root logger by ``setup_logging``. Each record carries
``{ts, level, logger, message}``.

The endpoints reject any caller whose ``request.client.host`` is not a
loopback address. There is no token auth — if the server ever binds to a
non-localhost interface, this guard is what keeps logs from leaking.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from core.log_setup import get_broadcast_handler


router = APIRouter()


_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _require_localhost(request: Request) -> JSONResponse | None:
    host = (request.client.host if request.client else "") or ""
    if host not in _LOCAL_HOSTS:
        return JSONResponse({"error": "logs endpoint is localhost-only"}, status_code=403)
    return None


@router.get("/server-logs")
async def list_logs(request: Request) -> JSONResponse:
    if (err := _require_localhost(request)) is not None:
        return err
    return JSONResponse({"logs": get_broadcast_handler().snapshot()})


@router.get("/server-logs/stream", response_model=None)
async def stream_logs(request: Request) -> EventSourceResponse | JSONResponse:
    if (err := _require_localhost(request)) is not None:
        return err

    handler = get_broadcast_handler()

    async def generate() -> AsyncIterator[dict]:
        queue = handler.subscribe()
        try:
            yield {"event": "backfill", "data": json.dumps(handler.snapshot())}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Heartbeat keeps the connection alive across idle gaps.
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": "log", "data": json.dumps(payload)}
        finally:
            handler.unsubscribe(queue)

    return EventSourceResponse(generate())
