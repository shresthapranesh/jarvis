"""Agent tools. Import the module you need — this package imports nothing.

It used to export a flat `TOOLS` list, from back when one registry was how the
agent got its toolset. Nothing has read it since `core/agents.py` started
composing per-role tool lists by importing each module directly, but it stayed
and kept eagerly importing `tools.code` (and through it LangChain/LangGraph),
`tools.finance` (yfinance) and the old `tools.web` — ~380ms and a large chunk
of the dependency tree, paid by **every** process that touched any tool module.
The kernel pays it at boot for `from tools.research import search, read`, where
none of it is wanted.

The layout, so the split stays deliberate:

* `research.py` — the **web**: `search()` for leads, `read()` for a page's
  text. Owns the fetch-and-extract ladder (httpx → headless Chromium →
  the real browser below) and knows nothing about driving a session.
* `browser.py` — the **browser**: one persistent, logged-in Chromium reached
  over CDP. Owns finding/launching it, the dedicated profile, the tab, and the
  challenge→human handoff. Knows nothing about extracting text.

`research.py` depends on `browser.py`; never the reverse. Anything that reads
*content* belongs in the first, anything that drives a *session* in the second.
"""
