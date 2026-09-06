"""A real, persistent Chromium the agent can borrow — the rung above headless.

`tools/research.py:read()` climbs three rungs: a plain httpx fetch, then
headless Chromium, then this one. The first two lose to sites that gate on a
headless fingerprint, a blank profile with no history, or an interstitial
challenge — not because the request is automated, but because it looks nothing
like a person's browser. This rung is a browser started the way a person
starts one: headed, with a profile that keeps its cookies between runs.

**The browser is owned by neither process.** `read()` runs in a kernel
(`core/kernels.py`), while the approval gate and the event stream live in the
server; kernels are per-conversation, capped, and reaped after 30 minutes idle.
A browser owned by either side would be wrong — one Chrome per conversation, or
a new transport so the other side can reach it. Attaching over CDP sidesteps
the split entirely: the browser is a third process both sides can dial, its
profile outlives every kernel, and a login done once is still there next week.

**Any Chromium, not just Chrome.** Attaching is browser-agnostic by
construction — CDP is a protocol, and `connect_over_cdp` cannot tell what is
listening on the other end. Only *launching* needs a binary path, so that is
the only place a preference exists: `browser.executable` when set, otherwise
the first entry in `_CANDIDATES` that exists on this platform.

**The profile is a dedicated one, and that is not a style choice.** Recent
Chromium builds refuse to open a remote-debugging port on the default
user-data-dir at all — any local process could otherwise read the cookie jar —
so aiming at a real profile yields no listener and a mystifying timeout. It is
also the containment boundary: this thing reads untrusted web pages for a
living, and it should reach only the sites someone deliberately signed into in
*this* window, never every session in the human's daily browser.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_CDP_URL = "http://127.0.0.1:9222"

_PROBE_TIMEOUT = 1.0        # per liveness poll against /json/version
_LAUNCH_TIMEOUT = 25.0      # how long a cold browser gets to open its port
_PAGE_TIMEOUT = 30_000      # ms, per navigation
_SETTLE_MS = 1_500          # give client-side rendering a beat, as the headless rung does
_MAX_CHALLENGE_BODY = 1_500  # a real page has more text on it than an interstitial

# Preference order for the launch path only. Chrome leads because it is the
# most common, not because anything depends on it — set `browser.executable`
# and none of this runs.
_CANDIDATES: dict[str, tuple[str, ...]] = {
    "darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
    ),
    "linux": (
        "google-chrome",
        "google-chrome-stable",
        "brave-browser",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "microsoft-edge-stable",
    ),
}

# Substrings that mean "a human has to look at this", matched lowercased
# against the title and body. Deliberately short: a false negative costs the
# agent concluding a site is empty, which is the worse error.
_CHALLENGE_MARKERS = (
    "verify you are human",
    "checking your browser",
    "are you a robot",
    "unusual traffic",
    "captcha",
    "cf-challenge",
    "just a moment",
    "access denied",
    "please enable javascript and cookies",
)


class BrowserUnavailable(RuntimeError):
    """No CDP browser could be reached, and none could be started here."""


# ── Configuration ────────────────────────────────────────────────────────────

def _setting(key: str) -> str:
    """One `config_settings` value, over the same read-only path the SDK uses.

    This runs in the kernel process, which has no ORM session and must never
    take a write lock against the server. Missing table, missing row and
    missing database all mean the same thing here: fall back to the default.
    """
    try:
        from tools.sdk import _connect

        with _connect() as conn:
            row = conn.execute(
                "SELECT value FROM config_settings WHERE key = ?", (key,)
            ).fetchone()
    except (sqlite3.Error, RuntimeError, ImportError):
        return ""
    if row is None:
        return ""
    value = row["value"]
    try:  # values are written as plain text, but tolerate a JSON-quoted one
        decoded = json.loads(value)
        if isinstance(decoded, str):
            value = decoded
    except (ValueError, TypeError):
        pass
    return (value or "").strip()


def cdp_url() -> str:
    """Where to find the browser. Setting → env → localhost:9222."""
    return (
        _setting("browser.cdp_url")
        or os.environ.get("JARVIS_BROWSER_CDP_URL", "").strip()
        or DEFAULT_CDP_URL
    )


def profile_dir() -> Path:
    """The dedicated user-data-dir. Never the human's daily profile."""
    configured = _setting("browser.profile_dir") or os.environ.get(
        "JARVIS_BROWSER_PROFILE", ""
    ).strip()
    if configured:
        return Path(configured).expanduser()
    from core.config import get_config

    return get_config().work_dir / "browser-profile"


def executable() -> str:
    """Resolved browser binary for the launch path, or "" if none is installed."""
    configured = _setting("browser.executable") or os.environ.get(
        "JARVIS_BROWSER_EXECUTABLE", ""
    ).strip()
    if configured:
        return configured if Path(configured).exists() or shutil.which(configured) else ""
    for candidate in _CANDIDATES.get(sys.platform, ()):
        if candidate.startswith("/"):
            if Path(candidate).exists():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return ""


def _has_display() -> bool:
    """Whether a headed browser can open a window here.

    macOS always can. On Linux a missing DISPLAY/WAYLAND_DISPLAY means a server
    with no session attached, and this rung stays out of the way rather than
    failing — the headless rung above it still works.
    """
    if sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# ── Reaching the browser ─────────────────────────────────────────────────────

def _endpoint_live(url: str) -> bool:
    import httpx

    try:
        r = httpx.get(f"{url.rstrip('/')}/json/version", timeout=_PROBE_TIMEOUT)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def launch() -> bool:
    """Start a headed browser on the configured port. True if it came up.

    Detached (`start_new_session`) on purpose: the browser must outlive the
    kernel that happened to want it first, so the next conversation attaches to
    a warm profile instead of paying the cold start and the logins again.
    """
    url = cdp_url()
    exe = executable()
    if not exe:
        return False
    if not _has_display():
        logger.info("browser: no display, staying on the headless rung")
        return False

    port = urlparse(url).port or 9222
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        # Brave and Edge both open an import/welcome wizard on a fresh profile,
        # and it sits in front of every page forever if it is not suppressed.
        "--no-first-run",
        "--no-default-browser-check",
    ]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        logger.warning("browser: launch failed (%s): %s", exe, exc)
        return False

    deadline = time.monotonic() + _LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        if _endpoint_live(url):
            logger.info("browser: launched %s on %s", Path(exe).name, url)
            return True
        time.sleep(0.4)
    logger.warning("browser: %s did not open %s within %ss", exe, url, _LAUNCH_TIMEOUT)
    return False


def ensure_running() -> str:
    """The CDP endpoint of a live browser, launching one if needed.

    Raises BrowserUnavailable rather than returning a sentinel: every caller
    has an older rung to fall back to, and the message says which of the three
    ways this can be unavailable actually happened.
    """
    url = cdp_url()
    if _endpoint_live(url):
        return url
    if launch():
        return url
    if not executable():
        raise BrowserUnavailable(
            "no Chromium-based browser found — install one, or set the "
            "`browser.executable` config key to its path"
        )
    if not _has_display():
        raise BrowserUnavailable(
            "no display available for a headed browser; point `browser.cdp_url` "
            "at a browser running on a machine that has one"
        )
    raise BrowserUnavailable(f"could not reach or start a browser at {url}")


@contextmanager
def page() -> Iterator[Any]:
    """A Playwright page on the persistent browser. Reuses its single tab.

    The tab is deliberately not closed on exit — it *is* the session, and
    reusing it keeps one window rather than accumulating one per read. Only the
    local Playwright connection is torn down; the browser was started outside
    this process and outlives it.
    """
    from playwright.sync_api import sync_playwright

    endpoint = ensure_running()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(endpoint)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        tab = ctx.pages[0] if ctx.pages else ctx.new_page()
        yield tab


# ── Challenge handoff ────────────────────────────────────────────────────────

def _challenge_marker(title: str, body: str) -> str:
    """The marker that fired, or "".

    Gated on the body being *short* first, which is what separates a challenge
    from an article about challenges: an interstitial is a sentence and a
    widget, while a page with real content that merely says "captcha" in it is
    the page the agent asked for. Matching on the words alone made an essay
    about CAPTCHAs read as a CAPTCHA.
    """
    stripped = body.strip()
    if len(stripped) > _MAX_CHALLENGE_BODY:
        return ""
    haystack = f"{title}\n{stripped}".lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in haystack:
            return marker
    return ""


def _ask_human(url: str, marker: str) -> bool:
    """Park until a human clears the challenge in the open window.

    Reuses the SDK's gate helpers rather than inventing a second mechanism:
    same durable `Approval` row, same inbox, same chat prompt, and
    `core/kernels.py:_hold_for_approval` already suspends the 60s cell timeout
    while one is open — so this can wait the full gate timeout without the cell
    being killed out from under it.

    Returns False when there is nobody to ask (CLI, a bot surface, tests), so
    the caller reports what it saw instead of blocking on an answer that will
    never come.
    """
    try:
        from tools import sdk
    except ImportError:
        return False
    if not getattr(sdk, "_conversation_id", None):
        return False

    try:
        approval_id = sdk._request_gate(
            "sdk:browser_challenge",
            "browser challenge",
            {
                "url": url,
                "detected": marker,
                "action": "Solve the challenge in the open browser window, then approve.",
            },
        )
        approved, _answer = sdk._await_gate(approval_id, time.time() + sdk._gate_timeout())
        return approved
    except Exception as exc:  # the run must survive a gate that cannot be raised
        logger.warning("browser: challenge handoff failed: %s", exc)
        return False


# ── The one thing research.py calls ──────────────────────────────────────────

def fetch(url: str, *, settle_ms: int = _SETTLE_MS, allow_handoff: bool = True) -> str:
    """Navigate the persistent browser to `url` and return the settled HTML.

    On a challenge interstitial, asks a human to clear it and re-reads the page
    once they have. Raises BrowserUnavailable if this rung isn't usable here.
    """
    with page() as tab:
        tab.goto(url, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT)
        tab.wait_for_timeout(settle_ms)
        html = tab.content()

        if not allow_handoff:
            return html
        marker = _challenge_marker(tab.title() or "", tab.inner_text("body") or "")
        if not marker:
            return html

        logger.info("browser: challenge on %s (%r) — asking for a human", url, marker)
        if not _ask_human(url, marker):
            return html
        # The human cleared it in the same tab; the clearance cookie is now in
        # the profile, so this and every later read get the real page.
        tab.wait_for_timeout(settle_ms)
        return tab.content()
