"""Primary code-execution tool — the agent's main action surface."""

import asyncio
import logging
import os
import sys
import tempfile
import time

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


@tool
async def execute(code: str) -> str:
    """Execute Python code in a subprocess and return the combined stdout/stderr output.

    This is the primary tool for all computational tasks. You have full access to
    everything installed in the Python environment.

    Stateless: each call runs in a FRESH subprocess. Variables, imports, and
    in-memory data do NOT persist between calls — batch related work into one
    call rather than splitting it across many.

    Common patterns:
      Web requests:      import httpx; r = httpx.get("https://..."); print(r.text)
      Async web:         import asyncio, httpx
                         async def main(): ...
                         asyncio.run(main())
      JS-rendered pages: from playwright.sync_api import sync_playwright
                         with sync_playwright() as p:
                             browser = p.chromium.launch()
                             page = browser.new_page()
                             page.goto("https://...")
                             print(page.content())
                             browser.close()
      Financial data:    import yfinance as yf; print(yf.Ticker("AAPL").info)
      Data analysis:     import pandas as pd, numpy as np; ...
      Current date/time: import datetime; print(datetime.datetime.now())
      Shell commands:    import subprocess; print(subprocess.check_output(["ls", "-la"]).decode())
      File operations:   from pathlib import Path; print(Path("file.txt").read_text())

    Timeout: 60s. No pip install at runtime — use packages already in the venv.
    """
    start = time.monotonic()
    logger.info("→ execute (%d chars of code)", len(code))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        fname = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, fname,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "Error: execution timed out (60s limit)"
        except asyncio.CancelledError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.communicate(), timeout=5)
            except asyncio.TimeoutError:
                pass
            raise
        out = stdout.decode().strip()
        err = stderr.decode().strip()
        if err:
            out = (out + "\n[stderr]\n" + err).strip()
        result = out or "(no output)"
        logger.info("← execute (%d chars, %.0fms)", len(result), (time.monotonic() - start) * 1000)
        return result
    finally:
        os.unlink(fname)
