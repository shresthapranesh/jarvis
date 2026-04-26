"""Agent and subagent definitions."""

import sqlite3 as _sqlite3
from functools import lru_cache

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StoreBackend
from deepagents.backends.local_shell import LocalShellBackend
from deepagents.middleware.subagents import CompiledSubAgent, SubAgent
from deepagents.middleware.summarization import create_summarization_tool_middleware

from .logging_middleware import log_tool_calls
from .strip_thinking_middleware import StripThinkingMiddleware
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite.aio import AsyncSqliteStore

from .config import get_config  # noqa: F401
from .model_catalog import (  # noqa: F401 — re-exported for backwards compat
    AVAILABLE_MODELS,
    DEFAULT_MODEL,
    ModelSpec,
    is_valid_model,
)
from tools.code import run_python
from tools.datetime import get_current_datetime
from tools.files import list_files, read_file, write_file
from tools.finance import (
    compare_stocks,
    get_earnings,
    get_historical_prices,
    get_stock_data,
    get_ticker_news,
)
from tools.automations import (
    create_automation,
    delete_automation,
    list_automations,
    update_automation,
)
from tools.browser_agent import smart_browser
from tools.web import extract_links, fetch_page, playwright_browse, web_search
from tools.workflows import (
    create_workflow,
    delete_workflow,
    list_workflows,
    update_workflow,
)

# ── Subagent definitions ─────────────────────────────────────────────────────

subagents: list[SubAgent | CompiledSubAgent] = [
    {
        "name": "web_researcher",
        "description": (
            "Executes multi-step browser tasks on a real Chromium window: logins, form filling, "
            "navigating dynamic sites, and extracting information from JavaScript-rendered pages. "
            "Can pause to ask the user for credentials or decisions mid-task."
        ),
        "system_prompt": (
            "You are a web browsing specialist. You have ONE tool: smart_browser(objective). "
            "Call it with a clear, complete natural-language description of what the user wants "
            "done — including URLs, which fields to fill, what information to extract, and any "
            "known constraints. The browser agent will handle the full click/type/navigate "
            "sequence autonomously and will prompt the user if it hits a login, 2FA, or "
            "ambiguous choice. After smart_browser returns, summarize its findings for the "
            "parent agent."
        ),
        "tools": [smart_browser],
    },
    {
        "name": "researcher",
        "description": (
            "Searches the web and reads full articles to gather up-to-date information on any topic. "
            "Use for questions that require current facts, news, documentation, or source URLs."
        ),
        "system_prompt": (
            "You are a thorough web researcher. Use web_search to find relevant information, "
            "then fetch_page to read full articles. Use playwright_browse for pages that require "
            "JavaScript to render content (fetch_page will return empty or incomplete results for these). "
            "Use extract_links to discover additional sources. "
            "Use get_current_datetime to know today's date when searching for recent content. "
            "Use read_file to check for any prior research on the same topic. "
            "Return a structured summary with key facts and source URLs."
        ),
        "tools": [get_current_datetime, web_search, fetch_page, playwright_browse, extract_links, read_file],
    },
    {
        "name": "coder",
        "description": (
            "Writes and executes Python code to solve computational problems, perform data analysis, "
            "run calculations, process files, or automate tasks. Use whenever the task involves "
            "numbers, data transformation, algorithms, or anything that benefits from code."
        ),
        "system_prompt": (
            "You are an expert Python developer. Write clean, correct Python code and run it with "
            "run_python to verify results. For data analysis tasks, prefer concise scripts that print "
            "their output. Use read_file / write_file to work with files on disk. "
            "Always show the code you ran and its output in your response."
        ),
        "tools": [run_python, read_file, write_file, list_files],
    },
    {
        "name": "financial_analyst",
        "description": (
            "Fetches live stock prices, market cap, P/E ratios, historical prices, earnings dates, "
            "and company news for publicly traded companies. Use for any question about specific "
            "tickers, financial metrics, or market data."
        ),
        "system_prompt": (
            "You are a financial analyst. Use get_stock_data for current metrics, "
            "get_historical_prices for price trends, get_earnings for upcoming earnings, "
            "compare_stocks to benchmark multiple tickers side by side, and get_ticker_news "
            "for company-specific news. Return a structured summary of the financials."
        ),
        "tools": [get_stock_data, get_historical_prices, get_earnings, compare_stocks, get_ticker_news],
    },
    {
        "name": "writer",
        "description": (
            "Synthesizes information from other agents into a polished, well-structured document "
            "and saves it to disk. Use to produce a final deliverable after research or analysis is done."
        ),
        "system_prompt": (
            "You are a skilled writer. Synthesize findings into a clear, well-structured markdown document. "
            "Adapt the format to the content — reports, summaries, how-to guides, analyses, etc. "
            "Use get_current_datetime to date the document. "
            "Use list_files to check what already exists, then save with write_file to outputs/<name>.md."
        ),
        "tools": [get_current_datetime, write_file, read_file, list_files],
    },
]


# ── Checkpointer + agent builder ────────────────────────────────────────────

_sync_checkpointer: SqliteSaver | None = None


def _get_sync_checkpointer() -> SqliteSaver:
    """Lazily open the CLI's sync SqliteSaver. Avoids a file-handle side effect
    at import time — server.py uses the async checkpointer and never needs this."""
    global _sync_checkpointer
    if _sync_checkpointer is None:
        conn = _sqlite3.connect(get_config().checkpoints_db, check_same_thread=False)
        _sync_checkpointer = SqliteSaver(conn=conn)
    return _sync_checkpointer


def _build_backend(store: AsyncSqliteStore | None = None):
    """Build the filesystem backend.

    When a store is provided (server path), memory/ is routed to StoreBackend
    backed by SQLite so memory persists across threads and server restarts.
    When store is None (CLI path), plain LocalShellBackend is used and
    memory/AGENTS.md is read from disk as before."""
    shell = LocalShellBackend(root_dir=".", virtual_mode=True)
    if store is None:
        return shell
    memory_backend = StoreBackend(
        store=store,
        namespace=lambda ctx: ("memory",),
    )
    return CompositeBackend(
        default=shell,
        routes={"memory/": memory_backend},
    )


@lru_cache(maxsize=16)
def _build_cached(model: str, checkpointer, store):
    """Process-wide compiled-graph cache keyed on (model, checkpointer, store identity).
    Graph compilation (middleware stack, subagent wiring, tool binding) is
    non-trivial and stable across requests; the server reuses one async
    checkpointer, so every call after the first is a cache hit.

    Context compression: deepagents injects an automatic SummarizationMiddleware
    into the main agent and every subagent (see deepagents/graph.py:297,334,375)
    with model-aware defaults — trigger around ~170k tokens, keep the last 6
    messages verbatim, offload the rest to conversation_history/{thread_id}.md.
    We additionally register a `compact_conversation` tool via
    `create_summarization_tool_middleware` so the agent can choose to compact
    between tasks rather than waiting for the hard limit."""
    backend = _build_backend(store)
    spec = next((m for m in AVAILABLE_MODELS if m.id == model), None)
    if spec is None:
        raise ValueError(f"Unknown model '{model}'")
    llm = spec.build_llm()
    return create_deep_agent(
        model=llm,
        tools=[
            list_automations,
            create_automation,
            update_automation,
            delete_automation,
            list_workflows,
            create_workflow,
            update_workflow,
            delete_workflow,
        ],
        subagents=subagents,
        backend=backend,
        memory=[get_config().memory_file],
        checkpointer=checkpointer,
        middleware=[StripThinkingMiddleware(), create_summarization_tool_middleware(llm, backend), log_tool_calls],
    )


def build_agent(model: str = DEFAULT_MODEL, checkpointer=None, store: AsyncSqliteStore | None = None):
    """Build a deepagent. Defaults to the sync SqliteSaver for CLI usage;
    server.py passes an AsyncSqliteSaver and AsyncSqliteStore for astream-based execution."""
    if checkpointer is None:
        checkpointer = _get_sync_checkpointer()
    return _build_cached(model, checkpointer, store)
