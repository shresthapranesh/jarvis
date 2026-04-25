"""browser-use powered smart_browser tool with native langgraph HITL.

Uses a persistent BrowserAgent task per conversation thread, kept alive
across LangGraph interrupts so the browser never restarts mid-task.
ask_human suspends via an asyncio.Queue instead of calling interrupt()
directly; smart_browser handles the LangGraph interrupt at the tool level
and resumes the waiting agent after the user answers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from browser_use import ActionResult, Agent as BrowserAgent, Browser, ChatGoogle, Controller
from langchain_core.tools import tool
from langgraph.config import get_config, get_stream_writer
from langgraph.types import interrupt

logger = logging.getLogger(__name__)

BROWSER_LLM_MODEL = "gemma-4-31b-it"
MAX_BROWSER_STEPS = 30

# ── Persistent browser session ───────────────────────────────────────────────

_browser_lock = asyncio.Lock()
_browser: Browser | None = None


def _get_browser() -> Browser:
    global _browser
    if _browser is not None and not _browser.is_cdp_connected:
        logger.info("Browser CDP connection is dead — recreating")
        _browser = None
    if _browser is None:
        _browser = Browser(headless=False)
    return _browser


# ── Per-conversation session state ───────────────────────────────────────────

@dataclass
class _BrowserSession:
    agent: BrowserAgent
    task: asyncio.Task
    question_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    answer_queue: asyncio.Queue = field(default_factory=asyncio.Queue)


_sessions: dict[str, _BrowserSession] = {}

# ── Controller ───────────────────────────────────────────────────────────────

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
    """Signal smart_browser to interrupt and wait for the user's reply.

    Puts the question on a queue and suspends until smart_browser delivers
    the user's answer. No LangGraph interrupt() is called here — the
    interrupt happens at the smart_browser tool level instead, so the
    BrowserAgent task stays alive across the interrupt/resume cycle.
    """
    thread_id = get_config()["configurable"]["thread_id"]
    session = _sessions.get(thread_id)
    if session is None:
        return ActionResult(extracted_content="Error: no active browser session.")
    await session.question_queue.put(question)
    answer = await session.answer_queue.get()
    return ActionResult(extracted_content=f"The user responded: {answer}")


# ── Tool ─────────────────────────────────────────────────────────────────────

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
    thread_id = get_config()["configurable"]["thread_id"]

    if thread_id not in _sessions:
        # First call — start the browser agent as a background task.
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
                writer({"type": "browser_step", "thought": thought, "actions": actions})
            except Exception as exc:
                logger.debug("browser_step writer failed: %s", exc)

        agent = BrowserAgent(
            task=objective,
            llm=llm,
            browser=browser,
            controller=controller,
        )
        session = _BrowserSession(
            agent=agent,
            task=asyncio.create_task(
                agent.run(on_step_end=on_step_end, max_steps=MAX_BROWSER_STEPS)
            ),
        )
        _sessions[thread_id] = session
    else:
        # Resuming after interrupt — deliver the user's answer to ask_human.
        session = _sessions[thread_id]
        answer = interrupt({"reason": "_resume_"})  # returns immediately on resume
        await session.answer_queue.put(answer)

    return await _drive_session(thread_id)


async def _drive_session(thread_id: str) -> str:
    """Wait until the agent finishes or asks a question, then act accordingly."""
    session = _sessions[thread_id]
    question_waiter = asyncio.create_task(session.question_queue.get())

    try:
        done, _ = await asyncio.wait(
            {session.task, question_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        question_waiter.cancel()
        session.task.cancel()
        _sessions.pop(thread_id, None)
        raise

    if session.task in done:
        # Agent finished (successfully or with an error).
        question_waiter.cancel()
        _sessions.pop(thread_id, None)
        try:
            history = session.task.result()
            return history.final_result() or "Browser task completed (no explicit result)."
        except Exception as exc:
            return f"Browser task failed: {exc}"

    # ask_human fired — forward the question to the user via LangGraph interrupt.
    question = question_waiter.result()
    interrupt({"reason": question})  # raises GraphInterrupt; smart_browser exits here
    return ""  # unreachable — satisfies type checker
