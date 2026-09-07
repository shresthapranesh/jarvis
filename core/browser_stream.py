"""Live frames from the persistent browser, fanned out to whoever is watching.

`tools/browser.py` gives the agent a real browser; this is how a human sees
what it is doing there. CDP's own `Page.startScreencast` is the whole source —
Chrome already encodes JPEG frames for DevTools' device mode, so nothing here
captures a screen, and no video stack is involved.

**A second CDP client, deliberately.** The kernel holds its own connection to
the same browser (sync Playwright, one tab), and CDP accepts concurrent
clients, so the server attaching independently disturbs nothing. It also means
frames keep flowing between reads — the panel shows the browser's real state,
not only the instants the agent happened to be inside `fetch()`.

**Screencast runs only while someone watches.** `subscribe()` reference-counts:
the first subscriber starts the cast and the last one stops it, so a closed
panel costs no encoding. Frames are pushed into per-subscriber queues of depth
1 — a slow socket drops stale frames instead of applying backpressure to the
browser, which is the right trade for video nobody rewinds.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Small enough to stay smooth over a LAN, large enough to read text on the page.
FRAME_FORMAT = "jpeg"
FRAME_QUALITY = 60
FRAME_MAX_WIDTH = 1280
FRAME_MAX_HEIGHT = 800
EVERY_NTH_FRAME = 2  # ~halves the rate Chrome would otherwise push

_ATTACH_TIMEOUT = 15.0


@dataclass
class Frame:
    """One screencast frame plus what the client needs to place it."""

    data: bytes
    width: int
    height: int
    url: str = ""


# eq=False keeps the identity hash: subscribers live in a set and are the
# same subscriber only if they are the same object. The generated __eq__ would
# set __hash__ to None and make them unhashable.
@dataclass(eq=False)
class _Subscriber:
    queue: asyncio.Queue[Frame] = field(default_factory=lambda: asyncio.Queue(maxsize=1))

    def offer(self, frame: Frame) -> None:
        """Replace whatever is queued. Never blocks, never grows."""
        with contextlib.suppress(asyncio.QueueEmpty):
            self.queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(frame)


class BrowserScreencast:
    """Owns the server's CDP connection and the subscriber set."""

    def __init__(self) -> None:
        self._subscribers: set[_Subscriber] = set()
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._session: Any = None
        self._last: Frame | None = None

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def _attach(self) -> None:
        """Connect, find the agent's tab, and start the cast. Raises on failure."""
        from playwright.async_api import async_playwright

        from tools import browser as browser_tool

        # ensure_running() may launch a browser, which is blocking and slow.
        endpoint = await asyncio.to_thread(browser_tool.ensure_running)

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(endpoint)
        ctx = (
            self._browser.contexts[0]
            if self._browser.contexts
            else await self._browser.new_context()
        )
        # The same single tab tools/browser.py reuses, so the panel shows the
        # page the agent is actually on rather than a blank one of our own.
        self._page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        self._session = await ctx.new_cdp_session(self._page)
        self._session.on("Page.screencastFrame", self._on_frame)
        await self._session.send(
            "Page.startScreencast",
            {
                "format": FRAME_FORMAT,
                "quality": FRAME_QUALITY,
                "maxWidth": FRAME_MAX_WIDTH,
                "maxHeight": FRAME_MAX_HEIGHT,
                "everyNthFrame": EVERY_NTH_FRAME,
            },
        )
        logger.info("browser stream: casting from %s", endpoint)
        await self._prime()

    async def _prime(self) -> None:
        """Grab one frame immediately, outside the screencast.

        Screencast only emits on *paint*, so attaching to a page that is
        already sitting still yields nothing at all — the panel would show
        "waiting for the first frame" indefinitely while everything worked. A
        one-shot capture makes the first paint the panel's problem rather than
        the viewer's.
        """
        session = self._session
        if session is None:
            return
        try:
            shot = await session.send(
                "Page.captureScreenshot", {"format": FRAME_FORMAT, "quality": FRAME_QUALITY}
            )
            metrics = await session.send("Page.getLayoutMetrics")
        except Exception as exc:
            logger.debug("browser stream: priming frame failed: %s", exc)
            return
        viewport = metrics.get("cssVisualViewport") or {}
        frame = Frame(
            data=base64.b64decode(shot["data"]),
            width=int(viewport.get("clientWidth") or 0),
            height=int(viewport.get("clientHeight") or 0),
            url=getattr(self._page, "url", "") or "",
        )
        self._last = frame
        for sub in tuple(self._subscribers):
            sub.offer(frame)

    async def _detach(self) -> None:
        """Stop the cast and drop the connection. Never raises."""
        for step in (
            lambda: self._session.send("Page.stopScreencast"),
            lambda: self._browser.close(),
            lambda: self._playwright.stop(),
        ):
            with contextlib.suppress(Exception):
                target = step()
                if target is not None:
                    await target
        self._session = self._page = self._browser = self._playwright = None
        self._last = None
        logger.info("browser stream: stopped")

    def _on_frame(self, params: dict) -> None:
        """CDP callback. Ack first — Chrome stops casting until we do."""
        session = self._session
        if session is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().create_task(
                    session.send(
                        "Page.screencastFrameAck", {"sessionId": params["sessionId"]}
                    )
                )
        meta = params.get("metadata") or {}
        try:
            payload = base64.b64decode(params["data"])
        except (KeyError, ValueError):
            return
        frame = Frame(
            data=payload,
            width=int(meta.get("deviceWidth") or 0),
            height=int(meta.get("deviceHeight") or 0),
            url=getattr(self._page, "url", "") or "",
        )
        self._last = frame
        for sub in tuple(self._subscribers):
            sub.offer(frame)

    # ── Subscription ────────────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def subscribe(self):
        """Yield a `_Subscriber` whose queue receives frames while held.

        Starting on the first subscriber is what keeps a closed panel free; the
        attach happens under the lock so two sockets opening at once cannot
        race two CDP connections into existence.
        """
        sub = _Subscriber()
        async with self._lock:
            first = not self._subscribers
            self._subscribers.add(sub)
            if first:
                try:
                    await asyncio.wait_for(self._attach(), timeout=_ATTACH_TIMEOUT)
                except Exception:
                    self._subscribers.discard(sub)
                    await self._detach()
                    raise
        # A late joiner should not stare at nothing until the page next paints.
        if self._last is not None:
            sub.offer(self._last)
        try:
            yield sub
        finally:
            async with self._lock:
                self._subscribers.discard(sub)
                if not self._subscribers:
                    await self._detach()


_screencast: BrowserScreencast | None = None


def get_screencast() -> BrowserScreencast:
    global _screencast
    if _screencast is None:
        _screencast = BrowserScreencast()
    return _screencast


async def shutdown_screencast() -> None:
    """Drop the CDP connection at server shutdown. Safe if never started."""
    global _screencast
    if _screencast is not None:
        await _screencast._detach()
        _screencast = None
