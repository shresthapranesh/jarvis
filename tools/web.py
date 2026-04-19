"""Web research tools: search, page fetching, and link extraction."""
import asyncio
import re

import httpx


async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web for news and information about a topic."""
    from ddgs import DDGS

    def _sync() -> str:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
        except Exception as e:
            return f"Search failed: {e}"
        if not results:
            return "No results found."
        return "\n\n".join(
            f"Title: {r['title']}\nURL: {r['href']}\nSummary: {r['body']}"
            for r in results
        )

    return await asyncio.to_thread(_sync)


async def fetch_page(url: str) -> str:
    """Fetch the text content of a web page."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.ConnectError as e:
        return f"Could not connect to {url!r}: {e}"
    except httpx.TimeoutException:
        return f"Request timed out for {url!r}."
    except httpx.HTTPStatusError as e:
        return f"HTTP {e.response.status_code} error for {url!r}."
    except Exception as e:
        return f"Failed to fetch {url!r}: {e}"
    return response.text[:8000]


async def extract_links(url: str) -> str:
    """Fetch a web page and return all unique outbound hyperlinks."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.ConnectError as e:
        return f"Could not connect to {url!r}: {e}"
    except httpx.TimeoutException:
        return f"Request timed out for {url!r}."
    except Exception as e:
        return f"Failed to fetch {url!r}: {e}"
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', response.text)
    seen: set[str] = set()
    links = []
    for href in hrefs:
        if href.startswith("http") and href not in seen:
            seen.add(href)
            links.append(href)
    if not links:
        return "No outbound links found."
    return "\n".join(links[:100])


async def playwright_browse(url: str, selector: str | None = None) -> str:
    """Fetch a web page using a headless Chromium browser and return its text content.

    Handles JavaScript-rendered pages that simple HTTP requests cannot read.
    Use this instead of fetch_page when a site requires JavaScript to display content.

    Args:
        url: The URL to navigate to.
        selector: Optional CSS selector to extract a specific element's text.
                  If omitted, returns the full visible page text.

    Returns:
        The page text content (up to 10,000 characters).
    """
    from playwright.async_api import Error as PlaywrightError, async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except PlaywrightError as e:
                return f"Failed to load {url!r}: {e}"
            if selector:
                el = await page.query_selector(selector)
                text = await el.inner_text() if el else f"Selector {selector!r} not found."
            else:
                text = await page.inner_text("body")
            return text[:10_000]
        finally:
            await browser.close()
