"""WebSocket: live frames from the agent's browser.

REST/WS rather than a GraphQL subscription for the reason the carve-out exists
— these are JPEG frames, and graphql-ws is JSON. Base64 through a subscription
would inflate every frame by a third and put video on the same socket as the
token stream.

**Protocol** (client ⇄ server, one socket):
  server → client  binary            one JPEG frame
  server → client  {"type":"meta"}   frame size + current page URL, on change
  server → client  {"type":"status"} "live" | "unavailable", with a reason
  client → server  {"type":...}      accepted and currently ignored

That last line is deliberate. The panel is view-only today, but the input
channel exists from the first version so adding click/type forwarding later is
a new message type rather than a new transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.browser_stream import get_screencast

logger = logging.getLogger(__name__)

router = APIRouter()

# Long enough that an idle browser doesn't churn, short enough that a dead
# socket is noticed rather than pinning a subscriber slot forever.
_IDLE_TIMEOUT = 30.0


async def _drain_client(websocket: WebSocket) -> None:
    """Consume client→server messages so the socket registers a disconnect.

    Nothing acts on them yet (the panel is view-only), but a socket nobody
    reads from cannot observe the client going away, and the subscriber would
    outlive the panel that opened it.
    """
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while True:
            await websocket.receive_text()


@router.websocket("/ws/browser")
async def browser_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    screencast = get_screencast()

    try:
        async with screencast.subscribe() as sub:
            await websocket.send_json({"type": "status", "state": "live"})
            reader = asyncio.create_task(_drain_client(websocket))
            last_meta: tuple[int, int, str] | None = None
            try:
                while True:
                    if reader.done():  # client hung up
                        return
                    try:
                        frame = await asyncio.wait_for(
                            sub.queue.get(), timeout=_IDLE_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        # A still page produces no frames; prove the socket is
                        # alive rather than letting the client guess.
                        await websocket.send_json({"type": "idle"})
                        continue
                    meta = (frame.width, frame.height, frame.url)
                    if meta != last_meta:
                        last_meta = meta
                        await websocket.send_json(
                            {
                                "type": "meta",
                                "width": frame.width,
                                "height": frame.height,
                                "url": frame.url,
                            }
                        )
                    await websocket.send_bytes(frame.data)
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
    except WebSocketDisconnect:
        return
    except Exception as exc:
        # No browser installed, no display, nothing listening on the CDP port —
        # all the same to the panel: say so instead of dropping the socket, so
        # it can render the reason rather than a spinner.
        logger.info("browser stream unavailable: %s", exc)
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {"type": "status", "state": "unavailable", "reason": str(exc)}
            )
        with contextlib.suppress(Exception):
            await websocket.close()
