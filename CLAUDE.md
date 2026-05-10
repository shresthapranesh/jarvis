# Research Assistant — Claude Setup

## Project Overview
Multi-agent AI research platform. Users submit queries via web UI or CLI; specialized AI agents collaborate in real-time to produce research, analysis, and reports. Results stream live via SSE. Also supports automations (scheduled/manual) and visual workflow graphs.

## Architecture

```
jarvis/
├── main.py               # CLI entrypoint (typer): run, start, config *, model *
├── core/
│   ├── agents.py         # build_agent(model) — LangGraph agent factory + subagents
│   ├── model_catalog.py  # AVAILABLE_MODELS, DEFAULT_MODEL, is_valid_model()
│   ├── state.py          # TaskState, _tasks, _notify(), stream_task_events()
│   ├── streaming.py      # TokenCoalescer, _process_chunk(), _finalize_message()
│   ├── schemas.py        # Pydantic request/response models
│   ├── scheduler.py      # APScheduler setup, _register_scheduler_job()
│   ├── config.py         # App config (DATABASE_URL, etc.)
│   ├── document_extractor.py  # PDF/DOCX/XLSX text extraction
│   └── logging_middleware.py
├── db/
│   ├── models.py         # ORM: Conversation, Message, Step, Artifact, Document, Automation, AutomationRun, Workflow, WorkflowRun, ConfigSetting
│   ├── ops.py            # Async CRUD functions
│   └── engine.py         # DB init, _migrate(), async_session, get_session
├── server/
│   ├── entrypoint.py     # FastAPI app, lifespan, router wiring, Telegram bot lifecycle
│   ├── telegram_bot.py   # Optional Telegram bot (enabled by TELEGRAM_BOT_TOKEN env var)
│   ├── routes_chat.py    # /run, /stop, /resume, /stream/{task_id}, /conversations, /conversations/{id}/todos
│   ├── routes_automations.py  # /automations CRUD + /stream/automation/{run_id}
│   ├── routes_workflows.py    # /workflows CRUD + /stream/workflow/{run_id}
│   ├── routes_artifacts.py    # /artifacts CRUD + raw download
│   ├── routes_documents.py    # /conversations/{id}/documents, /documents/{id}, /documents/{id}/raw
│   ├── routes_live.py    # WebSocket / live endpoints
│   ├── routes_media.py   # TTS, transcription, /models
│   ├── routes_memory.py  # Memory endpoints
│   └── routes_tasks.py   # Running-tasks list endpoints
├── workflow/
│   ├── engine.py         # BFS workflow executor — execute_workflow()
│   └── nodes.py          # AgentNode, ConditionalNode, MapNode, StartNode + _emit()
├── tools/
│   ├── execute.py        # safe_execute (Python in subprocess)
│   ├── files.py          # read_file, write_file, list_files
│   ├── artifacts.py      # write_artifact, read_artifact, list_artifacts
│   ├── todos.py          # write_todos, set_todo_status (per-conversation plan)
│   ├── workers.py        # spawn_workers (parallel role-templated subagents)
│   ├── automations.py    # CRUD as agent tools
│   ├── workflows.py      # CRUD as agent tools
│   ├── browser_agent.py  # Headless browser sub-agent
│   ├── web.py            # web_search, fetch_page, extract_links
│   ├── code.py           # run_python
│   ├── finance.py        # get_stock_data, get_historical_prices, compare_stocks, …
│   └── datetime.py       # get_current_datetime
└── frontend/             # React 19 + TanStack Router/Query + Vite + TypeScript
    └── src/
        ├── routes/       # File-based routes: index.tsx, c.$id.tsx, automation.tsx, workflow/
        ├── components/   # InputBox, MessageBubble, ConversationList, ActivitySidebar, …
        ├── hooks/        # useStream.ts, useAutomationStream.ts, useWorkflowStream.ts
        └── lib/          # api.ts, types.ts (parseDefinition, serializeDefinition)
```

## Development Commands

**Backend:**
```bash
uv run uvicorn server.entrypoint:app --reload   # start API server on :8000
uv run python main.py run "<query>"             # CLI one-shot query
uv add <package>                                # add dependency (updates pyproject.toml + uv.lock)
```

**Config CLI:**
```bash
uv run python main.py config set <key> <value>   # e.g. config set telegram.allowed_users "123456789"
uv run python main.py config get <key>
uv run python main.py config list
uv run python main.py config delete <key>
```

**Model CLI:**
```bash
uv run python main.py model list                         # show all models with labels; marks current default
uv run python main.py model set-default <model-id>      # persist default to DB
```

**Frontend** (always use `pnpm`, not npm):
```bash
cd frontend
pnpm dev       # dev server on :5173 (proxies API to :8000)
pnpm build     # build to ../static/dist/ (served by FastAPI in prod)
```

## Key Patterns

### Adding a new API endpoint
1. Add async CRUD function(s) to `db/ops.py` if DB access needed
2. Add endpoint to the relevant router in `server/routes_*.py` — return `JSONResponse`, use `Annotated[AsyncSession, Depends(get_session)]`
3. Add proxy entry in `frontend/vite.config.ts` under `server.proxy`
4. Add fetch function to `frontend/src/lib/api.ts`
5. Add TypeScript types to `frontend/src/lib/types.ts`

### Adding a new DB model
- Define in `db/models.py` using `DeclarativeBase`, `Mapped`, `mapped_column`
- Always add `index=True` to ForeignKey columns used in WHERE/JOIN
- `init_db()` calls `Base.metadata.create_all` — new tables auto-created on server start
- For schema changes on existing tables, add `CREATE INDEX IF NOT EXISTS` / `ALTER TABLE` to `_migrate()` in `db/engine.py`
- Use `_now()` for UTC timestamps, `str(uuid4())` for IDs
- **Do not call `session.refresh()` after commit** — `async_session` uses `expire_on_commit=False` and all defaults are Python-side, so the object is already fully populated

### Adding a new model to the catalog
- Add a `ModelSpec` entry to `AVAILABLE_MODELS` in `core/model_catalog.py` — that's the only change needed
- Frontend fetches `GET /models` and populates all dropdowns from the response automatically

### Adding a new frontend route
- Create file in `frontend/src/routes/` using TanStack Router file-based naming
- `routeTree.gen.ts` is auto-generated — never edit it manually, just run `pnpm dev`
- Pattern: `export const Route = createFileRoute('/path')({ component: MyPage })`

### Adding a new agent tool
- Add function to appropriate file in `tools/`
- Import and add to the relevant subagent's tool list in `core/agents.py`

### Adding a new conversation-scoped resource
Three exist today: artifacts (DB row + `.md` file under `artifacts_dir`), documents (DB row + raw bytes under `documents_dir`), todos (in the LangGraph checkpointer, not the SQL DB). To add a fourth that follows the same pattern:
- Add the model to `db/models.py` with `conversation_id` FK + an `index=True`, and a relationship from `Conversation` with `cascade="all, delete-orphan"` so DB cascade fires.
- For on-disk bytes: add a new `*_dir: Path` field to `AppConfig` in `core/config.py` (mirror `documents_dir`), and write to `{dir}/{id}{ext}` from the route handler that creates the row.
- Extend `delete_conversation` in `db/ops.py` to collect file paths *before* the cascade and `Path(p).unlink(missing_ok=True)` them after the commit. The function already calls `adelete_thread` on the async checkpointer for langgraph state cleanup.
- Conversation deletion intentionally cascades messages → steps → artifacts → documents via SQLAlchemy ORM cascade. SQLite FK enforcement (`PRAGMA foreign_keys=ON`) is **not** set; cascades work because SQLAlchemy emits explicit DELETEs on its own.

### Adding a new workflow node type
- Add a class extending `BaseNode` with a `node_type` class var and `execute()` method in `workflow/nodes.py`
- Register it in `NODE_REGISTRY` at the bottom of that file
- Use `_emit(task_state, "event_name", **data)` for SSE events — never append to `task_state.events` directly

## SSE Streaming Pattern
The SSE system is central to the app. Chat, automations, and workflows all use the same pattern:
- Trigger endpoint (POST) → registers `_tasks[id] = TaskState()` **before** returning, then `asyncio.create_task(...)` for the background work
- Background task puts events into `TaskState.events` and calls `_notify(state)` to wake waiting clients
- Stream endpoint (GET) → calls `stream_task_events(state)` from `core/state.py`, which yields events via a cursor+waiter loop
- **Critical:** `_tasks[id]` must be set synchronously before returning from the trigger — prevents SSE client race condition
- **Do not use `_tasks.setdefault(run_id, TaskState())`** in background executors — the caller always pre-sets it; use `state = _tasks[run_id]` directly

### Chat SSE events
`token`, `thinking_token`, `step`, `artifact`, `todos_updated`, `worker_done`, `browser_step`, `interrupt`, `interrupt_resolved`, `safety_input_blocked`, `safety_output_blocked`, `done`, `stopped`, `error`

Custom events (anything except `token`/`thinking_token`/`step`) are dispatched from tools via `adispatch_custom_event(name, {"type": name, ...})` (the `"type"` key is required — `core/streaming.py:_process_chunk` switches on it). Token/thinking/step events flow naturally from LangGraph stream modes.

### Automation SSE events
`token`, `thinking_token`, `step`, `done`, `error`

### Workflow SSE events
`node_start`, `node_token`, `node_condition`, `node_done`, `node_error`, `map_start`, `map_item_done`, `workflow_done`, `workflow_error`

## Automation Feature
Three input types:
- **prompt** — runs through `build_agent()`, streams tokens live via `TokenCoalescer`
- **code** — `asyncio.create_subprocess_exec` runs Python in subprocess, streams stdout
- **webhook** — `httpx.AsyncClient.request` fires HTTP call (for n8n, Zapier, etc.)

Scheduler: APScheduler (`core/scheduler.py`). Cron jobs fire in a thread; use `_validate_cron(schedule)` helper in `routes_automations.py` to validate expressions before saving.

## Workflow Feature
Visual graph executor (`workflow/engine.py`):
- Definitions stored as JSON (`Workflow.definition`) with `nodes` + `edges` lists
- `execute_workflow(run_id, definition, inputs, task_state)` runs BFS over the graph
- Node types: `agent` (full LangGraph loop), `conditional` (LLM yes/no router), `map` (parallel sub-workflow per list item), `start` (entry point with defaults)
- Conditional nodes prune inactive branches via `pruned_edges`; pruned nodes never execute

## Database
- SQLite at `~/.jarvis/database.db` (configurable via `DATABASE_URL` env var)
- Async via `aiosqlite` + SQLAlchemy async
- `async_session` uses `expire_on_commit=False` — no `session.refresh()` needed after commit
- `update_*` functions must use ORM-level `setattr` (not raw SQL UPDATE) so `onupdate` callbacks fire
- All ForeignKey columns carry `index=True`; new ones must too
- Two SQLite files live under `~/.jarvis/`: `database.db` (app state — conversations, messages, artifacts, documents, etc.) and `checkpoints.db` (LangGraph thread state, keyed by `thread_id == conversation_id`). Conversation deletion cascades both: ORM deletes app-DB rows, then `delete_conversation` calls `adelete_thread(conv_id)` on the async checkpointer.

## Default Model
Compile-time default is `google_genai:gemma-4-31b-it` (requires `GOOGLE_API_KEY`). Can be overridden at runtime via `uv run python main.py model set-default <id>` — stored in the `config_settings` DB table under key `default.model`. `get_default_model(session)` in `db/ops.py` returns the DB value or falls back to the catalog default. Ollama and AWS Bedrock models also available — see `core/model_catalog.py`.

`Conversation.model` is **sticky per-conversation**: `/run` updates it whenever `request.model` differs from the stored value, and the InputBox calls `PATCH /conversations/{id}` on dropdown change so a model picked mid-conversation persists across reloads. The frontend seeds the dropdown from `conversation.model` (returned by `GET /conversations/{id}`), falling back to `catalog.default` only when no conversation exists yet.

## Telegram Bot
Optional — enabled by setting the `TELEGRAM_BOT_TOKEN` environment variable before starting the server. Implemented in `server/telegram_bot.py`:
- Uses `python-telegram-bot` v22 (async, long-polling)
- Allowlist: `uv run python main.py config set telegram.allowed_users "123456789,987654321"` — **rejects all users by default when empty**
- Each Telegram chat gets its own LangGraph thread (`telegram_{chat_id}`) for persistent memory
- Streams the response by editing a placeholder message every ~1 second
- Bot token env var: `TELEGRAM_BOT_TOKEN`; user IDs can be obtained from @userinfobot on Telegram

## Discord Bot
Optional — enabled by setting the `DISCORD_BOT_TOKEN` environment variable before starting the server. Implemented in `server/discord_bot.py`:
- Uses `discord.py` v2 (async); the bot's **Message Content Intent** must be enabled in the Discord developer portal
- Allowlist: `uv run python main.py config set discord.allowed_users "123456789012345678,234567890123456789"` — **rejects all users by default when empty**
- Trigger rule: replies in DMs always; in guild channels only when @mentioned or when the message is a reply to the bot
- Each Discord channel (DM or guild) gets its own LangGraph thread (`discord_{channel_id}`) for persistent memory
- Streams the response by editing a single message every ~1 second; Discord's 2000-char hard limit is enforced via `_MAX_MSG_LEN = 1900`
- Voice messages and audio attachments are transcribed via `transcribe_bytes`; image attachments flow through the same vision path as the web UI
- Bot token env var: `DISCORD_BOT_TOKEN`; user IDs can be obtained from Discord by enabling Developer Mode → right-click user → Copy User ID

## Environment
- Python 3.13, managed with `uv`
- No test suite currently
- No linter/formatter configured; use `uvx pyrefly check --summarize-errors` for type checking
- Frontend: no UI library — pure CSS with dark theme CSS variables in `styles.css`
