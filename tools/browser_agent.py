"""browser-use powered smart_browser tool with native langgraph HITL.

This module replaces the bare Playwright tools with a single high-level
``smart_browser(objective)`` tool. Internally it wraps a ``browser_use.Agent``
which drives its own LLM loop to navigate, click, type, and extract. HITL
is exposed via an ``ask_human`` action registered on a ``Controller`` — the
browser LLM can invoke it like any other action, and its body calls
``langgraph.types.interrupt()`` to pause the entire deepagent graph until
the user replies.
"""

from __future__ import annotations

import logging
import threading

from browser_use import ActionResult, Agent as BrowserAgent, Browser, ChatGoogle, Controller
from langchain_core.tools import tool
from langgraph.config import get_stream_writer
from langgraph.types import interrupt

logger = logging.getLogger(__name__)

BROWSER_LLM_MODEL = "gemma-4-31b-it"
MAX_BROWSER_STEPS = 30

# ── Persistent browser session ──────────────────────────────────────────────
# A single Chromium instance is reused across every smart_browser call for
# the life of the process. This is critical for the interrupt+resume flow:
# when interrupt() fires, the node re-executes from the start, but the
# browser's URL / cookies / open tabs persist so the LLM picks up where it
# left off.
_browser_lock = threading.Lock()
_browser: Browser | None = None


def _get_browser() -> Browser:
    """Return a live Browser singleton, recreating it if the CDP connection died."""
    global _browser
    with _browser_lock:
        if _browser is not None and not _browser.is_cdp_connected:
            logger.info("Browser CDP connection is dead — recreating")
            _browser = None
        if _browser is None:
            _browser = Browser(headless=False)
        return _browser


# ── Controller + ask_human action ───────────────────────────────────────────
controller = Controller()


@controller.action(
    description=(
        "Ask the human user a question when you need information you cannot "
        "obtain from the page — credentials, 2FA code, which option to pick, "
        "confirmation before a destructive action, or any missing field you "
        "have no way to infer. The user's answer is returned as a string."
    ),
)
async def ask_human(question: str) -> ActionResult:
    """Pause the entire deepagent graph and wait for the user's reply.

    ``interrupt()`` raises ``GraphInterrupt`` which propagates out through
    ``await agent.run()`` → ``smart_browser`` → the deepagent subgraph, and
    langgraph saves state at the checkpointer. On resume via
    ``Command(resume=answer)``, the node re-executes and this ``interrupt()``
    call returns the user's answer immediately.
    """
    answer = interrupt({"reason": question})
    return ActionResult(extracted_content=f"The user responded: {answer}")


# ── The tool exposed to the deepagent ───────────────────────────────────────
@tool
async def smart_browser(objective: str) -> str:
    """Use a real Chromium browser to accomplish a web objective.

    Provide a clear, complete natural-language description of what you want
    done — e.g. "log into my portal at https://example.com with the email
    I'll provide and download the latest invoice". The browser agent will
    navigate, click, type, extract, and handle multi-step sequences on its
    own. If it runs into a login, 2FA, or any decision it cannot resolve
    from page content, it will pause and ask the user.

    Returns a summary of what was accomplished or the information extracted.
    """
    try:
        writer = get_stream_writer()
    except Exception:
        writer = None

    browser = _get_browser()
    llm = ChatGoogle(model=BROWSER_LLM_MODEL)

    async def on_step_end(ag: BrowserAgent) -> None:
        if writer is None or not ag.history.history:
            return
        last = ag.history.history[-1]
        if last.model_output is None:
            return
        try:
            thought = last.model_output.current_state.model_dump()
            actions = [a.model_dump() for a in (last.model_output.action or [])]
            writer({
                "type": "browser_step",
                "thought": thought,
                "actions": actions,
            })
        except Exception as exc:  # never let streaming break the browser loop
            logger.debug("browser_step writer failed: %s", exc)

    agent = BrowserAgent(
        task=objective,
        llm=llm,
        browser=browser,
        controller=controller,
    )

    history = await agent.run(on_step_end=on_step_end, max_steps=MAX_BROWSER_STEPS)
    return history.final_result() or "Browser task completed (no explicit result returned)."
