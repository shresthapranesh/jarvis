"""`read()` by what a URL returns, `read_many()`, and the per-kernel cache.

Nothing here touches the network: responses come from an `httpx.MockTransport`
put where `read()` builds its client, and the PDFs are written by hand below.
The browser rung fails the test unless a test says otherwise — none of these
bodies should ever need it.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from tools import research

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://example.test"


def _pdf(pages: list[str]) -> bytes:
    """A minimal PDF with one line of Helvetica text per page."""
    n = len(pages)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(n))}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


class _Chunks(httpx.SyncByteStream):
    """A long body that counts how much of it was pulled."""

    def __init__(self, chunk: bytes, count: int) -> None:
        self.chunk, self.count, self.sent = chunk, count, 0

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(self.count):
            self.sent += 1
            yield self.chunk


Handler = Callable[[httpx.Request], httpx.Response]


class Site:
    def __init__(self) -> None:
        self.routes: dict[str, Handler] = {}
        self.hits: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hits.append(request.url.path)
        handler = self.routes.get(request.url.path)
        return handler(request) if handler else httpx.Response(404)

    def serve(self, path: str, body: bytes | str, content_type: str | None = None) -> str:
        data = body.encode() if isinstance(body, str) else body
        headers = {"content-type": content_type} if content_type else {}
        self.routes[path] = lambda _: httpx.Response(200, headers=headers, content=data)
        return BASE + path


@pytest.fixture
def site(monkeypatch) -> Iterator[Site]:
    s = Site()
    monkeypatch.setattr(research, "_transport", httpx.MockTransport(s))
    monkeypatch.setattr(research, "_read_cdp", lambda url: pytest.fail(f"went to the browser for {url}"))
    research._cache_clear()
    yield s
    research._cache_clear()


ARTICLE = (
    "<html><head><title>Tides</title></head><body><nav>Home | About</nav><article>"
    "<h1>How tides work</h1>"
    + "".join(f"<p>Paragraph {i}: the moon's pull raises a bulge of water on the near side of the earth, "
              f"and inertia raises another on the far side, so most coasts see two tides a day.</p>"
              for i in range(8))
    + "</article></body></html>"
)


# ── By content type ──────────────────────────────────────────────────────────

def test_html_is_extracted_as_before(site):
    url = site.serve("/tides", ARTICLE, "text/html; charset=utf-8")
    out = research.read(url)
    assert "How tides work" in out and "two tides a day" in out
    assert "<p>" not in out and "Home | About" not in out


def test_a_pdf_is_read_page_by_page(site):
    url = site.serve("/report", _pdf(["Revenue grew nine percent", "Costs fell two percent"]), "application/pdf")
    out = research.read(url)
    assert "[page 1 of 2]" in out and "Revenue grew nine percent" in out
    assert "[page 2 of 2]" in out and "Costs fell two percent" in out
    assert out.index("Revenue") < out.index("Costs")


@pytest.mark.parametrize(
    "path,content_type",
    [
        ("/paper.pdf", "application/octet-stream"),  # by the extension
        ("/download?id=3", None),                    # by the first bytes
    ],
)
def test_an_undeclared_pdf_is_recognised(site, path, content_type):
    url = site.serve(path.split("?")[0], _pdf(["Recognised without a type"]), content_type)
    assert "Recognised without a type" in research.read(BASE + path if "?" in path else url)


def test_a_long_pdf_is_truncated_with_the_marker(site):
    url = site.serve("/long", _pdf([f"Page text number {i} " * 10 for i in range(6)]), "application/pdf")
    out = research.read(url, max_chars=300)
    assert "[page 1 of 6]" in out and "[page 6 of 6]" not in out
    assert "truncated" in out and "offset=300" in out


def test_a_pdf_with_no_text_says_so(site):
    url = site.serve("/scan", _pdf([""]), "application/pdf")
    out = research.read(url)
    assert "no extractable text" in out and "OCR" in out


def test_a_broken_pdf_says_so(site):
    url = site.serve("/broken", b"%PDF-1.4\nthis is not really a pdf", "application/pdf")
    assert research.read(url).startswith("Could not extract text from the PDF")


def test_a_pdf_over_the_ceiling_is_not_downloaded(site, monkeypatch):
    monkeypatch.setattr(research, "_PDF_DOWNLOAD_CAP", 200)
    url = site.serve("/huge", _pdf(["too big to fetch"] * 5), "application/pdf")
    out = research.read(url)
    assert "over read()'s" in out and "pypdf" in out
    assert "too big to fetch" not in out


@pytest.mark.parametrize(
    "path,body,content_type",
    [
        ("/data.json", '{"rate": 4.25, "unit": "percent"}', "application/json"),
        ("/notes", "plain notes, short ones", "text/plain; charset=utf-8"),
        ("/table", "year,rate\n2025,4.25\n", "text/csv"),
        ("/feed", "<?xml version='1.0'?><rss><channel><title>t</title></channel></rss>", "application/rss+xml"),
        ("/readme.md", "# Title\n\nbody", None),
    ],
)
def test_text_comes_back_as_it_is_without_the_browser(site, path, body, content_type):
    # Every body here is under the browser's 400-char threshold: an HTML page
    # that short would escalate, these must not.
    url = site.serve(path, body, content_type)
    assert research.read(url) == body


def test_a_binary_is_named_not_read(site):
    stream = _Chunks(b"\x89PNG\r\n\x1a\n" + b"\0" * 65_528, 100)
    site.routes["/photo"] = lambda _: httpx.Response(
        200, headers={"content-type": "image/png", "content-length": str(65_536 * 100)}, stream=stream
    )
    out = research.read(BASE + "/photo")
    assert "image/png" in out and "6.2 MB" in out
    assert "httpx.get(url, follow_redirects=True).content" in out
    assert "\n" not in out
    assert stream.sent <= 1  # the sniffing head, not 6 MB


def test_a_text_download_stops_at_the_ceiling(site, monkeypatch):
    monkeypatch.setattr(research, "_TEXT_DOWNLOAD_CAP", 10_000)
    stream = _Chunks(b"a" * 1_000, 1_000)
    site.routes["/log"] = lambda _: httpx.Response(200, headers={"content-type": "text/plain"}, stream=stream)
    out = research.read(BASE + "/log", max_chars=1_000_000)
    assert out.startswith("a" * 10_000)
    assert "download stopped" in out
    assert stream.sent <= 11


# ── Windows and the cache ────────────────────────────────────────────────────

def test_offset_pages_through_one_fetch(site):
    url = site.serve("/digits", "0123456789" * 3, "text/plain")
    first = research.read(url, max_chars=10)
    assert first.startswith("0123456789\n... [truncated 20 chars — read(url, offset=10)")
    second = research.read(url, max_chars=10, offset=10)
    assert second.startswith("[chars 10–20 of 30]\n0123456789\n")
    assert "offset=20" in second
    last = research.read(url, max_chars=10, offset=20)
    assert "truncated" not in last
    assert "past the end" in research.read(url, offset=30)
    assert research.read(url, max_chars=100) == "0123456789" * 3
    assert site.hits == ["/digits"]


def test_fresh_fetches_again(site):
    url = site.serve("/n", "some text", "text/plain")
    research.read(url)
    research.read(url, fresh=True)
    assert site.hits == ["/n", "/n"]


def test_a_failed_read_is_not_cached(site, monkeypatch):
    monkeypatch.setattr(research, "_read_cdp", lambda url: "")
    answers = iter([httpx.Response(503), httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok now")])
    site.routes["/flaky"] = lambda _: next(answers)
    assert "No readable text" in research.read(BASE + "/flaky")
    assert research.read(BASE + "/flaky") == "ok now"


def test_a_message_about_a_binary_is_not_cached(site):
    site.routes["/zip"] = lambda _: httpx.Response(200, headers={"content-type": "application/zip"}, content=b"PK\x03\x04")
    research.read(BASE + "/zip")
    research.read(BASE + "/zip")
    assert site.hits == ["/zip", "/zip"]


def test_entries_expire(site, monkeypatch):
    monkeypatch.setattr(research, "_CACHE_TTL", -1)
    url = site.serve("/t", "text", "text/plain")
    research.read(url)
    research.read(url)
    assert site.hits == ["/t", "/t"]


def test_the_cache_is_bounded_in_entries(site, monkeypatch):
    monkeypatch.setattr(research, "_CACHE_ENTRIES", 2)
    for p in ("/a", "/b", "/c"):
        research.read(site.serve(p, f"body {p}", "text/plain"))
    research.read(BASE + "/c")
    research.read(BASE + "/a")  # the oldest went
    assert site.hits == ["/a", "/b", "/c", "/a"]


def test_the_cache_is_bounded_in_size(site, monkeypatch):
    monkeypatch.setattr(research, "_CACHE_CHARS", 50)
    research.read(site.serve("/a", "x" * 30, "text/plain"))
    research.read(site.serve("/b", "y" * 30, "text/plain"))
    research.read(BASE + "/a")
    assert site.hits == ["/a", "/b", "/a"]
    assert research._cache_chars <= 50


def test_an_explicit_browser_read_skips_text_the_browser_did_not_produce(site, monkeypatch):
    calls: list[str] = []

    def cdp(url: str) -> str:
        calls.append(url)
        return "from the real browser " * 40

    url = site.serve("/tides", ARTICLE, "text/html")
    research.read(url)
    monkeypatch.setattr(research, "_read_cdp", cdp)
    assert "from the real browser" in research.read(url, browser=True)
    assert "from the real browser" in research.read(url, browser=True)  # now cached
    assert len(calls) == 1


# ── read_many ────────────────────────────────────────────────────────────────

def test_read_many_reads_concurrently_in_order(site):
    barrier = threading.Barrier(3, timeout=5)

    def slow(request: httpx.Request) -> httpx.Response:
        barrier.wait()  # only passes when all three are in flight together
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=request.url.path.encode())

    urls = [BASE + p for p in ("/x", "/y", "/z")]
    for p in ("/x", "/y", "/z"):
        site.routes[p] = slow
    out = research.read_many(urls + [urls[0]])
    assert list(out) == urls
    assert out == {u: u.removeprefix(BASE) for u in urls}


def test_read_many_survives_a_bad_url(site, monkeypatch):
    good = site.serve("/good", "fine", "text/plain")
    real_fetch = research._fetch

    def fetch(url: str) -> research._Body:
        if url.endswith("/bad"):
            raise ValueError("no such thing")
        return real_fetch(url)

    monkeypatch.setattr(research, "_fetch", fetch)
    out = research.read_many([BASE + "/bad", good])
    assert out[good] == "fine"
    assert out[BASE + "/bad"].startswith(f"Failed to read {BASE + '/bad'!r}: ValueError: no such thing")


def test_read_many_takes_turns_in_the_browser(monkeypatch):
    research._cache_clear()

    def refused(url: str) -> research._Body:
        raise httpx.ConnectError("blocked")

    active = 0
    peak = 0
    lock = threading.Lock()

    def cdp(url: str) -> str:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return f"rendered {url} " * 40

    monkeypatch.setattr(research, "_fetch", refused)
    monkeypatch.setattr(research, "_read_cdp", cdp)
    urls = [f"{BASE}/{i}" for i in range(4)]
    out = research.read_many(urls)
    assert all(f"rendered {u}" in out[u] for u in urls)
    assert peak == 1
    research._cache_clear()


def test_read_many_is_reachable_from_read():
    assert research.read.many is research.read_many  # type: ignore[attr-defined]
    assert research.read_many([]) == {}


# ── Kernel start stays cheap ─────────────────────────────────────────────────

def test_importing_research_does_not_import_pypdf():
    code = "import sys, tools.research; print('pypdf' in sys.modules, 'playwright' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["False", "False"]
