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

Access is gated by two checks:

1. ``request.client.host`` must be a loopback IP. This rejects anyone
   connecting from another machine when the server binds to a non-loopback
   interface. **Caveat:** if the server is ever deployed behind a reverse
   proxy (nginx, Caddy, Traefik), the TCP peer becomes the proxy itself
   and this check admits the world. Re-evaluate before that deployment.
2. If an ``Origin`` header is present (i.e. the request is a CORS fetch
   from a webpage), it must be a loopback origin. The app's global
   ``CORSMiddleware`` uses ``allow_origins=["*"]``, which by itself would
   let any visited website ``fetch('http://localhost:8000/server-logs')``
   from the user's browser and exfiltrate the buffer. This second check
   blocks that vector without touching the global CORS config.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from core.log_setup import get_broadcast_handler


router = APIRouter()


def _is_loopback_host(host: str) -> bool:
    """True for 127.0.0.0/8, ::1, and ::ffff:127.0.0.1-style mappings."""
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_origin(origin: str) -> bool:
    """True for an Origin header that points back at this machine."""
    # Same-origin SSE/fetch from the UI uses http://localhost:5173 (vite dev)
    # or http://localhost:8000 / http://127.0.0.1:8000 (prod). Anything else
    # is a cross-origin fetch and must be refused — see module docstring.
    return origin.startswith((
        "http://localhost",
        "http://127.0.0.1",
        "http://[::1]",
    ))


def _require_localhost(request: Request) -> JSONResponse | None:
    host = (request.client.host if request.client else "") or ""
    if not _is_loopback_host(host):
        return JSONResponse({"error": "logs endpoint is localhost-only"}, status_code=403)
    origin = request.headers.get("origin", "")
    if origin and not _is_loopback_origin(origin):
        return JSONResponse({"error": "cross-origin not allowed"}, status_code=403)
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
