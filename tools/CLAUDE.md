# tools/ — agent tools and the `jarvis` SDK

## Bound vs SDK
Tool schemas are re-sent on every LLM call, so only tools coupled to the agent **loop** are bound (`core/agents.py:_build_agent`):

| Tool | Why it stays bound |
|---|---|
| `run_cell` (`code.py`) | the door into the per-conversation kernel |
| `write_todos` / `set_todo_status` | write the run's thread state (`ToolContext.thread`) |
| `complete_task` / `block_task` (`board.py`) | act on the current board run; bound only with `build_agent(board=True)` |
| `spawn_workers` / `run_workflow` | run agents on this agent's LLM |
| `write_artifact` | its live event goes through this run's stream writer |
| `remember` (only with an embedder) | there is no `createMemory` mutation to route to |

Everything else lives in `sdk.py`, preloaded as `jarvis` in every kernel and discovered with `jarvis.help()` / `jarvis.help("<category>")`. `files.py` and `documents.py` are bound only to worker roles (`_ROLE_TOOLS`). `skills.py`, `projects.py`, `finance.py`, `datetime.py` are unbound.

## Adding to the SDK
- Define the function in `sdk.py` and register it in `_CATEGORIES`. The docstring is the only documentation `help()` shows.
- **Reads** use the `mode=ro` sqlite connection. **Writes** go through `api()` → the server's GraphQL mutations (so scheduler registration, board dispatch, approval gating and validation happen). Mutations take Relay GlobalIDs — use `_global_id()`. Endpoint is `$JARVIS_API_URL` (default `http://127.0.0.1:8000/graphql`).
- Need a side effect with no mutation? Add the mutation; don't write the DB from the kernel.
- Scope (`_conversation_id`, `_project_id`) is injected per kernel by `core/kernels.py` — the agent can't choose a project. `list_conversations` raises outside a project; all conversation reads drop incognito (`ephemeral`) conversations.
- Any SDK function that reads documents must wait on indexing first (`_wait_for_index`).

## Web vs browser
- `research.py` is the **web**: `search()` (Tavily/Brave, ddgs fallback) and `read(url)`. `browser.py` is the **browser**: one persistent headed Chromium reached over CDP. research may import browser, never the reverse.
- `read()` has two rungs: httpx + trafilatura, then the real browser (`browser=True` goes straight there). There is deliberately no headless rung, and `read()` must not import Playwright (a test pins this). No `playwright install` needed.
- The browser is its own process both server and kernels attach to; its dedicated profile is required (Chromium refuses remote debugging on the default profile) and is the containment boundary.
- In the kernel use `async with apage() as tab:` — the sync `page()` raises inside the kernel's event loop. Browser tests are `async def` on purpose.
- Challenge pages park on an `Approval` row for the human to clear in the visible window; skipped when there's no conversation.
- Config: `browser.cdp_url`, `browser.executable`, `browser.profile_dir` (or `JARVIS_BROWSER_*`). Never `chromium.launch()`.
- `browser.close()` on a CDP-attached browser disconnects, it doesn't quit — the persistent profile relies on that.

### Live browser view (`core/browser_stream.py` → `/ws/browser`)
- Frames come from CDP `Page.startScreencast` on the server's own CDP client; sent as binary WebSocket messages, not GraphQL.
- Screencast runs only while someone watches (ref-counted). Per-subscriber queues are depth 1, drop-oldest.
- `_Subscriber` must be `@dataclass(eq=False)` (it lives in a set). `_ensure_page` opens a tab via `PUT /json/new` when none exist. `_prime()` grabs one screenshot on attach, since screencast only emits on paint.
- The UI's Browser button follows the `browserAvailable` query, not run events.
- `_browser_volatile_parts()` tells the agent about the browser only when one is running.

## Package imports
`tools/__init__.py` imports nothing — kernels import `tools.research` at boot and must stay fast. Don't add a registry there.
