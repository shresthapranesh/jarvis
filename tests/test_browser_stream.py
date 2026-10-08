"""The browser video path: frame fan-out, page-target recovery, and the announce.

Nothing here starts a browser or a screencast — Chrome's own encoder is not the
thing that can regress. What can is the plumbing around it: a slow viewer must
not stall the browser, a browser left with no page target must not become an
unattachable one, and the announce that drives the "browsing" chip must reach
the run it names and no one else.
"""

from __future__ import annotations


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


# ── Telling the agent the browser exists ─────────────────────────────────────
#
# The capability was reachable but invisible: nothing in the prompt mentioned
# it, and an agent improvising with Playwright writes chromium.launch(), which
# rebuilds the blocked headless rung instead of using the logged-in browser.

def _clear_probe_cache():
    from core import agents

    agents._browser_probe = (0.0, False)


def test_no_segment_when_no_browser_is_running(monkeypatch):
    """A prompt line would be billed on every run; this costs nothing."""
    from core import agents

    _clear_probe_cache()
    monkeypatch.setattr(browser, "_endpoint_live", lambda url: False)
    assert agents._browser_volatile_parts() == []


def test_segment_appears_when_a_browser_is_running(monkeypatch):
    from core import agents

    _clear_probe_cache()
    monkeypatch.setattr(browser, "_endpoint_live", lambda url: True)
    parts = agents._browser_volatile_parts()
    assert len(parts) == 1
    assert parts[0].name == "browser"
    body = parts[0].content
    assert "browser=True" in body
    # The async form specifically: the kernel runs an event loop, so a segment
    # advertising sync `page()` sends the agent straight into a raise — which
    # is exactly what it did until a real run caught it.
    assert "apage" in body
    assert "async with" in body
    # The specific wrong turn it exists to prevent.
    assert "chromium.launch()" in body


def test_segment_has_a_stability_rank(monkeypatch):
    """Unranked segments sort last among cached blocks and churn the prefix."""
    from core.agents import _SEGMENT_STABILITY

    assert "browser" in _SEGMENT_STABILITY


def test_probe_is_cached_not_run_per_iteration(monkeypatch):
    """It sits in the per-turn retrieval path, and cdp_url may be remote."""
    from core import agents

    _clear_probe_cache()
    calls = []
    monkeypatch.setattr(browser, "_endpoint_live", lambda url: calls.append(url) or True)
    for _ in range(5):
        agents._browser_volatile_parts()
    assert len(calls) == 1


def test_probe_failure_is_not_fatal(monkeypatch):
    """It runs on every turn; a raise here would break the whole run."""
    from core import agents

    _clear_probe_cache()

    def _boom(url):
        raise RuntimeError("bad cdp_url")

    monkeypatch.setattr(browser, "_endpoint_live", _boom)
    assert agents._browser_volatile_parts() == []
