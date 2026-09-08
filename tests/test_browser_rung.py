"""The browser rung: resolution, challenge detection, and the escalation order.

There are two rungs now — a plain fetch, then the real browser. The headless
Chromium that used to sit between them was removed: it answered "the page is
client-side rendered" while *being* the reason for "the site refused us".

Nothing here launches a browser. What is worth pinning is the decision-making
around it — which binary gets picked, when a page counts as a challenge, and
that `read()` climbs to this rung only when the cheaper ones came back empty.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import browser, research


# ── Executable resolution ────────────────────────────────────────────────────

def test_configured_executable_wins_over_probe(monkeypatch, tmp_path):
    exe = tmp_path / "brave"
    exe.write_text("")
    monkeypatch.setattr(browser, "_setting", lambda key: str(exe) if key == "browser.executable" else "")
    assert browser.executable() == str(exe)


def test_configured_executable_that_does_not_exist_is_not_used(monkeypatch):
    monkeypatch.setattr(browser, "_setting", lambda key: "/nope/brave" if key == "browser.executable" else "")
    monkeypatch.setattr(browser.shutil, "which", lambda _: None)
    assert browser.executable() == ""


def test_probe_takes_the_first_installed_candidate(monkeypatch):
    monkeypatch.setattr(browser, "_setting", lambda key: "")
    monkeypatch.delenv("JARVIS_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.setattr(browser.sys, "platform", "linux")
    # Chrome absent, Brave present — any Chromium is acceptable, not just Chrome.
    monkeypatch.setattr(
        browser.shutil, "which", lambda name: "/usr/bin/brave-browser" if name == "brave-browser" else None
    )
    assert browser.executable() == "/usr/bin/brave-browser"


def test_linux_without_a_display_does_not_launch(monkeypatch):
    monkeypatch.setattr(browser.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert browser._has_display() is False
    monkeypatch.setattr(browser, "executable", lambda: "/usr/bin/chromium")
    called: list[object] = []
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **k: called.append(a))
    assert browser.launch() is False
    assert called == []  # never even tried — the headless rung above still works


def test_profile_dir_defaults_under_work_dir(monkeypatch, work_dir: Path):
    monkeypatch.setattr(browser, "_setting", lambda key: "")
    monkeypatch.delenv("JARVIS_BROWSER_PROFILE", raising=False)
    assert browser.profile_dir() == work_dir / "browser-profile"


def test_ensure_running_says_which_way_it_failed(monkeypatch):
    monkeypatch.setattr(browser, "_endpoint_live", lambda _: False)
    monkeypatch.setattr(browser, "executable", lambda: "")
    monkeypatch.setattr(browser, "launch", lambda: False)
    with pytest.raises(browser.BrowserUnavailable, match="no Chromium-based browser found"):
        browser.ensure_running()


# ── Challenge detection ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "title,body,expected",
    [
        ("Just a moment...", "Checking your browser before accessing.", True),
        ("", "Verify you are human to continue.", True),
        ("", "Access Denied. You do not have permission.", True),
        # The discrimination that matters: an article *about* captchas is the
        # page the agent asked for, not an interstitial in front of it.
        ("A history of CAPTCHAs", "Long piece on captcha design. " * 80, False),
        ("Cats", "A short ordinary page about cats.", False),
    ],
)
def test_challenge_detection(title, body, expected):
    assert bool(browser._challenge_marker(title, body)) is expected


def test_handoff_is_skipped_when_there_is_nobody_to_ask(monkeypatch):
    """CLI, bots and tests have no conversation — report, don't block forever."""
    from tools import sdk

    monkeypatch.setattr(sdk, "_conversation_id", None)
    assert browser._ask_human("https://example.com", "captcha") is False


# ── Escalation order ─────────────────────────────────────────────────────────

def test_browser_rung_is_not_reached_when_the_cheap_one_worked(monkeypatch):
    monkeypatch.setattr(research, "_extract", lambda html, url: "x" * 5_000)

    class _Resp:
        text = "<html/>"

        def raise_for_status(self) -> None: ...

    monkeypatch.setattr(research.httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(
        research, "_read_cdp", lambda url: pytest.fail("escalated on a page that read fine")
    )
    assert research.read("https://example.com").startswith("x")


def test_browser_rung_runs_when_the_plain_fetch_comes_back_empty(monkeypatch):
    def _fail(*a, **k):
        raise research.httpx.ConnectError("blocked")

    monkeypatch.setattr(research.httpx, "get", _fail)
    monkeypatch.setattr(research, "_read_cdp", lambda url: "the real article text " * 40)
    assert "the real article text" in research.read("https://example.com")


def test_browser_true_skips_straight_to_the_last_rung(monkeypatch):
    monkeypatch.setattr(
        research.httpx, "get", lambda *a, **k: pytest.fail("browser=True must not fetch over http")
    )
    monkeypatch.setattr(research, "_read_cdp", lambda url: "from the real browser " * 40)
    assert "from the real browser" in research.read("https://example.com", browser=True)


def test_every_rung_failing_reports_what_broke(monkeypatch):
    def _fail(*a, **k):
        raise research.httpx.ConnectError("connection refused")

    monkeypatch.setattr(research.httpx, "get", _fail)
    def _no_browser(url):
        raise browser.BrowserUnavailable("no display available")

    monkeypatch.setattr(research, "_read_cdp", _no_browser)
    out = research.read("https://example.com")
    assert "No readable text" in out
    assert "connection refused" in out and "no display available" in out


# ── Failure messages the agent has to act on ─────────────────────────────────

def test_failed_read_names_the_way_past_a_block(monkeypatch):
    """The only mention of the browser when none is running — the "Live
    browser" segment is absent then, and both suggestions launch one."""

    def _fail(*a, **k):
        raise research.httpx.ConnectError("refused")

    monkeypatch.setattr(research.httpx, "get", _fail)
    monkeypatch.setattr(research, "_read_cdp", lambda url: "")
    out = research.read("https://example.com")
    assert "browser=True" in out
    assert "from tools.browser import page" in out


def test_an_explicit_browser_read_does_not_suggest_itself(monkeypatch):
    monkeypatch.setattr(research, "_read_cdp", lambda url: "")
    out = research.read("https://example.com", browser=True)
    assert "browser=True" not in out


def test_rung_errors_are_capped(monkeypatch):
    """Playwright answers a missing binary with a multi-line ASCII box; uncapped
    it spends hundreds of tokens telling the agent nothing actionable."""

    def _fail(*a, **k):
        raise research.httpx.ConnectError("x" * 4000)

    monkeypatch.setattr(research.httpx, "get", _fail)
    monkeypatch.setattr(research, "_read_cdp", lambda url: "")
    out = research.read("https://example.com")
    assert len(out) < 800
    assert "…" in out


def test_rung_errors_are_flattened_to_one_line():
    exc = RuntimeError("line one\n  line two\n  line three")
    assert "\n" not in research._rung_error("headless", exc)


# ── The headless rung is gone, and must stay gone ────────────────────────────

def test_read_never_launches_a_browser_itself():
    """The whole point: reads go through the persistent, logged-in browser.

    A `chromium.launch()` here would reintroduce the blank profile whose
    fingerprint is what sites refuse. Checked against the parsed module rather
    than its text — prose about Playwright is fine, importing it is not.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(research))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "playwright" not in imported
    attrs = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "launch" not in attrs


def test_js_still_forces_the_browser(monkeypatch):
    """Older callers (and any conversation history) say js=True."""
    monkeypatch.setattr(
        research.httpx, "get", lambda *a, **k: pytest.fail("js=True must not fetch over http")
    )
    monkeypatch.setattr(research, "_read_cdp", lambda url: "rendered text " * 40)
    assert "rendered text" in research.read("https://example.com", js=True)


# ── The environment the browser actually runs in ─────────────────────────────
#
# Every earlier test here ran in a plain process with no event loop, so the
# whole browser path passed while being unusable from the only caller that
# matters. run_cell executes inside the kernel's asyncio loop, where the sync
# Playwright API raises "It looks like you are using Playwright Sync API inside
# the asyncio loop." These tests are async on purpose: pytest-asyncio runs them
# in a loop, which is the condition that was missing.

async def test_sync_page_refuses_inside_a_loop_and_names_the_fix():
    """It used to raise Playwright's message, which names the loop but not the
    escape — the agent had to reverse-engineer `apage` from the source."""
    with pytest.raises(RuntimeError, match="apage"):
        with browser.page():
            pass


async def test_fetch_works_inside_a_loop(monkeypatch):
    """`read(url, browser=True)` is called from a cell, i.e. inside the loop.

    It runs the sync API on a worker thread; the regression is any refactor
    that calls it directly again.
    """
    calls: list[str] = []
    monkeypatch.setattr(browser, "_announce", lambda url, phase: calls.append(phase))
    monkeypatch.setattr(browser, "ensure_running", lambda: "http://127.0.0.1:9222")

    class _Tab:
        url = "https://example.com/"

        def goto(self, *a, **k): ...
        def wait_for_timeout(self, *a): ...
        def content(self): return "<html><body>plenty of real text</body></html>"
        def title(self): return "Example"
        def inner_text(self, _): return "plenty of real text " * 200

    import contextlib

    @contextlib.contextmanager
    def _page():
        yield _Tab()

    monkeypatch.setattr(browser, "page", _page)
    html = browser.fetch("https://example.com")
    assert "real text" in html
    assert calls == ["start", "done"]


async def test_a_failed_browse_announces_error_not_success(monkeypatch):
    """The chip claimed a browse that never reached a browser."""
    calls: list[str] = []
    monkeypatch.setattr(browser, "_announce", lambda url, phase: calls.append(phase))

    import contextlib

    @contextlib.contextmanager
    def _page():
        class _Tab:
            def goto(self, *a, **k):
                raise RuntimeError("navigation failed")
        yield _Tab()

    monkeypatch.setattr(browser, "page", _page)
    with pytest.raises(RuntimeError):
        browser.fetch("https://example.com")
    assert calls == ["start", "error"]


async def test_nothing_is_announced_when_no_browser_can_be_reached(monkeypatch):
    """Announcing before the attempt is what lit the chip for a failed read."""
    calls: list[str] = []
    monkeypatch.setattr(browser, "_announce", lambda url, phase: calls.append(phase))

    def _no_browser():
        raise browser.BrowserUnavailable("nothing listening")

    monkeypatch.setattr(browser, "ensure_running", _no_browser)
    with pytest.raises(browser.BrowserUnavailable):
        browser.fetch("https://example.com")
    assert calls == []
