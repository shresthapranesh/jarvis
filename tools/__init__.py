"""What the agent's notebook kernels preload. Import the module you need —
this package imports nothing, because every kernel imports from it at boot.

* `sdk.py` — the `jarvis` SDK (with `text_dedupe.py`).
* `research.py` — the **web**: `search()` for leads, `read()` for a page's
  text. Owns the fetch-and-extract ladder (httpx → headless Chromium →
  the real browser below) and knows nothing about driving a session.
* `browser.py` — the **browser**: one persistent, logged-in Chromium reached
  over CDP. Owns finding/launching it, the dedicated profile, the tab, and the
  challenge→human handoff. Knows nothing about extracting text.

`research.py` depends on `browser.py`; never the reverse. Anything that reads
*content* belongs in the first, anything that drives a *session* in the second.
"""
