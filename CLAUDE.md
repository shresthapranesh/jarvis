# Research Assistant — Claude Setup

## Project Overview
Multi-agent AI research platform. Users submit queries via web UI or CLI; specialized AI agents collaborate in real-time to produce research, analysis, and reports. Results stream live over GraphQL subscriptions (WebSocket). Also supports automations (scheduled/manual), visual workflow graphs, persistent free-text agent memory, document/image uploads, and notification channels.

## Architecture

The API is **GraphQL-first** (Strawberry + FastAPI). Queries/mutations go over HTTP POST `/graphql`; live event streams go over `graphql-ws` WebSocket subscriptions on the same path. A handful of REST endpoints remain only for things that don't fit GraphQL (raw binary download, file upload, audio TTS/transcription, the live-audio WebSocket, log tailing, health). The frontend uses **Relay** against that schema.

Long-running work (chat / automation / workflow runs) is dispatched through a **durable SQLite-backed job queue** rather than bare `asyncio.create_task`: a mutation enqueues a `Job` row and pre-registers an in-memory `TaskState`; a background `Worker` claims the job and runs its handler. This survives restarts and gives a single cancellation path (`job.id == task_id`).

```
jarvis/
├── main.py               # CLI entrypoint (typer): run, start, config *, model *
├── core/
│   ├── agents.py         # build_agent(model) — LangGraph agent factory + subagents
│   ├── messages.py       # LLM-message hygiene: strip_historical_thinking,
│   │                     #   repair_orphan_tool_calls, build_llm_messages (+ estimate_tokens)
│   ├── model_catalog.py  # AVAILABLE_MODELS, DEFAULT_MODEL, is_valid_model()
│   ├── state.py          # TaskState, _tasks, _notify(), stream_task_events(),
│   │                     #   get_queue(), get_store(), get_async_checkpointer()
│   ├── queue/            # Durable job queue: protocol.py (Job, JobQueue ABC),
│   │                     #   sqlite.py (SqliteJobQueue), worker.py (Worker)
│   ├── streaming.py      # TokenCoalescer, STREAM_MODES, _process_chunk(), _finalize_message()
│   ├── summarization.py  # maybe_summarize() — history trimming for the agent loop
│   ├── safety.py         # gate_input() / gate_output() content gates
│   ├── memory_consolidation.py  # AGENTS.md blob store keys + consolidate_memory() (keyless fallback)
│   ├── memory_store.py   # discrete vector memory — load_core/search_memory/upsert_memory (Memory rows)
│   ├── skill_store.py    # skill description embedding + intent retrieval — skill_catalog()/search_skills()
│   ├── notifications.py  # notification-channel delivery
│   ├── schemas.py        # Pydantic + Strawberry input models (AttachmentIn, etc.)
│   ├── scheduler.py      # APScheduler setup, cron registration
│   ├── config.py         # AppConfig (DATABASE_URL, work_dir, artifacts_dir, documents_dir, staging_dir)
│   ├── document_extractor.py  # PDF/DOCX/XLSX text extraction
│   ├── doc_index.py      # chunk-index large attachments + cosine search (Gemini embeddings)
│   ├── kernels.py        # per-session IPython kernels (stateful run_cell) + idle reaper
│   └── log_setup.py / log_callback.py  # logging + AgentLogger
├── db/
│   ├── models.py         # ORM: Conversation, Message, Step, Automation, AutomationRun,
│   │                     #   ConfigSetting, NotificationChannel, Artifact, Document,
│   │                     #   Workflow, WorkflowRun, Job
│   ├── ops.py            # Async CRUD functions
│   └── engine.py         # DB init, _migrate(), async_session, get_session
├── server/
│   ├── entrypoint.py     # FastAPI app, lifespan (queue worker + bots), router wiring, SPA fallback
│   ├── graphql/          # ── Strawberry GraphQL layer (the primary API) ──
│   │   ├── schema.py     #   merge_types() composes Query / Mutation / Subscription from mixins
│   │   ├── router.py     #   GraphQLRouter — graphql-transport-ws + graphql-ws subscription protocols
│   │   ├── context.py    #   get_context() — injects the AsyncSession per request
│   │   ├── export_schema.py  # dumps schema.graphql for the frontend Relay compiler
│   │   ├── types/        #   GraphQL types (Relay Nodes): artifact, document, upload, conversation,
│   │   │                 #     automation, workflow, memory, notification, task_run, model_catalog,
│   │   │                 #     events, automation_events, workflow_events, todo
│   │   ├── queries/      #   *Query mixins: artifact, automation, conversation, memory, models,
│   │   │                 #     notification, task_run, workflow
│   │   ├── mutations/    #   *Mutation mixins: artifact, automation, conversation (start/stop/resume_task),
│   │   │                 #     memory, notification, task_run (stop_running_task), workflow
│   │   └── subscriptions/#   chat (taskEvents), automation (automationRunEvents),
│   │                     #     board_task (boardTaskEvents), workflow (workflowRunEvents)
│   │                     #     — all wrap stream_task_events
│   ├── chat_runtime.py        # register_chat_task / enqueue_chat_task + chat_job_handler + _run_agent_task
│   ├── automation_runtime.py  # register_automation_run + automation_job_handler (prompt/code/webhook)
│   ├── workflow_runtime.py    # register_workflow_run + workflow_job_handler
│   ├── task_board_runtime.py  # dispatch_board_tasks (kanban dispatcher) + board_task_job_handler
│   ├── telegram_bot.py   # Optional Telegram bot (enabled by TELEGRAM_BOT_TOKEN env var)
│   ├── discord_bot.py    # Optional Discord bot (enabled by DISCORD_BOT_TOKEN env var)
│   ├── routes_artifacts.py    # REST: GET /artifacts/{id}/raw (binary .md download)
│   ├── routes_documents.py    # REST: GET /documents/{id}/raw
│   ├── routes_uploads.py      # REST: POST /uploads → staged file, returns opaque uploadId
│   ├── routes_media.py        # REST: GET /health, POST /tts, POST /transcribe
│   ├── routes_live.py         # WS: /ws/live (live audio session)
│   └── routes_logs.py         # REST: GET /server-logs, GET /server-logs/stream
├── workflow/
│   ├── engine.py         # BFS workflow executor — execute_workflow()
│   └── nodes.py          # AgentNode, ConditionalNode, MapNode, StartNode + _emit()
├── tools/                # The main agent is CODE-FIRST: it binds only the [bound] tools
│   │                     #   below; web/finance/datetime/browser work is done by writing
│   │                     #   Python in run_cell, NOT via dedicated tools. [unbound] files
│   │                     #   exist but are not wired into the agent (see core/agents.py main_tools).
│   ├── code.py           # [bound] run_cell — stateful notebook session (per-conversation IPython kernel, core/kernels.py)
│   ├── files.py          # [bound] read_file, write_file, list_files
│   ├── artifacts.py      # [bound] write_artifact, read_artifact, list_artifacts
│   ├── todos.py          # [bound] write_todos, set_todo_status (per-conversation plan)
│   ├── documents.py      # [bound] search_documents, read_document (retrieval over indexed attachments)
│   ├── workers.py        # [bound] spawn_workers (parallel role-templated subagents)
│   ├── automations.py    # [bound] CRUD as agent tools
│   ├── board.py          # [bound] create_task/list_tasks (task board) + complete_task/block_task (in-run only)
│   ├── workflows.py      # [bound] CRUD as agent tools
│   ├── skills.py         # [bound] use_skill + list/create/update/delete_skill (agent-authored skills)
│   ├── memory.py         # [bound iff embedder] remember, search_memory (discrete vector memory)
│   ├── context.py        # current_ctx() — per-call ToolContext (code_session_key, conversation_id)
│   ├── research.py       # [kernel-preloaded] search() (Tavily/Brave, ddgs fallback) + read()
│   │                     #   (trafilatura extraction, Playwright fallback) — plain sync helpers
│   │                     #   injected into every run_cell kernel (core/kernels.py), not bound tools
│   ├── web.py            # [unbound] web_search (ddgs), fetch_page, extract_links, playwright_browse
│   ├── finance.py        # [unbound] get_stock_data, get_historical_prices, compare_stocks, …
│   ├── browser_agent.py  # [unbound] headless browser sub-agent
│   └── datetime.py       # [unbound] get_current_datetime
└── frontend/             # React 19 + TanStack Router + Relay + Vite + TypeScript
    ├── relay.config.json # Relay compiler config (schema → ./schema.graphql, artifacts → src/__generated__)
    ├── schema.graphql    # SDL exported from the Python schema (regenerate via `pnpm schema`)
    └── src/
        ├── routes/       # File-based routes: index, c.$id, automation, board, workflow/*, artifacts,
        │                 #   memory, live, logs, tasks, settings
        ├── components/   # InputBox, MessageThread, MessageBubble, ConversationList, ActivitySidebar,
        │                 #   ArtifactPanel, ArtifactsBrowser, MemoryView, NotificationsEditor,
        │                 #   WorkflowEditor*, AutomationForm, AutomationRunsPanel, InterruptPrompt, …
        ├── hooks/        # useTaskEvents, useAutomationRunEvents, useWorkflowRunEvents (live streams),
        │                 #   useModels, useLiveSocket, useAudioTTS, useWhisperSTT, useSpeechRecognition, useLogStream
        ├── relay/        # Per-operation query/mutation modules + environment.ts, globalId.ts
        ├── __generated__/# Relay-compiler output (never edit by hand)
        └── lib/          # api.ts (REST helpers), types.ts, toast.tsx, steps.ts, uploads.ts
```

## Development Commands

**Backend:**
```bash
uv run uvicorn server.entrypoint:app --reload   # start API server on :8000 (GraphQL at /graphql)
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
pnpm dev        # dev server on :5173 — runs vite AND relay-compiler --watch concurrently
pnpm relay      # run the Relay compiler once (regenerates src/__generated__)
pnpm schema     # regenerate schema.graphql from the Python schema (runs server.graphql.export_schema)
pnpm typecheck  # pnpm relay && tsc -b
pnpm build      # pnpm relay && vite build → ../static/dist/ (served by FastAPI in prod)
```

> **Relay gotcha:** `tsc` and `vite build` both depend on the generated `src/__generated__/*` artifacts, so the Relay compiler must run first — that's why `build`/`typecheck` prepend `pnpm relay`. After changing any `graphql\`...\`` literal, run `pnpm relay`. After changing the **Python schema**, run `pnpm schema` first (to refresh `schema.graphql`) then `pnpm relay`. Or rely on `pnpm dev`'s watcher (note: it watches literals, not the Python schema — rerun `pnpm schema` manually when the backend schema changes).

## Key Patterns

### Adding a new GraphQL field (query / mutation / subscription)
1. Add async CRUD function(s) to `db/ops.py` if DB access is needed.
2. Add/extend a Strawberry type in `server/graphql/types/`. DB-backed types are Relay Nodes: `id: relay.NodeID[str]`, a `from_db` classmethod, and `resolve_node` for `Node` lookups. Resolve expensive fields lazily (e.g. `Artifact.content` reads the `.md` file on demand) so list queries stay cheap.
3. Add the resolver to the relevant `*Query` / `*Mutation` / `*Subscription` mixin under `queries/`·`mutations/`·`subscriptions/`. **Mixins are composed via `merge_types(...)` in `schema.py`** — a brand-new mixin class must be added to that tuple, or its fields won't appear.
4. Resolvers read the DB session via `info.context["session"]` (see `context.py`).
5. `cd frontend && pnpm schema && pnpm relay` to regenerate `schema.graphql` + Relay artifacts.
6. Add a per-operation module under `frontend/src/relay/` (a `graphql\`...\`` literal + a refetch/commit helper) and consume it with `useLazyLoadQuery` / a commit function. Use `encodeGlobalId`/`decodeGlobalId` (`relay/globalId.ts`) to convert between Relay global IDs and raw DB IDs.

### Adding a new REST endpoint (only when GraphQL doesn't fit)
REST is reserved for binary download, file upload, audio, the live WS, log tailing, and health. To add one:
1. Add the endpoint to the relevant `server/routes_*.py` — use `Annotated[AsyncSession, Depends(get_session)]`, return `Response`/`PlainTextResponse`/`JSONResponse`.
2. Wire the router in `server/entrypoint.py` (`app.include_router(...)`).
3. Add a proxy entry in `frontend/vite.config.ts` under `server.proxy` (set `ws: true` for WebSocket paths). Existing proxied prefixes: `/graphql`, `/uploads`, `/health`, `/ws/live`, `/tts`, `/transcribe`, `/artifacts`, `/server-logs`.
4. Add a fetch helper to `frontend/src/lib/api.ts` and types to `frontend/src/lib/types.ts`.

### Adding a new DB model
- Define in `db/models.py` using `DeclarativeBase`, `Mapped`, `mapped_column`.
- Always add `index=True` to ForeignKey columns used in WHERE/JOIN.
- `init_db()` calls `Base.metadata.create_all` — new tables auto-created on server start.
- For schema changes on existing tables, add `CREATE INDEX IF NOT EXISTS` / `ALTER TABLE` to `_migrate()` in `db/engine.py`.
- Use `_now()` for UTC timestamps, `str(uuid4())` for IDs.
- **Do not call `session.refresh()` after commit** — `async_session` uses `expire_on_commit=False` and all defaults are Python-side, so the object is already fully populated.

### Adding a new model to the catalog
The catalog merges two layers in `core/model_catalog.py`: `BUILTIN_MODELS` (compiled-in seed) + a runtime cache of custom models. `build_llm()` switches only on `provider` (`KNOWN_PROVIDERS`: ollama, google_genai, bedrock, anthropic, meta) — the id is `provider:model_name`, so any model from those backends needs **no code**, only a catalog entry.
- **At runtime (no code change, preferred):** `uv run python main.py model add <provider:model_name> "<label>"` — persisted as JSON under the `config_settings` key `models.custom`. `model remove <id>` / `model list` manage them. Hydrated into the in-memory cache at server startup (lifespan), on every GraphQL `models` query, and per-invocation in the CLI (`_run_db`).
- **As a built-in default:** add a `ModelSpec` entry to `BUILTIN_MODELS`. Note `BUILTIN_MODELS[0]` is the compile-time `DEFAULT_MODEL`, so don't prepend unless you mean to change the default.
- All consumers go through `available_models()` / `get_model_spec()` / `is_valid_model()` (built-in ∪ custom, deduped). The frontend reads the catalog via the GraphQL `models` query (`queries/models.py`, async — re-hydrates from DB so runtime additions show without a restart) — dropdowns populate automatically.

### Adding a new frontend route
- Create a file in `frontend/src/routes/` using TanStack Router file-based naming.
- `routeTree.gen.ts` is auto-generated — never edit it manually, just run `pnpm dev`.
- Pattern: `export const Route = createFileRoute('/path')({ component: MyPage })`.

### Adding a new agent tool
- Add a function to the appropriate file in `tools/`.
- Import and add it to the relevant subagent's tool list in `core/agents.py`.

### Adding a new conversation-scoped resource
DB-backed conversation resources today: artifacts (DB row + `.md` file under `artifacts_dir`), documents (DB row + raw bytes under `documents_dir`). Todos live in the LangGraph checkpointer, not the SQL DB. (Uploads are *staging-only* — see "Uploads & Documents" below — not a conversation-scoped table.) To add a DB-backed one:
- Add the model to `db/models.py` with `conversation_id` FK + `index=True`, and a relationship from `Conversation` with `cascade="all, delete-orphan"`.
- For on-disk bytes: add a new `*_dir: Path` field to `AppConfig` in `core/config.py` (mirror `documents_dir`), and write to `{dir}/{id}{ext}` from whatever creates the row.
- Extend `delete_conversation` in `db/ops.py` to collect file paths *before* the cascade and `Path(p).unlink(missing_ok=True)` them after the commit. The function already calls `adelete_thread` on the async checkpointer for langgraph state cleanup.
- Conversation deletion intentionally cascades messages → steps → artifacts → documents via SQLAlchemy ORM cascade. SQLite FK enforcement (`PRAGMA foreign_keys=ON`) is **not** set; cascades work because SQLAlchemy emits explicit DELETEs on its own.

### Adding a new workflow node type
- Add a class extending `BaseNode` with a `node_type` class var and `execute()` method in `workflow/nodes.py`.
- Register it in `NODE_REGISTRY` at the bottom of that file.
- Use `_emit(task_state, "event_name", **data)` for stream events — never append to `task_state.events` directly.

## Live Streaming + Job Queue Pattern
Live streaming is central to the app, and all three run kinds (chat, automation, workflow) share one pattern built on the durable job queue (`core/queue/`) plus the in-memory `TaskState` registry (`core/state.py`). This replaced the old SSE `/stream/{id}` endpoints.

Lifecycle:
1. **Mutation triggers the run.** Chat: `startTask`/`resumeTask`/`stopTask` in `mutations/conversation.py`. Automations/workflows have their own run mutations. The mutation calls a `register_*` function in `server/*_runtime.py`.
2. **`register_*` enqueues + pre-registers, atomically.** It inserts the work row (chat: an assistant `Message` placeholder whose id becomes the `task_id`), enqueues a `Job` via `get_queue().enqueue(...)`, and **sets `_tasks[task_id] = TaskState(...)` synchronously before `await session.commit()`** — so a subscriber that gets `task_id` back can't race the worker. Returns `task_id`.
3. **A `Worker` (`core/queue/worker.py`, started in the lifespan) claims the job** and invokes its handler — `chat_job_handler` → `_run_agent_task`, `automation_job_handler` → `_run_automation_inner`, `workflow_job_handler` → `_run_workflow_inner` (→ `execute_workflow`). The handler reuses the pre-registered `TaskState` (`state = _tasks[task_id]`); on a post-restart resume where the trigger is gone, it re-creates one.
4. **The handler appends to `TaskState.events` and calls `_notify(state)`** to wake waiters.
5. **The client opens a subscription** — `taskEvents(taskId)` (chat), `automationRunEvents`, or `workflowRunEvents` — whose resolver yields via `stream_task_events(state)` (a cursor+waiter loop). If the task isn't in `_tasks`, the resolver falls back to the DB to emit a final `done`/`error` and closes. Frontend hooks: `useTaskEvents.ts`, `useAutomationRunEvents.ts`, `useWorkflowRunEvents.ts`.
6. **Cancellation is unified.** `stopRunningTask` (`mutations/task_run.py`; chat also has a per-kind `stopTask`) flips in-process `TaskState` flags (`cancelled`, `_stop_event`, cancels `resume_future`) for an immediate stop **and** calls `get_queue().cancel(task_id)` for the durable/cross-process path. `job.id == task_id` for all three kinds.

Conventions:
- **Do not `_tasks.setdefault(...)`** in handlers — the trigger pre-sets it; use `state = _tasks[task_id]` (handlers only re-create it on the restart-resume path).
- `running_tasks` query (`queries/task_run.py`) lists everything currently in `_tasks` for the Tasks page; finished tasks linger ~5s before being popped so the UI can show their terminal state.

### Chat events
`token`, `thinking_token`, `step`, `artifact`, `todos_updated`, `worker_done`, `browser_step`, `interrupt`, `interrupt_resolved`, `safety_input_blocked`, `safety_output_blocked`, `done`, `stopped`, `error`

Custom events (anything except `token`/`thinking_token`/`step`) are dispatched from tools via `adispatch_custom_event(name, {"type": name, ...})` (the `"type"` key is required — `core/streaming.py:_process_chunk` switches on it). Token/thinking/step events flow naturally from LangGraph stream modes (`STREAM_MODES`).

### Automation events
`token`, `thinking_token`, `step`, `done`, `error`

### Workflow events
`node_start`, `node_token`, `node_condition`, `node_done`, `node_error`, `map_start`, `map_item_done`, `workflow_done`, `workflow_error`

## Safety Gates
`core/safety.py` wraps each chat run: `gate_input(query, model)` judges the user's prompt before the agent spins up (a `step` event is surfaced immediately so the sidebar shows feedback during a cold ~60s Bedrock judge), and `gate_output(message, model)` judges the final answer. A blocked input/output emits `safety_input_blocked`/`safety_output_blocked` and persists the message with status `blocked`.

## Automation Feature
Four input types:
- **prompt** — runs through `build_agent()`, streams tokens live via `TokenCoalescer`
- **code** — `asyncio.create_subprocess_exec` runs Python in a subprocess, streams stdout
- **webhook** — `httpx.AsyncClient.request` fires an HTTP call (for n8n, Zapier, etc.)
- **monitor** — a delta-gated prompt run: always stateful (previous observations live in the shared thread), the prompt is wrapped with compare-against-last-check instructions (`_MONITOR_WRAPPER`), and when the agent's reply starts with the `NO_CHANGE` sentinel the run finishes with status `no_change` and **no notification is sent** — silence means "nothing new". First run reports a baseline; changes produce a concise report that is delivered normally.

Execution lives in `server/automation_runtime.py`; CRUD + listing are GraphQL (`queries/automation.py`, `mutations/automation.py`). Scheduler: APScheduler (`core/scheduler.py`); cron jobs fire in a thread — validate expressions before saving. **Every path that creates/updates/deletes a scheduled automation must call `_register_scheduler_job`/`_remove_scheduler_job`** — the GraphQL mutations and the agent tools (`tools/automations.py`) both do; a missed registration means the schedule silently won't fire until restart.

**Stateful prompt automations** (`Automation.stateful`, opt-in; monitors are always stateful): every run shares the LangGraph thread + Conversation `automation_{automation_id}` (deterministic id — see `automation_conversation_id()` in `db/ops.py`), so the agent remembers previous runs. The runtime lazily creates the Conversation (surface=`automation`) and mirrors each run into Message rows (user prompt + assistant output, statuses matching chat). Overlapping runs of the same stateful automation are **skipped** (run status `skipped`) — the guard checks the Job table for a claimed sibling (`_has_inflight_sibling`), since two runs writing one checkpointer thread would race. `delete_automation` deletes the backing conversation (messages, artifacts, checkpointer thread) via `delete_conversation`. Stateless runs keep the old per-run thread `automation_{run_id}`.

## Task Board Feature (kanban)
A durable multi-agent kanban layered on the job queue. A `BoardTask` is one card: `todo → ready → running → blocked/done → archived`, plus `BoardTaskLink` parent→child dependency edges.
- **Dispatcher** (`server/task_board_runtime.py`): `dispatch_board_tasks()` is the single scheduling entrypoint — an APScheduler interval job (`register_board_dispatch_job`, every 15s) ticks it, and create/ready paths (GraphQL mutations, `create_task` tool, task completion) call it directly so dispatch doesn't wait for the tick. Each pass: promote `todo`→`ready` where all parents are `done` (parentless todos are parked and never auto-promote), then enqueue `ready` tasks up to `MAX_IN_PROGRESS`, ordered by priority desc. Serialized by an asyncio lock.
- **Runs**: each dispatch enqueues a `board_task` job with a **fresh UUID** (`job.id == BoardTask.job_id`) — unlike the other kinds, the job id is NOT the domain row id, because a task can re-run and finished Job rows would collide. The handler composes the prompt (title/body + `complete_task`/`block_task` protocol + completed parents' `summary`/`result_metadata` handoffs + optional skill), runs `build_agent()` on the deterministic conversation `boardtask_{task_id}` (surface=`task`, `board_task_conversation_id()` in db/ops.py), and gates input/output like automations (a gate rejection → `blocked` with a `safety:` reason).
- **Terminal writes respect the agent — and run ownership**: `complete_task(summary, metadata)` / `block_task(reason, needs_input)` (tools/board.py, guarded by `ToolContext.board_task_id`) set the terminal status mid-run; the handler's `_finish_task(task_id, run_id, ...)` only applies its outcome when the row still belongs to this run (`job_id == run_id`), isn't re-queued (`ready`/`todo`), and is still `running` (no explicit tool call → final reply becomes the summary). Errors → `blocked` (`blocked_kind="error"`) + `failure_count` bump; stop → `blocked` (`"stopped"`); gate rejections → `blocked` (`"safety"`). `stop_board_task` also handles the pending-job case (queue cancel finishes the job before any handler runs, so it flips the row itself).
- **Needs-input loop**: `block_task(reason, needs_input=True)` sets `blocked_kind="needs_input"`; the board card shows an answer box → `answerBoardTask(id, answer)` stores `pending_answer` + flips to `ready`. The next dispatch consumes the answer at claim time and runs `_RESUME_PROMPT` (answer only — the thread already holds the question) on the same conversation; a transient failure restores `pending_answer` so a retry still resumes with it. **Race guard**: a tool call flips the row while the agent loop is still wrapping up, so the dispatcher skips `ready` tasks whose previous job is still pending/running — without this, an instant answer/re-run would start a second concurrent run on the same thread.
- **Dependency editing**: `replace_board_task_parents` (db/ops.py) swaps a task's parent links with validation (missing parents, self-link, cycles via descendant walk) and re-parks a waiting task to `todo` when new parents aren't done. Exposed via `updateBoardTask(input.parentIds)`; the create/edit modal renders a parent picker.
- **Auto-decompose**: `decompose_board_task(task_id)` has a planner LLM (task's model, bare `build_llm()` call — no agent loop) split a *standalone waiting* task into 2–`MAX_SUBTASKS` subtasks (JSON; `depends_on` may only reference earlier indexes, so the graph is a DAG by construction). Subtasks become **parents of the original**, which is parked in `todo` FIRST (so a dispatch tick can't start it mid-decompose) and runs last as the synthesis step with every subtask summary as handoff. On unparseable LLM output the task is left untouched (`ValueError`). Exposed as the `decomposeBoardTask` mutation (card split-button + "Auto-split into subtasks" on create) and the `create_task(decompose=True)` agent tool.
- **Startup sweep**: `cleanup_zombie_running_rows` flips `running` board tasks back to `ready` only when no pending/running job holds them — tasks whose job survived restart are left for that job to re-run (flipping those too would double-dispatch).
- **GraphQL + UI**: `boardTasks`/`boardTask` queries, `createBoardTask`/`updateBoardTask`/`setBoardTaskStatus`/`answerBoardTask`/`decomposeBoardTask`/`deleteBoardTask`/`stopBoardTask` mutations, `boardTaskEvents(runId)` subscription (reuses the AutomationEvent union — same wire shape). `frontend/src/components/TaskBoard.tsx` + the `/board` route render the columns (3s polling + `useBoardTaskEvents` live token tail on running cards); cards link to the run transcript at `/c/boardtask_{id}`.

## Workflow Feature
Visual graph executor (`workflow/engine.py`):
- Definitions stored as JSON (`Workflow.definition`) with `nodes` + `edges` lists.
- `execute_workflow(run_id, definition, inputs, task_state)` runs BFS over the graph; `server/workflow_runtime.py` is the background trigger.
- Node types: `agent` (full LangGraph loop), `conditional` (LLM yes/no router), `map` (parallel sub-workflow per list item), `start` (entry point with defaults).
- Conditional nodes prune inactive branches via `pruned_edges`; pruned nodes never execute.
- CRUD + runs are GraphQL (`queries/workflow.py`, `mutations/workflow.py`); the editor lives in `frontend/src/components/WorkflowEditor*.tsx`.

## Memory Feature
Agent memory has **two layers**, selected by whether an embedder is configured (`embeddings_available()`):
- **With an embedder (default): discrete vector memory.** Atomic items live in the `Memory` SQL table (`kind` = `core` | `fact`), embedded on write. `core/memory_store.py` exposes `upsert_memory`, `load_core` (always-on `core` items), and `search_memory` (top-k cosine over `fact` items). The agent reads/writes via the `remember` / `search_memory` tools (`tools/memory.py`, bound only when embeddings are available). Each turn `core/agents.py` (`_memory_volatile_parts`) injects the `core` items + the items retrieved for the current user turn into the system prompt's **volatile suffix** (after the cache breakpoint).
- **Without an embedder (keyless/Ollama): a single free-text blob.** Falls back to one `AGENTS.md` blob in the LangGraph store under the `_MEMORY_NS`/`_MEMORY_KEY` keys (`core/memory_consolidation.py`), accessed via `get_store()`. `consolidate_memory()` collapses/merges it; `_migrate_legacy_key()` upgrades the old key on read.

GraphQL `agentMemory` query + `updateMemory` (blob) / `updateMemoryItem` (discrete) mutations expose both; `frontend/src/components/MemoryView.tsx` + the `/memory` route render and edit them.

## Skills Feature
A **skill** is a named, reusable procedure the agent can author and later reload: a `description` (the routing key, embedded for intent retrieval) plus a `body` (the full instructions, loaded on demand). Stored in the `Skill` SQL table.
- **Agent tools** (`tools/skills.py`, all bound): `use_skill(name)` loads a body to follow; `list_skills` / `create_skill` / `update_skill` / `delete_skill` curate them.
- **Surfacing** (`core/skill_store.py`): each description is embedded; `skill_catalog(query)` returns enabled skills ranked by intent match, which `core/agents.py` (`_skills_volatile_parts`) injects as a `## Available Skills` list — **name + description only** — in the volatile suffix. The body stays out of context until `use_skill` pulls it.
- **GraphQL + UI**: `skills` query + `createSkill`/`updateSkill`/`deleteSkill` mutations (`server/graphql/queries|mutations/skill.py`, type in `types/skill.py`); `frontend/src/components/SkillsView.tsx` + the `/skills` route manage them.

## Notifications
`NotificationChannel` rows define delivery targets. GraphQL `notification` query/mutation + `frontend/src/components/NotificationsEditor.tsx` manage them; delivery logic in `core/notifications.py`.

## Uploads & Documents
- **Uploads** are *staging-only* (no DB table). `POST /uploads` (`routes_uploads.py`) stores the bytes + a `.meta.json` under `staging_dir` and returns an opaque `uploadId`; the client passes that back in `startTask` instead of inlining large files in the GraphQL body. A scheduler job GCs stale staged files. Cap: 100 MiB/file.
- **Documents** are extracted-text sources persisted for a conversation. When a chat is started with attachments, `register_chat_task` (`chat_runtime.py`) decodes/writes the bytes under `documents_dir` and creates a `Document` row. Raw bytes: `GET /documents/{id}/raw`. Text extraction: `core/document_extractor.py` (PDF/DOCX/XLSX). Image attachments flow through the model's vision path instead.
- **Large documents are indexed, not inlined.** Documents whose extracted text exceeds `INLINE_THRESHOLD` (12k chars, `core/doc_index.py`) are chunked + embedded into `DocumentChunk` rows and replaced in the message by a stub carrying the `document_id`; the agent retrieves passages via the `search_documents`/`read_document` tools (`tools/documents.py`, conversation-scoped). Embeddings use a Gemini model (default `models/gemini-embedding-001`, override via `config set embedding.model <id>`; needs `GOOGLE_API_KEY`). When embeddings are unavailable — or the document came from a source without a `Document` row (bots, CLI) — it falls back to inlining (capped at 80k chars), so nothing breaks on keyless/Ollama-only setups. Search is brute-force numpy cosine over the conversation's chunks; chunks cascade-delete with their Document.

## Database
- SQLite at `~/.jarvis/database.db` (configurable via `DATABASE_URL` env var).
- Async via `aiosqlite` + SQLAlchemy async.
- `async_session` uses `expire_on_commit=False` — no `session.refresh()` needed after commit.
- `update_*` functions must use ORM-level `setattr` (not raw SQL UPDATE) so `onupdate` callbacks fire.
- All ForeignKey columns carry `index=True`; new ones must too.
- Tables: `Conversation, Message, Step, Automation, AutomationRun, BoardTask, BoardTaskLink, ConfigSetting, NotificationChannel, Artifact, Document, DocumentChunk, Workflow, WorkflowRun, Job, Memory, Skill` (`Job` = the durable queue's backing table; `DocumentChunk` = embedded passages for large-doc retrieval; `Memory` = discrete vector memory items; `Skill` = reusable agent procedures; `BoardTask`/`BoardTaskLink` = the task board's cards and dependency edges).
- Two SQLite files live under `~/.jarvis/`: `database.db` (app state) and `checkpoints.db` (LangGraph thread state + the store, keyed by `thread_id == conversation_id`). Conversation deletion cascades both: ORM deletes app-DB rows, then `delete_conversation` calls `adelete_thread(conv_id)` on the async checkpointer.

## Default Model
Compile-time default is `google_genai:gemma-4-31b-it` (requires `GOOGLE_API_KEY`). Override at runtime via `uv run python main.py model set-default <id>` — stored in the `config_settings` table under key `default.model`. `get_default_model(session)` in `db/ops.py` returns the DB value or falls back to the catalog default. Ollama and AWS Bedrock models also available — see `core/model_catalog.py`.

`Conversation.surface` records where a conversation lives: `web` | `telegram` | `discord` | `automation` | `task`. The `conversations` GraphQL query filters to `surface: "web"` by default so bot threads, automation histories, and board-task transcripts stay out of the web sidebar (pass another surface, or `null` for all). Bots and the automation/task-board runtimes set it at `get_or_create_conversation` call sites; `_migrate()` backfills pre-existing bot rows by id prefix.

`Conversation.model` is **sticky per-conversation**: the chat `startTask` mutation updates it whenever the request's model differs from the stored value, and the InputBox commits a conversation-update mutation on dropdown change so a model picked mid-conversation persists across reloads. The frontend seeds the dropdown from `conversation.model`, falling back to the catalog default only when no conversation exists yet.

## LLM-call node requirement
Any new agent-loop node that calls an LLM must run `strip_historical_thinking` + `repair_orphan_tool_calls` + `build_llm_messages` (all defined in `core/messages.py`) on the history **before** `.ainvoke` — otherwise Bedrock/Anthropic reject the call (orphaned tool calls / stale thinking blocks).

## Telegram Bot
Optional — enabled by setting `TELEGRAM_BOT_TOKEN` before starting the server. Implemented in `server/telegram_bot.py`:
- Uses `python-telegram-bot` v22 (async, long-polling). Bot lifecycle is wired into the FastAPI lifespan.
- Allowlist: `uv run python main.py config set telegram.allowed_users "123456789,987654321"` — **rejects all users by default when empty**.
- Each Telegram chat gets its own LangGraph thread (`telegram_{chat_id}`) for persistent memory.
- Streams the response by editing a placeholder message every ~1 second. Do **not** send a placeholder before the agent has content — create the message on the first real token.
- User IDs can be obtained from @userinfobot on Telegram.

## Discord Bot
Optional — enabled by setting `DISCORD_BOT_TOKEN` before starting the server. Implemented in `server/discord_bot.py`:
- Uses `discord.py` v2 (async); the bot's **Message Content Intent** must be enabled in the Discord developer portal.
- Allowlist: `uv run python main.py config set discord.allowed_users "123...,234..."` — **rejects all users by default when empty**.
- Trigger rule: replies in DMs always; in guild channels only when @mentioned or when the message is a reply to the bot.
- Each Discord channel (DM or guild) gets its own LangGraph thread (`discord_{channel_id}`) for persistent memory.
- Streams the response by editing a single message every ~1 second; Discord's 2000-char hard limit is enforced via `_MAX_MSG_LEN = 1900`.
- Voice/audio attachments are transcribed via `transcribe_bytes`; image attachments flow through the same vision path as the web UI.
- User IDs: enable Developer Mode → right-click user → Copy User ID.

## Environment
- Python 3.13, managed with `uv`.
- Backend stack: FastAPI + **Strawberry GraphQL** (`strawberry-graphql[fastapi]`, graphql-ws), SQLAlchemy async + `aiosqlite`, LangGraph/LangChain (Anthropic, AWS Bedrock, Google GenAI, Ollama, OpenAI, Meta), `langgraph-checkpoint-sqlite`, APScheduler, `browser-use`/Playwright, faster-whisper/mlx-whisper + piper-tts (audio), yfinance (finance tools).
- Frontend stack: React 19, TanStack Router/Query, **Relay** (`babel-plugin-relay` run as a standalone Vite transform — see `vite.config.ts`), Vite, TypeScript.
- No test suite currently.
- No linter configured; use `uvx pyrefly check --summarize-errors` for Python type checking and `pnpm typecheck` for the frontend. Frontend formatting via `pnpm fmt` (oxfmt).
- Frontend: no UI library — pure CSS with dark-theme CSS variables in `styles.css`.
