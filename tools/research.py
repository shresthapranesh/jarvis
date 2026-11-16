"""Web research helpers — preloaded into every run_cell kernel (edge/src/kernels/).

These are NOT bound tools. They are plain sync functions the agent
calls from Python cells (`search("...")`, `read(url)`), keeping web research
code-first and composable: results land in kernel variables and feed straight
into later cells.

`search` uses an API-backed provider when a key is present — Tavily
(TAVILY_API_KEY) or Brave Search (BRAVE_API_KEY), in that order — and falls
back to DuckDuckGo scraping (ddgs) keyless, mirroring how embeddings degrade.
`read` fetches a URL and goes by what came back: an HTML page has its main
article text extracted with trafilatura, escalating to the persistent real
browser (tools/browser.py) when the plain fetch comes back empty — a refused
request and a client-side-rendered page look the same from here, and one rung
answers both. A PDF is extracted page by page (pypdf, imported only when one
arrives), text and JSON come back as they are, and any other binary is named
rather than read. `read_many` reads several URLs at once. Extracted text is
kept briefly per kernel, so a longer or later window of the same page is not
fetched again.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Below this many extracted chars, assume the fetch was refused or the page
# renders client-side, and go to the real browser.
_BROWSER_FALLBACK_THRESHOLD = 400

# Per-rung failure text kept in the message the agent reads.
_ERROR_CAP = 160

# Download ceilings. A page or text body past _TEXT_DOWNLOAD_CAP is read up to
# it (the head of a huge file is still text); a PDF past _PDF_DOWNLOAD_CAP is
# refused, since a cut-off PDF has no cross-reference table to parse. Other
# binaries are never downloaded past the sniffing head.
_TEXT_DOWNLOAD_CAP = 10 * 1024 * 1024
_PDF_DOWNLOAD_CAP = 40 * 1024 * 1024
_SNIFF_BYTES = 1024
# Wall-clock budgets inside one cell's 60 s: the whole body download, and
# PDF text extraction (pypdf is pure Python — a long report takes seconds).
_DOWNLOAD_DEADLINE = 25.0
_PDF_EXTRACT_DEADLINE = 20.0

# The per-kernel cache of extracted full text, keyed by URL. Small on purpose:
# kernels run on low-RAM machines, and this exists so a longer window, the
# next part, or a repeat read of a page just read costs nothing.
_CACHE_TTL = 15 * 60
_CACHE_ENTRIES = 32
_CACHE_CHARS = 16_000_000

_DEFAULT_MAX_CHARS = 12_000
_READ_MANY_WORKERS = 6

# One real browser serves the whole kernel: `read_many`'s threads queue for it
# rather than drive its tabs at once.
_BROWSER_LOCK = threading.Lock()

# Tests put an `httpx.MockTransport` here; None is httpx's own.
_transport: httpx.BaseTransport | None = None


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
    read(url) the promising results — or read_many(urls) for several at once —
    before drawing conclusions.
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


def _rung_error(rung: str, exc: Exception) -> str:
    """One line per failed rung. Capped, because these land in the agent's context.

    Playwright answers a launch failure with a multi-line ASCII box; uncapped,
    a failed read spends several hundred tokens telling the agent nothing it
    can act on.
    """
    text = " ".join(str(exc).split())
    return f"{rung}: {text[:_ERROR_CAP]}" + ("…" if len(text) > _ERROR_CAP else "")


def _read_cdp(url: str) -> str:
    """Second rung: the persistent real browser (tools/browser.py).

    Reached when the plain fetch came back near-empty, because it costs a real
    browser and possibly a human's attention — and because for the ordinary
    page it wins nothing the httpx fetch already had.
    """
    from tools import browser as _browser

    return _extract(_browser.fetch(url), url) or ""


def _extract(html: str, url: str) -> str | None:
    import trafilatura

    return trafilatura.extract(
        html, url=url, include_comments=False, include_tables=True, favor_recall=True
    )


# ── What came back ───────────────────────────────────────────────────────────

_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_TEXT_TYPES = frozenset({
    "application/json", "application/ld+json", "application/x-ndjson", "application/xml",
    "application/rss+xml", "application/atom+xml", "application/csv", "application/yaml",
    "application/x-yaml", "application/javascript",
})
# Types that say nothing, so the URL and the first bytes decide.
_UNTYPED = frozenset({"", "application/octet-stream", "binary/octet-stream", "application/unknown"})
_HTML_EXTS = frozenset({".html", ".htm", ".xhtml"})
_TEXT_EXTS = frozenset({
    ".txt", ".csv", ".tsv", ".md", ".markdown", ".rst", ".log", ".json", ".jsonl",
    ".ndjson", ".xml", ".rss", ".atom", ".yaml", ".yml",
})


def _kind(content_type: str, url: str, head: bytes) -> str:
    """"html", "pdf", "text" or "binary": by the declared type, else the URL's
    extension, else the first bytes. Anything that still looks like markup is
    "html", which is what every body was before."""
    ct = content_type.split(";", 1)[0].strip().lower()
    if ct == "application/pdf" or b"%PDF-" in head:
        return "pdf"
    if ct in _HTML_TYPES:
        return "html"
    if ct.startswith("text/") or ct in _TEXT_TYPES or ct.endswith(("+json", "+xml")):
        return "text"
    if ct not in _UNTYPED:
        return "binary"
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext == ".pdf":
        return "pdf"
    if ext in _HTML_EXTS:
        return "html"
    if ext in _TEXT_EXTS:
        return "text"
    if b"\x00" in head:
        return "binary"
    sample = head.decode("utf-8", "replace")
    if sample.count("�") > 2:  # a cut multi-byte char at the end is fine
        return "binary"
    return "html" if sample.lstrip().startswith("<") else "text"


class _Body:
    """What `_fetch` read. A plain class, not a NamedTuple or dataclass: those
    cost import time on every kernel start."""

    __slots__ = ("kind", "content_type", "data", "encoding", "size", "complete")

    def __init__(self, kind: str, content_type: str, data: bytes, encoding: str,
                 size: int | None, complete: bool) -> None:
        self.kind = kind
        self.content_type = content_type
        self.data = data
        self.encoding = encoding
        self.size = size          # Content-Length when declared, else what was read
        self.complete = complete  # the whole body was read


def _fetch(url: str) -> _Body:
    """GET `url`, reading only as much of the body as its kind is worth:
    a sniffing head for a binary, up to a ceiling for text and PDFs."""
    with httpx.Client(
        follow_redirects=True, timeout=20, headers={"User-Agent": _UA}, transport=_transport
    ) as client, client.stream("GET", url) as r:
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        try:
            declared: int | None = int(r.headers.get("content-length", ""))
        except ValueError:
            declared = None
        chunks = r.iter_bytes()
        got: list[bytes] = []
        n = 0
        complete = True
        for chunk in chunks:
            got.append(chunk)
            n += len(chunk)
            if n >= _SNIFF_BYTES:
                complete = False
                break
        kind = _kind(ct, str(r.url), b"".join(got)[:_SNIFF_BYTES])
        if kind == "binary":
            size = declared if declared is not None else (n if complete else None)
            return _Body(kind, ct, b"", "", size, complete)
        cap = _PDF_DOWNLOAD_CAP if kind == "pdf" else _TEXT_DOWNLOAD_CAP
        if kind == "pdf" and declared is not None and declared > cap:
            return _Body(kind, ct, b"", "", declared, False)
        deadline = time.monotonic() + _DOWNLOAD_DEADLINE
        while not complete and n < cap and time.monotonic() < deadline:
            chunk = next(chunks, None)
            if chunk is None:
                complete = True
                break
            got.append(chunk)
            n += len(chunk)
        data = b"".join(got)[:cap]
        return _Body(kind, ct, data, r.encoding or "utf-8", declared if declared is not None else n, complete)


def _decode(body: _Body) -> str:
    try:
        return body.data.decode(body.encoding, errors="replace")
    except LookupError:  # a charset Python doesn't know
        return body.data.decode("utf-8", errors="replace")


def _size(n: int | None) -> str:
    if n is None:
        return "size unknown"
    if n < 1024:
        return f"{n} bytes"
    x = n / 1024
    for unit in ("KB", "MB"):
        if x < 1024:
            return f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} GB"


def _pdf_text(data: bytes) -> tuple[str, int, bool]:
    """A PDF's text page by page: (text, page count, any text found).

    pypdf is imported here, not at module level: kernels import this module
    at boot, and most reads are not PDFs.
    """
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        reader.decrypt("")  # most "encrypted" PDFs only restrict printing
    total = len(reader.pages)
    parts: list[str] = []
    found = False
    deadline = time.monotonic() + _PDF_EXTRACT_DEADLINE
    for i, page in enumerate(reader.pages, 1):
        if time.monotonic() > deadline:
            parts.append(
                f"[stopped after page {i - 1} of {total}: extraction ran past "
                f"{_PDF_EXTRACT_DEADLINE:.0f} s — for the rest, download the file and use pypdf]"
            )
            break
        text = (page.extract_text() or "").strip()
        found = found or bool(text)
        parts.append(f"[page {i} of {total}]\n{text}")
    return "\n\n".join(parts), total, found


class _Read:
    __slots__ = ("text", "ok", "via_browser")

    def __init__(self, text: str, ok: bool, via_browser: bool) -> None:
        self.text = text                # the full text, or a message when not ok
        self.ok = ok                    # worth caching
        self.via_browser = via_browser  # the text came from the real browser


def _not_html(url: str, body: _Body) -> _Read:
    """A PDF, text or binary body. None of these go to the browser: it would
    only fetch the same bytes again."""
    ct = body.content_type.split(";", 1)[0].strip() or "no declared type"
    if body.kind == "binary":
        return _Read(
            f"Not a text page: {url!r} is {ct}, {_size(body.size)}. read() returns text only — "
            "get the bytes with httpx.get(url, follow_redirects=True).content.",
            False, False,
        )
    if body.kind == "pdf":
        if not body.complete:
            if body.size is not None and body.size >= _PDF_DOWNLOAD_CAP or len(body.data) >= _PDF_DOWNLOAD_CAP:
                why = f"is {_size(body.size)}, over read()'s {_size(_PDF_DOWNLOAD_CAP)} limit"
            else:
                why = f"did not finish downloading in {_DOWNLOAD_DEADLINE:.0f} s"
            return _Read(
                f"The PDF at {url!r} {why}. Stream it to a file with httpx.stream() "
                "and extract the pages you need with pypdf.",
                False, False,
            )
        try:
            text, pages, found = _pdf_text(body.data)
        except Exception as exc:
            return _Read(f"Could not extract text from the PDF at {url!r}: {_rung_error('pypdf', exc)}", False, False)
        if not found:
            return _Read(
                f"The PDF at {url!r} has {pages} page(s) but no extractable text — "
                "probably scanned images, which need OCR.",
                False, False,
            )
        return _Read(text, True, False)
    text = _decode(body)
    if not text.strip():
        return _Read(f"{url!r} returned an empty {ct} body.", False, False)
    if not body.complete and len(body.data) >= _TEXT_DOWNLOAD_CAP:
        text += f"\n... [download stopped at {_size(_TEXT_DOWNLOAD_CAP)}; the file is larger]"
    return _Read(text, True, False)


def _read_full(url: str, browser: bool) -> _Read:
    """The whole text of `url`, climbing the rungs an HTML page needs."""
    text = ""
    errors: list[str] = []
    if not browser:
        try:
            body = _fetch(url)
        except httpx.HTTPError as exc:
            errors.append(_rung_error("http", exc))  # fall through to the browser
        else:
            if body.kind != "html":
                return _not_html(url, body)
            text = _extract(_decode(body), url) or ""
    via_browser = False
    if len(text) < _BROWSER_FALLBACK_THRESHOLD:
        # An explicit browser=True reports its own failure, since the caller
        # asked for this specifically; an automatic escalation stays quiet and
        # keeps whatever the plain fetch managed to extract.
        try:
            with _BROWSER_LOCK:
                real_text = _read_cdp(url)
        except Exception as exc:
            if browser and not text:
                return _Read(f"Failed to read {url!r} in the browser: {exc}", False, False)
            errors.append(_rung_error("browser", exc))
            logger.info("read(%s): browser rung unavailable: %s", url, exc)
            real_text = ""
        if len(real_text) > len(text):
            text, via_browser = real_text, True
    if not text:
        detail = f" ({'; '.join(errors)})" if errors else ""
        # Teach the next move here rather than in the system prompt. This is
        # the exact moment it is needed, and it costs nothing on the runs that
        # never fail — where a standing prompt line would be billed anyway.
        # It matters most when NO browser is running: the "Live browser"
        # segment is absent then, so this is the only mention, and the
        # suggestion below starts one on demand.
        hint = (
            " If the site blocks automation, retry with read(url, browser=True) to go "
            "through a real logged-in browser, or drive the page yourself: "
            "`from tools.browser import apage` then `async with apage() as tab:` "
            "(async — this kernel runs an event loop)."
        ) if not browser else ""
        return _Read(f"No readable text extracted from {url!r}.{detail}{hint}", False, False)
    # A rung that failed on the way may have cost text: don't keep it.
    return _Read(text, not errors, via_browser)


# ── The per-kernel cache ─────────────────────────────────────────────────────

_cache: OrderedDict[str, tuple[float, str, bool]] = OrderedDict()
_cache_chars = 0
_cache_lock = threading.Lock()


def _cache_get(url: str, browser: bool) -> str | None:
    """Fresh text for `url`. An explicit browser read takes only text the
    browser produced — asking for it means the plain fetch wasn't good enough."""
    with _cache_lock:
        entry = _cache.get(url)
        if entry is None:
            return None
        stamp, text, via_browser = entry
        if time.monotonic() - stamp > _CACHE_TTL:
            _cache_drop(url)
            return None
        if browser and not via_browser:
            return None
        _cache.move_to_end(url)
        return text


def _cache_drop(url: str) -> None:
    """Remove one entry. Caller holds `_cache_lock`."""
    global _cache_chars
    entry = _cache.pop(url, None)
    if entry is not None:
        _cache_chars -= len(entry[1])


def _cache_put(url: str, text: str, via_browser: bool) -> None:
    global _cache_chars
    with _cache_lock:
        _cache_drop(url)
        if len(text) > _CACHE_CHARS:
            return
        _cache[url] = (time.monotonic(), text, via_browser)
        _cache_chars += len(text)
        while len(_cache) > _CACHE_ENTRIES or _cache_chars > _CACHE_CHARS:
            _, (_, old, _) = _cache.popitem(last=False)
            _cache_chars -= len(old)


def _cache_clear() -> None:
    global _cache_chars
    with _cache_lock:
        _cache.clear()
        _cache_chars = 0


def _window(text: str, max_chars: int, offset: int) -> str:
    """`max_chars` of `text` from `offset`, saying where it sits and how to
    get the rest."""
    total = len(text)
    offset = max(0, offset)
    if offset and offset >= total:
        return f"[offset {offset} is past the end — the text is {total} chars]"
    end = offset + max(0, max_chars)
    part = text[offset:end]
    if offset:
        part = f"[chars {offset}–{min(end, total)} of {total}]\n{part}"
    if end < total:
        part += (
            f"\n... [truncated {total - end} chars — read(url, offset={end}) for the next part, "
            "or a larger max_chars; no re-fetch]"
        )
    return part


# ── Reading ──────────────────────────────────────────────────────────────────

def read(
    url: str,
    max_chars: int = _DEFAULT_MAX_CHARS,
    browser: bool = False,
    js: bool = False,
    offset: int = 0,
    fresh: bool = False,
) -> str:
    """Fetch a URL and return its text: a web page's main content (markup/nav/ads
    stripped), a PDF's text page by page, a text/JSON/CSV/XML body as it is.
    Any other file (image, zip, Office…) comes back as one line naming its type
    and size — get those bytes with httpx yourself.

    A web page has two rungs: a plain HTTP fetch, then the persistent real
    browser when that comes back empty — which is what gets past sites that
    refuse automation and what renders a client-side page. `browser=True` (or
    the older `js=True`) skips straight to it, worth naming when a site is known
    to block.

    Returns max_chars from `offset`, with an explicit marker saying what is
    left. The full text is kept for 15 minutes, so a larger max_chars or the
    next offset costs no fetch; `fresh=True` fetches again. Several URLs at
    once: read_many(urls) (also `read.many`).
    """
    browser = browser or js
    if not fresh:
        hit = _cache_get(url, browser)
        if hit is not None:
            return _window(hit, max_chars, offset)
    got = _read_full(url, browser)
    if not got.ok:
        return got.text
    _cache_put(url, got.text, got.via_browser)
    return _window(got.text, max_chars, offset)


def read_many(
    urls: list[str],
    max_chars: int = _DEFAULT_MAX_CHARS,
    browser: bool = False,
    offset: int = 0,
    fresh: bool = False,
) -> dict[str, str]:
    """read() several URLs at once → {url: text}, in the order given.

    Each value is what read(url, ...) would return; a URL that fails gets its
    failure message, never an exception, so one bad link costs only its own
    entry. Plain fetches run in parallel; pages that need the real browser
    take turns in it.
    """
    if isinstance(urls, str):
        urls = [urls]
    order = list(dict.fromkeys(urls))
    if not order:
        return {}

    def one(u: str) -> str:
        try:
            return read(u, max_chars=max_chars, browser=browser, offset=offset, fresh=fresh)
        except Exception as exc:
            return f"Failed to read {u!r}: {_rung_error(type(exc).__name__, exc)}"

    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(
        max_workers=min(_READ_MANY_WORKERS, len(order)), thread_name_prefix="read_many"
    )
    try:
        futures = {u: pool.submit(one, u) for u in order}
        return {u: f.result() for u, f in futures.items()}
    finally:
        # Not `with`: its exit waits for every read, and an interrupted cell
        # (the 60 s limit) must come back now, not after the slowest URL.
        pool.shutdown(wait=False, cancel_futures=True)


# Reachable from a kernel that preloaded only `read`.
setattr(read, "many", read_many)  # noqa: B010
