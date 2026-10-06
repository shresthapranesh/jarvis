"""The live browser view the edge serves (`edge/src/browser/`, `/ws/browser`).

A fake DevTools browser stands in for Chrome: it speaks enough CDP for the
cast (auto-attach, the page domain, screencast, screenshot) and records every
call, so what the edge asks the browser for is asserted along with what the
panel gets. Python's route attaches through Playwright, which a fake this small
can't satisfy, so these run the edge alone; the refusal reasons are diffed
against `tools/browser.py`.

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect

from edge_support import _free_port, _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class FakeBrowser:
    """Two tabs, S1 first in auto-attach order; a service worker ahead of both."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.calls: list[dict] = []
        self.connections = 0
        self.disconnects = 0
        self.socket: WebSocket | None = None
        self.app = Starlette(routes=[
            Route("/json/version", self._version),
            Route("/json/list", self._list),
            WebSocketRoute("/devtools/browser/fake", self._ws),
        ])

    async def _version(self, request):
        return JSONResponse({"Browser": "Fake/1.0", "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/fake"})

    async def _list(self, request):
        return JSONResponse([{"type": "page", "id": "T1"}, {"type": "page", "id": "T2"}])

    async def _ws(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections += 1
        self.socket = ws
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                self.calls.append(msg)
                result: dict = {}
                method = msg["method"]
                if method == "Target.setAutoAttach":
                    for target, kind, session in (("W", "service_worker", "SW"), ("T1", "page", "S1"), ("T2", "page", "S2")):
                        await self._send(ws, "Target.attachedToTarget", {
                            "sessionId": session,
                            "targetInfo": {"targetId": target, "type": kind, "url": f"https://{target.lower()}.test/"},
                        })
                elif method == "Page.getFrameTree":
                    result = {"frameTree": {"frame": {"id": "F1", "url": "https://example.com/start", "urlFragment": "#top"}}}
                elif method == "Page.captureScreenshot":
                    result = {"data": _b64(b"primed")}
                elif method == "Page.getLayoutMetrics":
                    result = {"cssVisualViewport": {"clientWidth": 800, "clientHeight": 600.7}}
                reply = {"id": msg["id"], "result": result}
                if "sessionId" in msg:
                    reply["sessionId"] = msg["sessionId"]
                await ws.send_text(json.dumps(reply))
        except WebSocketDisconnect:
            self.disconnects += 1

    @staticmethod
    async def _send(ws: WebSocket, method: str, params: dict, session: str | None = None) -> None:
        msg: dict = {"method": method, "params": params}
        if session:
            msg["sessionId"] = session
        await ws.send_text(json.dumps(msg))

    async def event(self, method: str, params: dict, session: str | None = "S1") -> None:
        assert self.socket is not None
        await self._send(self.socket, method, params, session)

    def sent(self, method: str) -> list[dict]:
        return [c for c in self.calls if c["method"] == method]


@pytest.fixture
async def browser():
    fake = FakeBrowser()
    server = uvicorn.Server(uvicorn.Config(fake.app, host="127.0.0.1", port=fake.port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    yield fake
    server.should_exit = True
    await task


@pytest.fixture
async def edge(browser, database, work_dir: Path, edge_binary: Path):
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", {"JARVIS_BROWSER_CDP_URL": browser.url}) as client:
        yield client


def _ws_url(client) -> str:
    return str(client.base_url).replace("http://", "ws://").rstrip("/") + "/ws/browser"


async def _recv(viewer, timeout: float = 5):
    msg = await asyncio.wait_for(viewer.recv(), timeout)
    return msg if isinstance(msg, bytes) else json.loads(msg)


async def _until(check, timeout: float = 5) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def test_frames_reach_the_panel_from_the_agents_tab(edge, browser):
    async with connect(_ws_url(edge)) as viewer:
        assert await viewer.recv() == '{"type":"status","state":"live"}'
        # The primed screenshot, placed by the viewport.
        assert await _recv(viewer) == {"type": "meta", "width": 800, "height": 600, "url": "https://example.com/start#top"}
        assert await _recv(viewer) == b"primed"

        # Everything went to the first page auto-attach reported, as
        # Playwright's pages[0].
        assert {c.get("sessionId") for c in browser.calls if c["method"].startswith("Page.")} == {"S1"}
        [cast] = browser.sent("Page.startScreencast")
        assert cast["params"] == {"format": "jpeg", "quality": 60, "maxWidth": 1280, "maxHeight": 800, "everyNthFrame": 2}

        await browser.event("Page.frameNavigated", {"frame": {"id": "F1", "url": "https://example.com/next"}})
        await browser.event("Page.frameNavigated", {"frame": {"id": "F2", "parentId": "F1", "url": "https://ads.test/"}})
        await browser.event("Page.screencastFrame", {"sessionId": 7, "data": _b64(b"one"),
                                                     "metadata": {"deviceWidth": 1280.0, "deviceHeight": 719.6}})
        assert await _recv(viewer) == {"type": "meta", "width": 1280, "height": 719, "url": "https://example.com/next"}
        assert await _recv(viewer) == b"one"
        await _until(lambda: browser.sent("Page.screencastFrameAck"))
        assert browser.sent("Page.screencastFrameAck")[0]["params"] == {"sessionId": 7}

        # Same size and URL: no meta, just the frame. Another tab's frame and
        # a payload that isn't base64 are dropped.
        await browser.event("Page.screencastFrame", {"sessionId": 8, "data": _b64(b"other tab"), "metadata": {}}, session="S2")
        await browser.event("Page.screencastFrame", {"sessionId": 9, "data": "not base64!!", "metadata": {}})
        await browser.event("Page.screencastFrame", {"sessionId": 10, "data": _b64(b"two"),
                                                     "metadata": {"deviceWidth": 1280, "deviceHeight": 719}})
        assert await _recv(viewer) == b"two"

        await browser.event("Page.navigatedWithinDocument", {"frameId": "F1", "url": "https://example.com/next#b"})
        await browser.event("Page.screencastFrame", {"sessionId": 11, "data": _b64(b"three"),
                                                     "metadata": {"deviceWidth": 1280, "deviceHeight": 719}})
        assert await _recv(viewer) == {"type": "meta", "width": 1280, "height": 719, "url": "https://example.com/next#b"}
        assert await _recv(viewer) == b"three"


async def test_viewers_share_one_cast_that_stops_with_the_last(edge, browser):
    async with connect(_ws_url(edge)) as first:
        assert (await _recv(first))["state"] == "live"
        await _recv(first), await _recv(first)
        await browser.event("Page.screencastFrame", {"sessionId": 1, "data": _b64(b"latest"),
                                                     "metadata": {"deviceWidth": 10, "deviceHeight": 5}})
        await _recv(first), await _recv(first)

        async with connect(_ws_url(edge)) as second:
            assert (await _recv(second))["state"] == "live"
            # A late joiner starts from the newest frame, not the next paint.
            assert (await _recv(second))["width"] == 10
            assert await _recv(second) == b"latest"
            assert browser.connections == 1
        await asyncio.sleep(0.3)
        assert not browser.sent("Page.stopScreencast"), "stopped while someone still watched"

    await _until(lambda: browser.disconnects == 1)
    assert len(browser.sent("Page.stopScreencast")) == 1

    # The next viewer attaches afresh.
    async with connect(_ws_url(edge)) as again:
        assert (await _recv(again))["state"] == "live"
        assert browser.connections == 2


async def test_a_closed_tab_ends_the_stream(edge, browser):
    async with connect(_ws_url(edge)) as viewer:
        assert (await _recv(viewer))["state"] == "live"
        await _recv(viewer), await _recv(viewer)
        await browser.event("Target.detachedFromTarget", {"sessionId": "S1", "targetId": "T1"}, session=None)
        assert await _recv(viewer) == {"type": "status", "state": "unavailable", "reason": "the browser went away"}
    await _until(lambda: browser.disconnects == 1)


async def test_no_browser_says_why_in_pythons_words(database, work_dir: Path, edge_binary: Path, monkeypatch):
    from tools import browser as browser_tool

    dead = f"http://127.0.0.1:{_free_port()}"
    env = {"JARVIS_BROWSER_CDP_URL": dead, "JARVIS_BROWSER_EXECUTABLE": "/nonexistent/chrome"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(browser_tool.BrowserUnavailable) as python:
        browser_tool.ensure_running()

    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
        async with connect(_ws_url(client)) as viewer:
            assert await _recv(viewer) == {"type": "status", "state": "unavailable", "reason": str(python.value)}
            with pytest.raises(Exception):
                await _recv(viewer, timeout=2)
