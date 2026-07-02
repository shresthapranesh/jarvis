"""Web research helpers — preloaded into every run_cell kernel (core/kernels.py).

These are NOT bound LangChain tools. They are plain sync functions the agent
calls from Python cells (`search("...")`, `read(url)`), keeping web research
code-first and composable: results land in kernel variables and feed straight
into later cells.

`search` uses an API-backed provider when a key is present — Tavily
(TAVILY_API_KEY) or Brave Search (BRAVE_API_KEY), in that order — and falls
back to DuckDuckGo scraping (ddgs) keyless, mirroring how embeddings degrade.
`read` fetches a URL and extracts the main article text with trafilatura,
falling back to headless Chromium for JS-rendered pages.
"""

from __future__ import annotations

import os

import httpx

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Below this many extracted chars, assume the page needed JS and try Playwright.
_JS_FALLBACK_THRESHOLD = 400


def _search_tavily(query: str, max_results: int, api_key: str) -> list[dict]:
    r = httpx.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": query, "max_results": max_results},
        timeout=20,
    )
    r.raise_for_status()
    return [
        {"title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("content", "")}
        for item in r.json().get("results", [])
    ]


def _search_brave(query: str, max_results: int, api_key: str) -> list[dict]:
    r = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": max_results},
        headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    return [
        {"title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("description", "")}
        for item in r.json().get("web", {}).get("results", [])[:max_results]
    ]


def _search_ddgs(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS

    with DDGS() as ddgs:
        return [
            {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
            for r in ddgs.text(query, max_results=max_results)
        ]


def search(query: str, max_results: int = 8) -> list[dict]:
    """Web search. Returns [{title, url, snippet}, ...] — leads, not answers.

    Uses Tavily or Brave when an API key is configured (TAVILY_API_KEY /
    BRAVE_API_KEY env vars), otherwise DuckDuckGo. Snippets are teasers:
    read(url) the promising results before drawing conclusions.
    """
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()
    providers: list[tuple[str, object]] = []
    if tavily_key:
        providers.append(("tavily", lambda: _search_tavily(query, max_results, tavily_key)))
    if brave_key:
        providers.append(("brave", lambda: _search_brave(query, max_results, brave_key)))
    providers.append(("ddgs", lambda: _search_ddgs(query, max_results)))

    errors: list[str] = []
    for name, fn in providers:
        try:
            results = fn()  # type: ignore[operator]
            if results:
                return results
            errors.append(f"{name}: no results")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError("all search providers failed — " + "; ".join(errors))


def _read_playwright(url: str) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=_UA)
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1_500)  # give client-side rendering a beat
            html = page.content()
        finally:
            browser.close()
    return _extract(html, url) or ""


def _extract(html: str, url: str) -> str | None:
    import trafilatura

    return trafilatura.extract(
        html, url=url, include_comments=False, include_tables=True, favor_recall=True
    )


def read(url: str, max_chars: int = 12_000, js: bool = False) -> str:
    """Fetch a URL and return its main text content (markup/nav/ads stripped).

    Plain HTTP fetch + trafilatura extraction; automatically retries with
    headless Chromium when the page looks JS-rendered (or pass js=True to
    force it). Truncates to max_chars with an explicit marker.
    """
    text = ""
    if not js:
        try:
            r = httpx.get(
                url, follow_redirects=True, timeout=20, headers={"User-Agent": _UA}
            )
            r.raise_for_status()
            text = _extract(r.text, url) or ""
        except httpx.HTTPError:
            pass  # fall through to the browser path
    if len(text) < _JS_FALLBACK_THRESHOLD:
        try:
            browser_text = _read_playwright(url)
        except Exception as exc:
            if not text:
                return f"Failed to read {url!r}: {exc}"
            browser_text = ""
        if len(browser_text) > len(text):
            text = browser_text
    if not text:
        return f"No readable text extracted from {url!r}."
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars — call read(url, max_chars=...) for more]"
    return text
