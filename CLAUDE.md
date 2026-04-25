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
│   ├── models.py         # ORM: Conversation, Message, Step, Automation, AutomationRun, Workflow, WorkflowRun, ConfigSetting
│   ├── ops.py            # Async CRUD functions
│   └── engine.py         # DB init, _migrate(), async_session, get_session
├── server/
│   ├── entrypoint.py     # FastAPI app, lifespan, router wiring, Telegram bot lifecycle
│   ├── telegram_bot.py   # Optional Telegram bot (enabled by TELEGRAM_BOT_TOKEN env var)
│   ├── routes_chat.py    # /run, /stop, /resume, /stream/{task_id}, /conversations
│   ├── routes_automations.py  # /automations CRUD + /stream/automation/{run_id}
│   ├── routes_workflows.py    # /workflows CRUD + /stream/workflow/{run_id}
│   ├── routes_live.py    # WebSocket / live endpoints
│   ├── routes_media.py   # TTS, transcription
│   └── routes_memory.py  # Memory endpoints
├── workflow/
│   ├── engine.py         # BFS workflow executor — execute_workflow()
│   └── nodes.py          # AgentNode, ConditionalNode, MapNode, StartNode + _emit()
├── tools/
│   ├── web.py            # web_search, fetch_page, extract_links
│   ├── code.py           # run_python
│   ├── files.py          # read_file, write_file, list_files
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
`token`, `thinking_token`, `step`, `interrupt`, `interrupt_resolved`, `done`, `error`

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
- SQLite at `./database.db` (configurable via `DATABASE_URL` env var)
- Async via `aiosqlite` + SQLAlchemy async
- `async_session` uses `expire_on_commit=False` — no `session.refresh()` needed after commit
- `update_*` functions must use ORM-level `setattr` (not raw SQL UPDATE) so `onupdate` callbacks fire
- All ForeignKey columns carry `index=True`; new ones must too

## Default Model
Compile-time default is `google_genai:gemma-4-31b-it` (requires `GOOGLE_API_KEY`). Can be overridden at runtime via `uv run python main.py model set-default <id>` — stored in the `config_settings` DB table under key `default.model`. `get_default_model(session)` in `db/ops.py` returns the DB value or falls back to the catalog default. Ollama and AWS Bedrock models also available — see `core/model_catalog.py`.

## Telegram Bot
Optional — enabled by setting the `TELEGRAM_BOT_TOKEN` environment variable before starting the server. Implemented in `server/telegram_bot.py`:
- Uses `python-telegram-bot` v22 (async, long-polling)
- Allowlist: `uv run python main.py config set telegram.allowed_users "123456789,987654321"` — **rejects all users by default when empty**
- Each Telegram chat gets its own LangGraph thread (`telegram_{chat_id}`) for persistent memory
- Streams the response by editing a placeholder message every ~1 second
- Bot token env var: `TELEGRAM_BOT_TOKEN`; user IDs can be obtained from @userinfobot on Telegram

## Environment
- Python 3.13, managed with `uv`
- No test suite currently
- No linter/formatter configured; use `uvx pyrefly check --summarize-errors` for type checking
- Frontend: no UI library — pure CSS with dark theme CSS variables in `styles.css`
