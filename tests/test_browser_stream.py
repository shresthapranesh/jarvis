"""The browser video path: frame fan-out, page-target recovery, and the announce.

Nothing here starts a browser or a screencast — Chrome's own encoder is not the
thing that can regress. What can is the plumbing around it: a slow viewer must
not stall the browser, a browser left with no page target must not become an
unattachable one, and the announce that drives the "browsing" chip must reach
the run it names and no one else.
"""

from __future__ import annotations

import asyncio

import pytest

from core.browser_stream import BrowserScreencast, Frame, _Subscriber
from tools import browser


def _frame(tag: bytes = b"x") -> Frame:
    return Frame(data=tag, width=100, height=50, url="https://example.com")


# ── Fan-out ──────────────────────────────────────────────────────────────────

def test_subscriber_is_hashable():
    """It lives in a set. A dataclass __eq__ would silently make it not.

    This is not hypothetical: the first version was a plain @dataclass and
    every connection failed at `subscribers.add(sub)`.
    """
    assert len({_Subscriber(), _Subscriber()}) == 2


def test_offer_replaces_rather_than_queues():
    """Depth 1, newest wins — a viewer behind by 20 frames wants the latest."""
    sub = _Subscriber()
    sub.offer(_frame(b"old"))
    sub.offer(_frame(b"new"))
    assert sub.queue.qsize() == 1
    assert sub.queue.get_nowait().data == b"new"


def test_offer_never_blocks_a_full_queue():
    """Backpressure must not reach the browser: a stalled socket drops frames."""
    sub = _Subscriber()
    for i in range(200):
        sub.offer(_frame(str(i).encode()))
    assert sub.queue.qsize() == 1
    assert sub.queue.get_nowait().data == b"199"


async def test_frames_reach_every_subscriber():
    cast = BrowserScreencast()
    a, b = _Subscriber(), _Subscriber()
    cast._subscribers.update({a, b})
    cast._on_frame({"sessionId": 1, "data": "aGk=", "metadata": {"deviceWidth": 8, "deviceHeight": 4}})
    assert a.queue.get_nowait().data == b"hi"
    assert b.queue.get_nowait().data == b"hi"


async def test_a_bad_frame_payload_is_dropped_not_raised():
    """The CDP callback runs on the event loop; a raise there kills the stream."""
    cast = BrowserScreencast()
    sub = _Subscriber()
    cast._subscribers.add(sub)
    cast._on_frame({"sessionId": 1, "data": "not base64!!", "metadata": {}})
    cast._on_frame({"sessionId": 1, "metadata": {}})  # no data at all
    assert sub.queue.empty()


# ── Page-target recovery ─────────────────────────────────────────────────────

def test_ensure_page_opens_a_tab_when_the_browser_has_none(monkeypatch):
    """Closing the last window leaves Chrome up with no page target, and
    connect_over_cdp then fails with an error about context management that
    says nothing about missing pages."""
    opened: list[str] = []

    class _Resp:
        def json(self) -> list:
            return [{"type": "browser_ui"}]

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(httpx, "put", lambda url, **k: opened.append(url))
    browser._ensure_page("http://127.0.0.1:9222")
    assert opened and "/json/new" in opened[0]


def test_ensure_page_is_a_noop_when_a_page_exists(monkeypatch):
    import httpx

    class _Resp:
        def json(self) -> list:
            return [{"type": "page"}]

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(
        httpx, "put", lambda *a, **k: pytest.fail("opened a tab when one existed")
    )
    browser._ensure_page("http://127.0.0.1:9222")


def test_ensure_page_survives_an_unreachable_endpoint(monkeypatch):
    """It runs on the path to a browser that may not be there at all."""
    import httpx

    def _boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", _boom)
    browser._ensure_page("http://127.0.0.1:9222")  # must not raise


# ── The announce that drives the chip ────────────────────────────────────────

def _context(session, caller: str = "agent", conversation_id: str | None = None):
    from server.graphql.extensions import SESSION_LOCK_KEY

    return {
        "session": session,
        SESSION_LOCK_KEY: asyncio.Lock(),
        "caller": caller,
        "caller_conversation_id": conversation_id,
    }


MUTATION = """
mutation($u: String!, $p: String!) { browserActivity(url: $u, phase: $p) }
"""


async def _announce(caller="agent", phase="start", conversation_id=None):
    from db import async_session
    from server.graphql.schema import schema

    async with async_session() as s:
        return await schema.execute(
            MUTATION,
            context_value=_context(s, caller, conversation_id),
            variable_values={"u": "https://example.com/a", "p": phase},
        )


async def test_announce_is_refused_for_a_human_caller(database):
    """A person opening the panel is not an agent announcing a navigation."""
    result = await _announce(caller="human")
    assert result.errors and "agent-initiated" in str(result.errors[0])


async def test_announce_rejects_an_unknown_phase(database):
    result = await _announce(phase="sideways")
    assert result.errors and "phase must be one of" in str(result.errors[0])


async def test_announce_without_a_live_run_is_false_not_an_error(database):
    """CLI, bots and tests browse with no TaskState. That is normal."""
    result = await _announce()
    assert not result.errors, result.errors
    assert result.data == {"browserActivity": False}


async def test_announce_lands_on_the_conversations_live_run(database):
    from core.state import TaskState, _tasks

    conv = "conv-browsing"
    state = TaskState()
    state.parent_id = conv
    _tasks["task-browsing"] = state
    try:
        result = await _announce(conversation_id=conv)
        assert not result.errors, result.errors
        assert result.data == {"browserActivity": True}
        emitted = [e for e in state.events if e.get("event") == "browser_step"]
        assert len(emitted) == 1
        import json

        payload = json.loads(emitted[0]["data"])
        assert payload["url"] == "https://example.com/a"
        assert payload["phase"] == "start"
    finally:
        _tasks.pop("task-browsing", None)
