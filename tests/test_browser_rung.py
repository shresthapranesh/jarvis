"""The browser rung: resolution, challenge detection, and the escalation order.

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


def test_browser_rung_runs_when_the_others_come_back_empty(monkeypatch):
    def _fail(*a, **k):
        raise research.httpx.ConnectError("blocked")

    monkeypatch.setattr(research.httpx, "get", _fail)
    monkeypatch.setattr(research, "_read_playwright", lambda url: "")
    monkeypatch.setattr(research, "_read_cdp", lambda url: "the real article text " * 40)
    assert "the real article text" in research.read("https://example.com")


def test_browser_true_skips_straight_to_the_last_rung(monkeypatch):
    monkeypatch.setattr(
        research.httpx, "get", lambda *a, **k: pytest.fail("browser=True must not fetch over http")
    )
    monkeypatch.setattr(
        research, "_read_playwright", lambda url: pytest.fail("browser=True must skip headless")
    )
    monkeypatch.setattr(research, "_read_cdp", lambda url: "from the real browser " * 40)
    assert "from the real browser" in research.read("https://example.com", browser=True)


def test_every_rung_failing_reports_what_broke(monkeypatch):
    def _fail(*a, **k):
        raise research.httpx.ConnectError("connection refused")

    monkeypatch.setattr(research.httpx, "get", _fail)
    monkeypatch.setattr(research, "_read_playwright", lambda url: "")
    def _no_browser(url):
        raise browser.BrowserUnavailable("no display available")

    monkeypatch.setattr(research, "_read_cdp", _no_browser)
    out = research.read("https://example.com")
    assert "No readable text" in out
    assert "connection refused" in out and "no display available" in out
