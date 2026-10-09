# tools/ — what the agent's kernels preload: the `jarvis` SDK, web and browser

## Bound vs SDK
Tool schemas are re-sent on every LLM call, so only tools coupled to the agent **loop** are bound — the server runs them (`edge/src/agent/tools.rs`, schemas in `tools.json`):

| Tool | Why it stays bound |
|---|---|
| `run_cell` | the door into the per-conversation kernel |
| `write_todos` / `set_todo_status` | write the run's thread state |
| `complete_task` / `block_task` | act on the current board run; bound only on board runs |
| `spawn_workers` / `run_workflow` | run agents on this agent's model |
| `write_artifact` | its live event belongs to the run |
| `remember` (only with an embedder) | a memory write the loop makes |

Everything else lives in `sdk.py`, preloaded as `jarvis` in every kernel and discovered with `jarvis.help()` / `jarvis.help("<category>")`. Workers also get `read_file`/`write_file`/`list_files` and the artifact reads (`edge/src/agent/files.rs`, `artifacts.rs`).

## Adding to the SDK
- Define the function in `sdk.py` and register it in `_CATEGORIES`. The docstring is the only documentation `help()` shows.
- **Reads** use the `mode=ro` sqlite connection. **Writes** — and anything the server holds, like embedding a query (`searchMemory`) — go through `api()` → the server's GraphQL (so scheduler reloads, board dispatch, approval gating and validation happen). Mutations take Relay GlobalIDs — use `_global_id()`. Endpoint is `$JARVIS_API_URL` (default `http://127.0.0.1:8000/graphql`; the server sets it for its kernels).
- The SDK imports only the standard library, `httpx` and `tools.text_dedupe` (`numpy` is the agent's, not the SDK's). It finds the database as the server does (`DATABASE_URL`, else `$WORK_DIR/database.db`), and the tool policy is the `tools.policy` setting, re-read every 2 s.
- Need a side effect with no mutation? Add the mutation; don't write the DB from the kernel.
- Scope (`_conversation_id`, `_project_id`) is injected per kernel by the server (`edge/src/kernels/`) — the agent can't choose a project. `list_conversations` raises outside a project; all conversation reads drop incognito (`ephemeral`) conversations.

## Web vs browser
- `research.py` is the **web**: `search()` (Tavily/Brave, ddgs fallback) and `read(url)`. `browser.py` is the **browser**: one persistent headed Chromium reached over CDP. research may import browser, never the reverse.
- `read()` has two rungs: httpx + trafilatura, then the real browser (`browser=True` goes straight there). There is deliberately no headless rung, and `read()` must not import Playwright (a test pins this). No `playwright install` needed.
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
