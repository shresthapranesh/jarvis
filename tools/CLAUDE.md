# tools/ — what the agent's kernels preload: the `jarvis` SDK, web and browser

## Bound vs SDK
Tool schemas are re-sent on every LLM call, so only tools coupled to the agent **loop** are bound — the server runs them (`edge/src/agent/tools.rs`, schemas in `tools.json`):

| Tool | Why it stays bound |
|---|---|
| `run_cell` | the door into the per-conversation kernel |
| `write_todos` / `set_todo_status` | write the run's thread state |
| `complete_task` / `block_task` | act on the current board run; bound only on board runs |
| `run_agents` / `check_agents` / `run_workflow` | sub-agents (each a conversation of its own, re-usable by id) and workflows run on the server |
| `write_artifact` | its live event belongs to the run |
| `remember` (only with an embedder) | a memory write the loop makes |

Everything else lives in `sdk.py`, preloaded as `jarvis` in every kernel and discovered with `jarvis.help()` / `jarvis.help("<category>")`. Workers also get `read_file`/`write_file`/`list_files` and the artifact reads (`edge/src/agent/files.rs`, `artifacts.rs`).

## Adding to the SDK
- Define the function in `sdk.py` and register it in `_CATEGORIES`. The docstring is the only documentation `help()` shows — and its first lines are what a wrong call shows: `_policed` checks arguments with `Signature.bind` after the policy gate and raises a `TypeError` with the real signature and docstring head (`_usage`). A `TypeError` from the function body passes through untouched.
- **Reads** use the `mode=ro` sqlite connection. **Writes** — and anything the server holds, like embedding a query (`searchMemory`) — go through `api()` → the server's GraphQL (so scheduler reloads, board dispatch, approval gating and validation happen). Mutations take Relay GlobalIDs — use `_global_id()`. Endpoint is `$JARVIS_API_URL` (default `http://127.0.0.1:8000/graphql`; the server sets it for its kernels).
- The SDK imports only the standard library, `httpx` and `tools.text_dedupe` (`numpy` is the agent's, not the SDK's). It finds the database as the server does (`DATABASE_URL`, else `$WORK_DIR/database.db`), and the tool policy is the `tools.policy` setting, re-read every 2 s.
- Need a side effect with no mutation? Add the mutation; don't write the DB from the kernel.
- Scope (`_conversation_id`, `_project_id`) is injected per kernel by the server (`edge/src/kernels/`) — the agent can't choose a project. `list_conversations` raises outside a project; all conversation reads drop incognito (`ephemeral`) conversations.

## Web vs browser
- `research.py` is the **web**: `search()` (Tavily/Brave, ddgs fallback), `read(url)` and `read_many(urls)`. `browser.py` is the **browser**: one persistent headed Chromium reached over CDP. research may import browser, never the reverse.
- `read()` goes by the response's content type (else the URL's extension, else the first bytes). HTML has two rungs: httpx + trafilatura, then the real browser (`browser=True` goes straight there). There is deliberately no headless rung, and `read()` must not import Playwright (a test pins this). No `playwright install` needed.
- A PDF is text page by page via pypdf, imported inside the function (as trafilatura is) so kernel start stays cheap — a test pins that too. Text/JSON/CSV/XML come back as they are; any other binary is one line (type, size). None of these escalate to the browser. Downloads are streamed and capped: 10 MB for text/HTML (read up to it), 40 MB for PDFs (refused past it), a 1 KB sniff for binaries.
- Extracted full text is cached per kernel by URL (`_cache`: 15 min, 32 entries, 16 M chars; failures and binary notes aren't cached; `browser=True` uses only browser-made text; `fresh=True` bypasses), so `max_chars`/`offset` windows re-slice it without a fetch.
- `read_many` runs `read` on ≤ 6 threads, failures as their message. The browser rung is behind `_BROWSER_LOCK` — one shared browser, one tab driven at a time. The kernel bootstrap (`edge/src/kernels/mod.rs`) preloads it with `search`/`read`; `read.many` is the same function.
- The browser is its own process both server and kernels attach to; its dedicated profile is required (Chromium refuses remote debugging on the default profile) and is the containment boundary.
- In the kernel use `async with apage() as tab:` — the sync `page()` raises inside the kernel's event loop. Browser tests are `async def` on purpose.
- Challenge pages park on an `Approval` row for the human to clear in the visible window; skipped when there's no conversation.
- Config: `browser.cdp_url`, `browser.executable`, `browser.profile_dir` (or `JARVIS_BROWSER_*`). Never `chromium.launch()`.
- `browser.close()` on a CDP-attached browser disconnects, it doesn't quit — the persistent profile relies on that.

### Live browser view (`/ws/browser`)
The server's (`edge/src/browser/`), attached to the same browser the kernels drive.
- Frames come from CDP `Page.startScreencast` on the server's own CDP client; sent as binary WebSocket messages, not GraphQL.
- Screencast runs only while someone watches (ref-counted). Per-subscriber queues are depth 1, drop-oldest. A tab is opened (`PUT /json/new`) when none exist; one screenshot is sent on attach, since screencast only emits on paint.
- The UI's Browser button follows the `browserAvailable` query, not run events. The agent's prompt mentions the browser only when one is running.

## Package imports
`tools/__init__.py` imports nothing — kernels import `tools.research` at boot and must stay fast. Don't add a registry there.

`preload.py` is what every kernel preloads (`from tools.preload import *`, also the agent's restore line after `%reset -f`): `search`/`read`, `jarvis`, `httpx`, `datetime`, `subprocess`. Keep it cheap — this runs on low-RAM hardware; pandas, numpy and yfinance are installed, not preloaded, and a lazy binding doesn't hold (the first traceback's `inspect.getmodule` touches every module in `sys.modules`). Change the prompt's preload lines (`edge/src/system_prompt.md`) with it.
