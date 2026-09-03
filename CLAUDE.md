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
│   ├── messages.py       # LLM-message hygiene: elide_stale_tool_results, strip_historical_thinking,
│   │                     #   repair_orphan_tool_calls, build_llm_messages (+ estimate_tokens)
│   │                     #   _is_tool_result_carrier covers Anthropic HumanMessage tool_result blocks
│   │                     #   multi-breakpoint via context_cache.CacheSegment
│   ├── compaction.py     # group_messages(), apply_per_call_compaction() (elide + collapse_old_tool_results),
│   │                     #   maybe_compact() -> CompactionResult — incremental sliding-window summarization
│   │                     #   with cached summary; always returns the leaned view (see "Compaction contract")
│   │                     #   (MAF-inspired grouping, ADK-inspired token-budget, pins recent user)
│   ├── context_cache.py  # ADK ContextCacheConfig analog — CacheSegment, ContextCacheConfig,
│   │                     #   build_cached_system_message() multi-breakpoint (max 4)
│   ├── runner.py         # ADK Runner analog — JarvisRunner owns checkpointer/store/queue/http,
│   │                     #   should_use_cache(), get_context_cache_config(), get_budget_limits(), build_agent()
│   ├── approval.py       # ADK LongRunningFunctionTool analog — request_tool_approval() via interrupt,
│   │                     #   is_affirmative_answer(), require_approval decorator
│   ├── budget.py         # MAF TokenUsageTermination analog — BudgetLimits, BudgetTracker, BudgetCallbackHandler,
│   │                     #   get_budget_limits_for_task() — enforces max tokens/calls/duration
│   ├── mcp.py            # ADK McpToolset analog — MultiServerMCPClient wrapper,
│   │                     #   load_mcp_server_configs() (env JARVIS_MCP_SERVERS + ~/.jarvis/mcp.json),
│   │                     #   McpManager.initialize(), get_mcp_tools_sync()
│   ├── model_catalog.py  # AVAILABLE_MODELS, DEFAULT_MODEL, is_valid_model()
│   ├── state.py          # TaskState, _tasks, _notify(), stream_task_events(),
│   │                     #   get_queue(), get_store(), get_async_checkpointer()
│   │                     #   now includes input_tokens/output_tokens/llm_calls/tool_calls/budget_exceeded + _budget_tracker
│   ├── queue/            # Durable job queue: protocol.py (Job, JobQueue ABC),
│   │                     #   sqlite.py (SqliteJobQueue), worker.py (Worker)
│   ├── streaming.py      # TokenCoalescer, STREAM_MODES, _process_chunk(), _finalize_message()
│   │                     #   forwards approval_request/approval_resolved/workflow_event/budget_exceeded
│   ├── summarization.py  # DEPRECATED — use compaction.maybe_compact; kept for backwards compat
│   ├── memory_consolidation.py  # AGENTS.md blob store keys + consolidate_memory() (keyless fallback)
│   ├── memory_store.py   # discrete vector memory — load_core/search_memory/upsert_memory (Memory rows)
│   ├── skill_store.py    # skill description embedding + intent retrieval — skill_catalog()/search_skills()
│   ├── notifications.py  # notification-channel delivery
│   ├── schemas.py        # Pydantic + Strawberry input models (AttachmentIn, etc.)
│   ├── scheduler.py      # APScheduler setup, cron registration
│   ├── config.py         # AppConfig (DATABASE_URL, work_dir, artifacts_dir, documents_dir, staging_dir)
│   ├── settings_admin.py # config_settings as an administrable surface — KNOWN_SETTINGS registry
│   │                     #   + apply_setting() in-process side effects (Settings → Config)
│   ├── voice.py          # Piper TTS voice download, shared by `main.py download-voice`
│   │                     #   and the downloadVoice mutation
│   ├── document_extractor.py  # PDF/DOCX/XLSX text extraction
│   ├── doc_index.py      # chunk-index large attachments + cosine search (Gemini embeddings)
│   ├── kernels.py        # per-session IPython kernels (stateful run_cell) + idle reaper
│   └── log_setup.py / log_callback.py  # logging + AgentLogger
├── db/
│   ├── models.py         # ORM: Conversation, Message, Step, Automation, AutomationRun,
│   │                     #   ConfigSetting, NotificationChannel, Artifact, Document,
│   │                     #   Project, Workflow, WorkflowRun, Job
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
│   │   │                 #     automation, workflow, memory, notification, project, task_run,
│   │   │                 #     model_catalog, events, automation_events, workflow_events, todo
│   │   ├── queries/      #   *Query mixins: artifact, automation, conversation, memory, models,
│   │   │                 #     notification, project, task_run, workflow, setting, maintenance
│   │   ├── mutations/    #   *Mutation mixins: artifact, automation, conversation (start/stop/resume_task),
│   │   │                 #     memory, notification, project (+setConversationProject),
│   │   │                 #     task_run (stop_running_task), workflow, setting, maintenance
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
│   ├── engine.py         # BFS workflow executor — execute_workflow() + retry/timeout/on_error + template ctx
│   └── nodes.py          # AgentNode (output_schema), ConditionalNode, MapNode, StartNode, RouterNode,
│                         #   RefineNode, SequentialNode (output_schema per step), ParallelNode, LoopNode,
│                         #   ApprovalNode, HumanInputNode, PlannerNode + _emit() — ADK Sequential/Parallel/Loop/Approval/Planning analogs
│                         #   + structured output via output_schema + resilience
├── core/
│   ├── workflow_template.py  # Jinja2 expression engine — {{inputs.*}}, {{nodes.*}}, {{workflow.*}} + filters
│   ├── planning.py           # ADK planning analog — should_plan_for_query(), planning directive injection
├── tools/                # The main agent is CODE-FIRST. Only tools coupled to the agent
│   │                     #   GRAPH are [bound]; everything else lives in the kernel-preloaded
│   │                     #   `jarvis` SDK (tools/sdk.py), discovered on demand via
│   │                     #   jarvis.help(). See "Lazy tool loading" below.
│   │                     #   [workers] = bound only to worker roles (_ROLE_TOOLS);
│   │                     #   [unbound] = not wired anywhere.
│   ├── code.py           # [bound] run_cell — stateful notebook session (per-conversation IPython kernel, core/kernels.py)
│   ├── sdk.py            # [kernel-preloaded as `jarvis`] the lazy surface. Reads go direct to a
│   │                     #   mode=ro sqlite connection; WRITES go through the server's own
│   │                     #   GraphQL API (api()) so in-process side effects still fire.
│   │                     #   help()/help(category) is the discovery entrypoint.
│   ├── files.py          # [workers] read_file, write_file, list_files (main agent uses pathlib in run_cell)
│   ├── artifacts.py      # [bound] write_artifact (versioned — its live event needs the run's
│                         #   stream writer); reads are [workers] + jarvis SDK
│   ├── todos.py          # [bound] write_todos, set_todo_status — return Command(update=...) state deltas
│   ├── documents.py      # [workers] search_documents, read_document — main agent uses jarvis SDK
│   ├── workers.py        # [bound] spawn_workers (parallel role-templated subagents)
│   ├── automations.py    # [unbound] manage_automations — superseded by jarvis.create_automation etc.
│   ├── board.py          # [bound, board runs only] complete_task/block_task (current-run
│                         #   lifecycle); create/list via jarvis SDK
│   ├── workflows.py      # [bound] run_workflow (Agent-as-Tool); CRUD via jarvis SDK
│   ├── skills.py         # [unbound] superseded by jarvis.use_skill / create_skill / …
│   ├── projects.py       # [unbound] superseded by jarvis.project_memory
│   ├── memory.py         # [bound iff embedder] remember (no createMemory mutation to route to);
│   │                     #   search via jarvis SDK
│   ├── context.py        # current_ctx() — per-call ToolContext (code_session_key, conversation_id)
│   ├── research.py       # [kernel-preloaded] search() (Tavily/Brave, ddgs fallback) + read()
│   │                     #   (trafilatura extraction, Playwright fallback) — plain sync helpers
│   │                     #   injected into every run_cell kernel (core/kernels.py), not bound tools
│   ├── web.py            # [unbound] web_search (ddgs), fetch_page, extract_links, playwright_browse
│   ├── finance.py        # [unbound] get_stock_data, get_historical_prices, compare_stocks, …
│   └── datetime.py       # [unbound] get_current_datetime
└── frontend/             # React 19 + TanStack Router + Relay + Vite + TypeScript
    ├── relay.config.json # Relay compiler config (schema → ./schema.graphql, artifacts → src/__generated__)
    ├── schema.graphql    # SDL exported from the Python schema (regenerate via `pnpm schema`)
    └── src/
        ├── routes/       # File-based routes: index, c.$id, automation, board, workflow/*, projects/*,
        │                 #   artifacts, memory, live, logs, tasks, settings
        ├── components/   # InputBox, MessageThread, MessageBubble, ConversationList, ActivitySidebar,
        │                 #   ArtifactPanel, ArtifactsBrowser, MemoryView, NotificationsEditor,
        │                 #   ProjectsView, ProjectDetail, WorkflowEditor*, AutomationForm,
        │                 #   AutomationRunsPanel, InterruptPrompt, …
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
   **`get_context` is resolved for the subscription WebSocket too**, so any parameter it takes
   must exist in a websocket scope: use `starlette.requests.HTTPConnection` (the base of both
   `Request` and `WebSocket`), never `Request` — FastAPI leaves a `Request` param unfilled on a
   WS connection, `get_context()` raises `missing 1 required positional argument`, and *every*
   subscription silently fails to connect while queries/mutations keep working (the UI loses all
   live streaming and only shows finished messages after a reload).
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
The catalog merges two layers in `core/model_catalog.py`: `BUILTIN_MODELS` (compiled-in seed) + a runtime cache of custom models. `build_llm()` switches only on `provider` (`KNOWN_PROVIDERS`: ollama, google_genai, bedrock, anthropic, meta, openrouter) — the id is `provider:model_name`, so any model from those backends needs **no code**, only a catalog entry.
- **At runtime (no code change, preferred):** `uv run python main.py model add <provider:model_name> "<label>"` — persisted as JSON under the `config_settings` key `models.custom`. `model remove <id>` / `model list` / `model set-default <id>` manage them. Hydrated into the in-memory cache at server startup (lifespan), on every GraphQL `models` query, and per-invocation in the CLI (`_run_db`).
- **Same thing from the UI:** Settings → Models (`frontend/src/routes/settings.tsx`) does add/update/remove/set-default over the `addModel`/`updateModel`/`removeModel`/`setDefaultModel` mutations (`server/graphql/mutations/models.py`), writing the same `models.custom` key. Every mutation returns the whole `ModelCatalog` (built via `load_model_catalog`, which re-hydrates the cache after the write, so the response already reflects it). Built-ins are flagged `builtin: true` and are set-default-only — never editable or removable. Removing the current default resets it to the compile-time `DEFAULT_MODEL` so `default.model` can't point at a model that no longer exists.
- **As a built-in default:** add a `ModelSpec` entry to `BUILTIN_MODELS`. Note `BUILTIN_MODELS[0]` is the compile-time `DEFAULT_MODEL`, so don't prepend unless you mean to change the default.
- **`ModelSpec.context_window`** (optional) sizes the destructive-summarization trigger per model: `compact_threshold(model)` in `core/compaction.py` returns `40%` of the window, clamped to `[12k, 200k]`, and `_build_agent` resolves it once per compiled agent. **Leave it `None` unless the number is known** — the error is asymmetric: too small only summarizes early, too large disables the trigger until the provider rejects the call. `ollama:*` entries stay `None` on purpose, because `ChatOllama` sends no `num_ctx` and the server's own default (not the model card's window) is what actually truncates. Unknown → flat 80k. `JARVIS_COMPACT_TOKEN_THRESHOLD` overrides the derivation entirely.
- **`openrouter:`** is one provider fronting many upstreams. It speaks the OpenAI wire format, so `ChatOpenAI` pointed at `OPENROUTER_BASE_URL` is the whole integration; the model name is the upstream id verbatim (`openrouter:anthropic/claude-sonnet-4.6`, `openrouter:deepseek/deepseek-r1:free` — the id splits on the *first* colon, so a `:free`/`:batch` variant suffix survives, and a `~` prefix marks an alias route). Needs `OPENROUTER_API_KEY`. Two things are easy to get wrong here:
  - **`stream_usage=True` is mandatory, not a tuning knob.** `ChatOpenAI` defaults it to `None` → False, and every run in this app streams — without it `usage_metadata` never arrives and `BudgetTracker`/`UsageAccumulator`/`PerfTracker` all measure zero tokens forever (the budget never trips, the perf badge reads unknown).
  - **Caching is decided per *upstream*, not per provider.** The `cache_control: ephemeral` blocks this app emits are Anthropic's spelling; OpenRouter forwards them to Anthropic upstreams and ignores them elsewhere, where caching is automatic (OpenAI, DeepSeek) or absent. `honors_cache_control(spec, enabled_providers)` in `core/model_catalog.py` is the single place that resolves it — `RunnerConfig.cache_enabled_providers` stays the operator switch, and this adds the sub-provider rule the switch can't express. Both `JarvisRunner.should_use_cache` and `_build_agent`'s no-runner fallback go through it, so they can't drift apart. Extended (`1h`) TTL stays anthropic-only.
- All consumers go through `available_models()` / `get_model_spec()` / `is_valid_model()` (built-in ∪ custom, deduped). The frontend reads the catalog via the GraphQL `models` query (`queries/models.py`, async — re-hydrates from DB so runtime additions show without a restart) — dropdowns populate automatically. `ModelSpec` normalizes on its `id` field in the Relay store, so the settings page's `network-only` refetch also refreshes the chat model dropdown (`useModels`, `store-or-network`) without a reload.

### Keeping the catalog honest (`model sync`)
`core/model_discovery.py` enumerates a provider's live models and diffs them against the catalog. It is a **lint, not a source of truth** — the catalog stays `BUILTIN_MODELS` + the `models.custom` layer, because `BUILTIN_MODELS[0]` is the compile-time `DEFAULT_MODEL` and has to resolve with no network, no key, and no provider outage.

```bash
uv run python main.py model sync                 # every discoverable provider
uv run python main.py model sync google_genai    # one provider
uv run python main.py model sync google_genai --probe     # + a real one-token call per catalog model
uv run python main.py model sync google_genai --add-new   # register new models into models.custom
```

**In the UI:** Settings → Models → **Sync** (`frontend/src/components/settings/ModelSyncModal.tsx`) runs the same thing over the `modelSync(provider, probe)` query (`queries/models.py` → `types/model_sync.py:run_model_sync`), which is where the *report* stops. Acting on it is the separate `addDiscoveredModels(models)` mutation, so a listing can never quietly rewrite the catalog: the operator ticks the models to register, rather than the CLI's all-or-nothing `--add-new`. Notes on that split:
- Discovery is blocking IO (sync httpx, boto3, provider SDKs), so each provider runs in `asyncio.to_thread` — a five-provider sync must not park the event loop and stall every live subscription.
- **A skipped provider is not a clean one.** `ModelSyncReport.skipped` carries the `DiscoveryError`; its empty finding lists mean *no data*, not *no drift*, and the UI renders it as `skipped`, never `✓ in sync`.
- `addDiscoveredModels` validates the entire batch before writing anything, so one bad id can't leave half of thirty registered. It is also the apply path for a `context_window` finding (`add_custom_model` upserts by id) — but only for custom models: a built-in's window is compiled in, so those rows render a `built-in` badge instead of a button.
- `add_custom_model(..., context_window=None)` **keeps** the stored window rather than clearing it. It backs both add and edit, and an edit that only changes a label must not discard a number discovery took a round trip to learn.
- In the Relay query every `id` is aliased to `modelId`. Relay's default `getDataID` keys a record by its `id` field alone, ignoring the typename, so a `DiscoveredModel`/`WindowFinding` selecting `id` would normalize onto the *same store record* as the `ModelSpec` with that id and overwrite its label/provider with report data.

Read-only by default. It reports four kinds of drift: catalog entries the provider no longer offers, models offered but absent from the catalog, `context_window` values the provider states where the catalog has `None`, and disagreements between the two.

- **Listing is not entitlement.** Measured against Google: `gemini-2.5-pro` is still returned by `ListModels` but 404s with *"no longer available to new users"* on an account that lacks it. A listing reports what the provider publishes, not what this credential may call — only `--probe` separates them. That is why probing is opt-in rather than the default: it costs one request per catalog model.
- **`--probe` must never let its own argument handling decide the verdict.** The output cap is not portable (`ChatGoogleGenerativeAI` rejects `max_tokens` with "Extra inputs are not permitted"), so `probe()` retries uncapped when the rejection is a *signature* error and judges on that call. Without this every Google model reads as unreachable.
- **The chat/non-chat split is a name heuristic, and deliberately advisory.** Google's `ListModels` publishes no output-modality field — `lyria-3-pro-preview` (music) and `gemini-2.5-flash-preview-tts` (speech) are structurally identical to `gemini-3.5-flash`, same `supportedGenerationMethods` and all. `looks_like_chat()` flags them so they can be skipped by `--add-new`, but they stay visible in the report; a heuristic that silently dropped models would reintroduce exactly the listing-disagrees-with-reality failure this command exists to catch.
- `context_window` backfill is the quiet win: most entries are `None`, so `compact_threshold()` falls back to a flat 80k. Google states the real number as `inputTokenLimit`. `ollama:*` stays `None` regardless, for the `num_ctx` reason above.
- Discovery adapters exist for `google_genai`, `anthropic`, `bedrock`, `ollama` and `openrouter`. OpenRouter's listing is **public** (no key), states `context_length` for every model, and — unlike Google — publishes real output modalities, so `likely_chat` there is a stated fact with the name heuristic only as a fallback. A provider in `KNOWN_PROVIDERS` but not in `DISCOVERABLE` (`meta`) is reported as unsupported rather than as "no models found", so a gap here can't read as a gap at the provider. Each provider degrades on its own — a missing key or unreachable host skips that one and the rest still run.

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
`token`, `thinking_token`, `step`, `artifact`, `todos_updated`, `queued_message`, `queued_withdrawn`, `queued_consumed`, `worker_start`, `worker_step`, `worker_token`, `worker_done`, `browser_step`, `interrupt`, `interrupt_resolved`, `approval_request`, `approval_resolved`, `workflow_event`, `budget_exceeded`, `budget_update`, `perf_update`, `done`, `stopped`, `error`

> `browser_step` currently has **no producer** — its only emitter was `tools/browser_agent.py`, which was deleted along with the `browser-use` dependency. The consumer plumbing (`core/streaming.py`, `BrowserStepEvent` in the `ChatEvent` union, the frontend hook) is left in place because removing a union member is a GraphQL schema change requiring a Relay regeneration. Wire a new emitter to it, or remove the plumbing as its own change.

Worker lifecycle events (`worker_*`) stream live from `tools/workers.py` and — except `worker_token` — are also persisted as `Step` rows (`source="subagent"`, `subagent="<role>:<idx>"`, result capped at `WORKER_RESULT_PERSIST_CAP`), so the activity sidebar can rebuild per-worker groups after a reload.

Custom events (anything except `token`/`thinking_token`/`step`) are dispatched from tools via `adispatch_custom_event(name, {"type": name, ...})` (the `"type"` key is required — `core/streaming.py:_process_chunk` switches on it). Token/thinking/step events flow naturally from LangGraph stream modes (`STREAM_MODES`).

### Mid-run message queue
A message typed while a run is in flight is **queued, not started** — two runs on one conversation share a LangGraph `thread_id` and would race the checkpointer (nothing but the web UI's disabled composer prevented that before; bots could always trigger it). `queueMessage(taskId, query)` / `unqueueMessage(taskId, messageId)` (`mutations/conversation.py` → `server/chat_runtime.py`).

**Every surface goes through one rule**, `route_to_live_run` — it is not the web UI's policy, because the surface that could always trigger the race is the bots. `in_flight_chat_task(conv_id)` scans `_tasks`, which covers pending *and* running work (`enqueue_chat_task` registers the state before it commits the job), so an accepted-but-unclaimed turn counts. `startTask` returns the **running** task's id with `queued: true` + `queuedMessageId` — subscribe to it either way, since that run is what will answer. Telegram/Discord send a short "added to what I'm working on" note instead of opening a second stream on the same `TaskState`. The rule **raises** rather than falling back to a concurrent run when the message can't join: attachments (text-only, above) or a run paused on an interrupt.

The one gap is a job that outlived a restart — nothing re-registers its `TaskState` until a worker claims it, so a message sent inside that window starts its own turn. `_adopt_queued_messages` is why that loses nothing.

**Delivery point: `core/agents.py:_drain_queued_input`, called at the top of `model_request_node`.** That node is the only chokepoint every main-graph LLM call passes, which makes it the one safe injection point — the `tools` node sits *between* an AIMessage's `tool_calls` and their results, and a HumanMessage spliced in there is exactly the orphan pairing Anthropic/Bedrock reject. Arriving here, the queued text lands after the current tool batch's results and before the next model call. Drained messages are appended to `raw_messages` *before* compaction and returned in the node's state update, so the same list feeds the LLM call and the checkpointer.

**Two carriers, and both are needed.** `TaskState.pending_input` is the fast path the node reads with no IO; a `messages` row with status `queued` is what renders in the transcript and what survives a restart. Delivery flips the row to `done` and reuses the row id as the `HumanMessage` id — which is also the retrieval-cache key (`_retrieved_volatile_parts` keys off the last HumanMessage's id), warmed by `prefetch_retrieval` at queue time so the drain doesn't put a memory lookup on the critical path of the iteration that consumes it.

`TaskState.drain_input()` is **sync on purpose**: no await between reading the list and clearing it, and every producer is on the same loop, so two writers need no lock.

Text only. A queued row has to be replayable from the DB alone after a restart, and the display row carries attachment *metadata*, not bytes.

**Delivery records `status: "delivered"`, not `done`** — and that is load-bearing, not bookkeeping. The assistant row is stamped at run *start*, so a mid-run message is created **after** the reply it lands in and sorts underneath it; nothing in creation order distinguishes that from an ordinary next-turn prompt. `orderDeliveredAboveReply` (`routes/c.$id.tsx`) lifts each delivered message above the nearest preceding assistant row — always the run that delivered it, so the move is exact. `_redispatch_queued` passes `done` instead, since a promoted leftover opens its own turn.

Lifecycle at the edges:
- **Clean finish with leftovers** → `_redispatch_queued` marks the first as delivered and `enqueue_chat_task`s it as a new turn; the rest stay `queued`.
- **Stopped / errored / restarted** → the queue is left as `queued` rows on purpose (don't fire a turn at a conversation the user just interrupted). `_adopt_queued_messages` runs at the top of every `chat_job_handler`, so the next run on that conversation picks them up — one path for restart, stop, and re-dispatch alike.
- **Paused on an interrupt** → `queueMessage` is refused. The run is waiting for *that* answer and will not reach a drain until it gets one; `resumeTask` is the path.

**UI** (`routes/c.$id.tsx`): while a run is up the composer stays live and switches to queueing (`InputBox`'s `queueing` + `onQueue`) — dashed rule, "Queue a message for this run…", and both Stop and Send. Queued rows are held out of the thread and rendered by `QueuedMessages` **below** the streaming turn, each with a withdraw button; putting them above would read as messages the agent had already been given. `useTaskEvents` treats `queued_message`/`queued_withdrawn`/`queued_consumed` as "the queue moved" and refetches the page rather than keeping a parallel list — the rows are the source of truth, which is what makes a reload and a second tab agree.

### Automation events
`token`, `thinking_token`, `step`, `done`, `error`

### Workflow events
`node_start`, `node_token`, `node_condition`, `node_done`, `node_error`, `map_start`, `map_item_done`, `workflow_done`, `workflow_error`

## Lazy tool loading (`tools/sdk.py`)
Tool schemas are re-sent on **every** LLM call, and `should_use_cache()` only returns true for `bedrock`/`anthropic` — so on any other provider the whole bound set is re-billed each iteration. To keep that small, only graph-coupled tools stay bound; the rest live in the kernel-preloaded `jarvis` SDK and are **discovered on demand** (`jarvis.help()` → categories, `jarvis.help("<category>")` → signatures). First-turn input went 9,051 → 5,690 tokens (bound schemas 5,781 → 2,118), and a later pass
(merging `write_artifact_file` into `write_artifact`, binding the board tools only for board runs, and
trimming descriptions + the system prompt) took the chat floor to **~3,100** — 996 tokens of bound
schema + 2,099 of system prompt.

**Two transports, chosen by what the operation needs:**
- **Reads** → direct `mode=ro` sqlite connection (cannot take write locks against the server) + `core.doc_index.get_embedder()` for semantic search.
- **Writes** → the server's own GraphQL API over HTTP (`jarvis.api()`), endpoint from `$JARVIS_API_URL` (default `http://127.0.0.1:8000/graphql` — **set this if you run uvicorn on another port**). A direct DB write from the kernel would persist the row but silently skip the in-process side effects that make it real: `_register_scheduler_job` (the cron would never fire) and `dispatch_board_tasks()`. Routing through the mutation also inherits its argument validation. Mutations take Relay `GlobalID`s, so the SDK encodes `base64("TypeName:rawId")` via `_global_id()`.

**What must stay bound, and why** — the rule is *coupling to the agent graph*, not "is it a write":
| Tool | Why it can't move |
|---|---|
| `run_cell` | the door into the kernel |
| `write_todos` / `set_todo_status` | return `Command(update=...)` state deltas; a separate process cannot write the reducer |
| `complete_task` / `block_task` | act on the **current run's** lifecycle via `ToolContext.board_task_id` — and so are bound **only when `build_agent(board=True)`**, since anywhere else they can do nothing but return "only available while executing a board task". `board` is part of the `_build_cached` key. |
| `spawn_workers` / `run_workflow` | instantiate subgraphs bound to this agent's LLM |
| `write_artifact` | its live side-panel event goes through this run's stream writer. One tool for both shapes: `content=` writes markdown, `file_path=` registers an on-disk audio/video/image file (`kind`/`mime_type` are inferred, not on the tool surface). |
| `remember` | there is no `createMemory` mutation to route to (only `updateMemoryItem`) |

Scope (`conversation_id`, `project_id`) is injected per kernel by `core/kernels.py:KernelSession.run()`, fed from `ToolContext` by `tools/code.py`, and re-injected after a kernel restart (`_sdk_scope`). Note it uses `conversation_id`, **not** the kernel key — workers override the key but must still resolve the parent conversation.

**Cross-conversation recall** lives in the `conversations` category: `search_conversations` (BM25 over `messages_fts` + a substring match on titles), `read_conversation` (defaults to the current chat, so it also recovers turns lost to compaction), and `list_conversations`. All three go through `_conversation_scope()`, which drops `ephemeral` (incognito) conversations unconditionally and — when the kernel has a `project_id` — restricts to that project. That project default is the point of the feature: project memory is a summary, and this is how the agent gets back to the transcript behind it (the `_project_volatile_parts` header in `core/agents.py` tells it to). Search additionally excludes the current conversation, which is already in context.

`list_conversations` **raises outside a project**, and takes no `surface`. A project is a bounded set of same-topic conversations, so enumerating it answers something; without one the "list" is just the user's entire chat history, which is a search with no question attached. No surface arg because only `surface="web"` conversations can join a project (`db/ops.py:set_conversation_project`). Scope reaches the SDK as the module global `_project_id`, injected per kernel by `core/kernels.py` from `ToolContext` — the agent cannot pick a project, and outside chat (automations, board, bots, CLI) nothing sets one.

**MCP servers choose per server** whether their tools are bound (`always`) or discovered through
the SDK's `mcp` category (`lazy`) — see "Load modes" under MCP Tool Loader. It is the same trade
this section makes, applied to third-party tools nobody here wrote the schemas for.

**Adding to the SDK:** define the function in `tools/sdk.py`, then register it in `_CATEGORIES` — `help()` renders signatures from `inspect.signature` + the docstring, so the docstring is the only documentation and costs zero tokens until discovered. If it needs a server-side side effect, add/route through a GraphQL mutation rather than writing the DB directly.

## Safety Posture
There are **no runtime LLM safety gates** — the per-turn input/output judge (`core/safety.py`) and the earlier per-tool-call judge were both removed. Safety rests on two layers instead: the agent's operating constraints in `core/system_prompt.md` (no secret exfiltration, no secrets in replies, no working harm, injection resistance) and deployment isolation (run the app in a container / on an isolated box). Historical messages persisted with status `blocked` by the old gates still render with a banner in `MessageBubble.tsx`.

## Automation Feature
Four input types:
- **prompt** — runs through `build_agent()`, streams tokens live via `TokenCoalescer`
- **code** — `asyncio.create_subprocess_exec` runs Python in a subprocess, streams stdout
- **webhook** — `httpx.AsyncClient.request` fires an HTTP call (for n8n, Zapier, etc.)
- **monitor** — a delta-gated prompt run: always stateful (previous observations live in the shared thread), the prompt is wrapped with compare-against-last-check instructions (`_MONITOR_WRAPPER`), and when the agent's reply starts with the `NO_CHANGE` sentinel the run finishes with status `no_change` and **no notification is sent** — silence means "nothing new". First run reports a baseline; changes produce a concise report that is delivered normally.

Execution lives in `server/automation_runtime.py`; CRUD + listing are GraphQL (`queries/automation.py`, `mutations/automation.py`). Scheduler: APScheduler (`core/scheduler.py`); cron jobs fire in a thread — validate expressions before saving. **Every path that creates/updates/deletes a scheduled automation must call `_register_scheduler_job`/`_remove_scheduler_job`** — the GraphQL mutations and the agent tools (`tools/automations.py`) both do; a missed registration means the schedule silently won't fire until restart.

**Cron expressions are interpreted in local time**, not UTC — a schedule is written by a human, so "0 9 * * 1" means Monday 9am where the machine is. The zone resolves once via `core/scheduler.py:get_scheduler_timezone()`: `scheduler.timezone` config setting → `JARVIS_TIMEZONE` env → the machine's local zone (tzlocal) → UTC. The setting is applied in the lifespan by `set_scheduler_timezone()` **before** `_scheduler.start()` — APScheduler cannot reconfigure a running scheduler. **Cron expressions are standard Unix crontab**, including `0`=Sunday..`6`=Saturday day-of-week numbering. APScheduler numbers weekdays `0`=Monday..`6`=Sunday and `CronTrigger.from_crontab` passes that field through *unremapped*, so every numeric weekday used to fire a day late (`0 9 * * 1` ran Tuesday; `0 9 * * 1-5` meant Tue–Sat). `core/scheduler.py:normalize_crontab()` rewrites the field as explicit weekday names — which APScheduler does read unambiguously — handling ranges, wrapping ranges, lists, steps and the `7`=Sunday alias; the other four fields and any syntax it doesn't model are passed through untouched.

**Build every trigger with `_cron(expr)`**, never a bare `CronTrigger.from_crontab(expr)`. `_cron` is the single place both corrections are applied, and skipping it reintroduces one or both: a trigger *instance* passed to `add_job` bypasses the scheduler's timezone (it silently defaults to its own), which made jobs fire at local time while `_compute_next_run_at` displayed UTC. Registration (`_register_scheduler_job`), display (`_compute_next_run_at`), and validation (the `createAutomation`/`updateAutomation` mutations and `tools/automations.py`) all route through it, so what the UI shows, what validation accepts, and when the job fires cannot drift apart.

**Stateful prompt automations** (`Automation.stateful`, opt-in; monitors are always stateful): every run shares the LangGraph thread + Conversation `automation_{automation_id}` (deterministic id — see `automation_conversation_id()` in `db/ops.py`), so the agent remembers previous runs. The runtime lazily creates the Conversation (surface=`automation`) and mirrors each run into Message rows (user prompt + assistant output, statuses matching chat). Overlapping runs of the same stateful automation are **skipped** (run status `skipped`) — the guard checks the Job table for a claimed sibling (`_has_inflight_sibling`), since two runs writing one checkpointer thread would race. `delete_automation` deletes the backing conversation (messages, artifacts, checkpointer thread) via `delete_conversation`. Stateless runs keep the old per-run thread `automation_{run_id}`.

## Task Board Feature (kanban)
A durable multi-agent kanban layered on the job queue. A `BoardTask` is one card: `todo → ready → running → blocked/done → archived`, plus `BoardTaskLink` parent→child dependency edges.
- **Dispatcher** (`server/task_board_runtime.py`): `dispatch_board_tasks()` is the single scheduling entrypoint — an APScheduler interval job (`register_board_dispatch_job`, every 15s) ticks it, and create/ready paths (GraphQL mutations, `create_task` tool, task completion) call it directly so dispatch doesn't wait for the tick. Each pass: promote `todo`→`ready` where all parents are `done` (parentless todos are parked and never auto-promote), then enqueue `ready` tasks up to `MAX_IN_PROGRESS`, ordered by priority desc. Serialized by an asyncio lock.
- **Runs**: each dispatch enqueues a `board_task` job with a **fresh UUID** (`job.id == BoardTask.job_id`) — unlike the other kinds, the job id is NOT the domain row id, because a task can re-run and finished Job rows would collide. The handler composes the prompt (title/body + `complete_task`/`block_task` protocol + completed parents' `summary`/`result_metadata` handoffs + optional skill), and runs `build_agent()` on the deterministic conversation `boardtask_{task_id}` (surface=`task`, `board_task_conversation_id()` in db/ops.py).
- **Terminal writes respect the agent — and run ownership**: `complete_task(summary, metadata)` / `block_task(reason, needs_input)` (tools/board.py, guarded by `ToolContext.board_task_id`) set the terminal status mid-run; the handler's `_finish_task(task_id, run_id, ...)` only applies its outcome when the row still belongs to this run (`job_id == run_id`), isn't re-queued (`ready`/`todo`), and is still `running` (no explicit tool call → final reply becomes the summary). Errors → `blocked` (`blocked_kind="error"`) + `failure_count` bump; stop → `blocked` (`"stopped"`). `stop_board_task` also handles the pending-job case (queue cancel finishes the job before any handler runs, so it flips the row itself).
- **Needs-input loop**: `block_task(reason, needs_input=True)` sets `blocked_kind="needs_input"`; the board card shows an answer box → `answerBoardTask(id, answer)` stores `pending_answer` + flips to `ready`. The next dispatch consumes the answer at claim time and runs `_RESUME_PROMPT` (answer only — the thread already holds the question) on the same conversation; a transient failure restores `pending_answer` so a retry still resumes with it. **Race guard**: a tool call flips the row while the agent loop is still wrapping up, so the dispatcher skips `ready` tasks whose previous job is still pending/running — without this, an instant answer/re-run would start a second concurrent run on the same thread.
- **Dependency editing**: `replace_board_task_parents` (db/ops.py) swaps a task's parent links with validation (missing parents, self-link, cycles via descendant walk) and re-parks a waiting task to `todo` when new parents aren't done. Exposed via `updateBoardTask(input.parentIds)`; the create/edit modal renders a parent picker.
- **Auto-decompose**: `decompose_board_task(task_id)` has a planner LLM (task's model, bare `build_llm()` call — no agent loop) split a *standalone waiting* task into 2–`MAX_SUBTASKS` subtasks (JSON; `depends_on` may only reference earlier indexes, so the graph is a DAG by construction). Subtasks become **parents of the original**, which is parked in `todo` FIRST (so a dispatch tick can't start it mid-decompose) and runs last as the synthesis step with every subtask summary as handoff. On unparseable LLM output the task is left untouched (`ValueError`). Exposed as the `decomposeBoardTask` mutation (card split-button + "Auto-split into subtasks" on create) and the `create_task(decompose=True)` agent tool.
- **Startup sweep**: `cleanup_zombie_running_rows` flips `running` board tasks back to `ready` only when no pending/running job holds them — tasks whose job survived restart are left for that job to re-run (flipping those too would double-dispatch).
- **GraphQL + UI**: `boardTasks`/`boardTask` queries, `createBoardTask`/`updateBoardTask`/`setBoardTaskStatus`/`answerBoardTask`/`decomposeBoardTask`/`deleteBoardTask`/`stopBoardTask` mutations, `boardTaskEvents(runId)` subscription (reuses the AutomationEvent union — same wire shape). `frontend/src/components/TaskBoard.tsx` + the `/board` route render the columns (3s polling + `useBoardTaskEvents` live token tail on running cards); cards link to the run transcript at `/c/boardtask_{id}`.

## Workflow Feature
Visual graph executor (`workflow/engine.py`):
- Definitions stored as JSON (`Workflow.definition`) with `nodes` + `edges` lists.
- `execute_workflow(run_id, definition, inputs, task_state)` runs BFS over the graph; `server/workflow_runtime.py` is the background trigger.
- Node types: `agent` (full LangGraph loop, supports `output_schema`), `conditional` (LLM yes/no router), `map` (parallel sub-workflow per list item), `start` (entry point with defaults), `router` (N-way classifier), `refine` (generate+evaluate loop), `approval` (human approval gate), `human_input` (free-text human input), `planner` (fast single-turn LLM that outputs JSON array of steps).
- **ADK multi-agent analogs** (`workflow/nodes.py`): `sequential` (SequentialAgent — steps in order sharing state, each step can have `output_schema`), `parallel` (ParallelAgent — branches concurrently), `loop` (LoopAgent — generate/evaluate until PASS or max_iter), `approval` (LongRunningFunctionTool analog — emits `approval_request` + `interrupt`, waits on `TaskState.resume_future`, supports `approved`/`denied` branching), `human_input` (Human-in-the-loop — emits `interrupt`, waits for free-text answer), `planner` (Planning agent — cheap single-turn planning, outputs plan list + text). These compose via `steps`/`branches` config lists and stream `node_token` per sub-step.
- **Structured output**: `AgentNode` and `SequentialNode` support `output_schema` (JSON schema dict or JSON string). Prompt is augmented to request JSON, `_extract_first_json()` extracts first JSON object/array, merges dict keys as top-level outputs. `output_schema_mode="strict"` raises if no JSON.
- **Resilience per node** (new): every node config can set `timeout_seconds` (float), `retries` (int, 0-10), `retry_delay_seconds` (float), `on_error` (`error` = branch stalls, `continue`/`skip` = emit `node_done` with `fallback_output` dict and keep branch), `fallback_output` (dict). Engine emits `node_retry` between attempts, respects `task_state.cancelled` during sleep, records `attempts` in node record. Implemented in `_run_node` wrapper with `asyncio.wait_for` + retry loop + `ContextVar` template context.
- **Expression language** (new, `core/workflow_template.py`): templates now Jinja2-backed if installed, fallback regex otherwise. Available vars: `{{var}}` = `{{inputs.var}}` (legacy), `{{inputs.foo}}`, `{{nodes.node_id.output_key}}` or `{{nodes.node_id}}` (full output dict), `{{workflow.foo}}` (top-level workflow inputs). Filters: `upper`, `lower`, `trim`, `default`, `tojson`/`json`, `fromjson` when Jinja available. ContextVar `_template_ctx` set by engine before each node so `_interpolate` inside nodes can resolve `{{nodes.*}}` even without explicit completed arg. Backward compatible with old `{{var}}` placeholders.
- **Agent-as-Tool**: `run_workflow(workflow_id, inputs_json)` (`tools/workflows.py`, bound) lets main agent invoke a saved workflow as sub-agent (ADK AgentTool). Emits `worker_start`/`worker_done` + `workflow_event` for visibility.
- Conditional nodes prune inactive branches via `pruned_edges`; pruned nodes never execute.
- **HITL**: workflow approval/human_input nodes pause via `TaskState.pending_interrupt_id` + `resume_future`, resumed via GraphQL `resumeWorkflowRun(runId, answer)` / `resolveWorkflowApproval(runId, approved, answer)` mutations (`mutations/workflow.py`). Events `approval_request`/`approval_resolved`/`interrupt`/`interrupt_resolved` flow via `workflowRunEvents` subscription (`WorkflowApprovalRequestEvent` etc).
- CRUD + runs are GraphQL (`queries/workflow.py`, `mutations/workflow.py`); the editor lives in `frontend/src/components/WorkflowEditor*.tsx`. All node types including `sequential`/`parallel`/`loop`/`router`/`refine`/`approval`/`human_input`/`planner` added to `WorkflowNodeType` in `lib/types.ts` for future UI support.

## Workflow Resilience + Template (new)
- `core/workflow_template.py`: `render_template(template, inputs, completed, workflow_inputs)` tries Jinja2 `Environment` with custom filters (`fromjson`, `json`/`tojson`), falls back to regex supporting `{{nodes.id.port}}`. ContextVar `set_template_context(completed, workflow_inputs)` / `reset_template_context` lets legacy `_interpolate` calls resolve nodes.
- `workflow/engine.py`: `_run_node` now parses `timeout_seconds`, `retries`, `retry_delay_seconds`, `on_error`, `fallback_output` from config, implements retry loop with `node_retry` event emission and cancellation check during delay sleep. Uses `ContextVar` to expose `completed` to nodes. `node_records` now include `attempts`, `timeout_seconds`, `retries_config`, `on_error`, `fallback_used`.
- `server/graphql/types/workflow_events.py`: new `WorkflowNodeRetryEvent(node_id, attempt, max_retries, error)`, added to `WorkflowEvent` union and `coerce_workflow_event`.
- `frontend/src/hooks/useWorkflowRunEvents.ts`: subscription includes `WorkflowNodeRetryEvent`, state stores retry info as error field.

## MCP Dynamic Management (ADK McpToolset runtime CRUD)
- `core/mcp.py`: new constant `MCP_DB_KEY="mcp.servers"`, helpers `_parse_db_raw`, `load_mcp_server_configs_with_db(db_cfg)`, `get_mcp_servers_from_db(session)`, `set_mcp_servers_in_db`, `add_mcp_server_to_db`, `remove_mcp_server_from_db`, `load_mcp_server_configs_async`. Merging order env < file < DB (DB wins). `McpManager.reload(connections?)` method clears client/tools and re-initializes (avoids dead-lock by releasing lock before re-init).
- `server/entrypoint.py`: lifespan now loads DB mcp config via `async_session` + `get_mcp_servers_from_db` and merges via `load_mcp_server_configs_with_db` before `McpManager.initialize(merged)`.
- GraphQL: type `McpServer(name, config JSON string, transport, command, url, tool_count, enabled, load_mode, tools)` in `types/mcp.py` (+ `McpTool`, `McpToolResult`), queries `mcpServers` + `mcpTools(server)` in `queries/mcp.py`, mutations `addMcpServer(name, configJson)`, `updateMcpServer`, `removeMcpServer`, `reloadMcpServers`, `setMcpServerLoadMode`, `setMcpDefaultLoadMode`, `callMcpTool` in `mutations/mcp.py` (persist to DB, reload manager). Wired in `schema.py`.
- `mcpServers` returns the **union** of configured and connected servers — a server the manager still holds but config no longer names is live and still billing tools, so hiding it until restart would be a lie.
- Frontend: `schema.graphql` includes `McpServer` and `WorkflowNodeRetryEvent`; `pnpm relay` compiles.

## Planning Mode (ADK planning analog)
- `core/planning.py`: `get_planning_mode()` reads `JARVIS_PLANNING_MODE` env (auto/always/off, default auto), `should_auto_plan(query)` heuristic (multi-line, 80+ chars, numbered/bullet list, keywords like research/implement/build..., phrases "and then"/"first"/"step"), `should_plan_for_query`, `build_planning_directive` (injected as volatile `## Planning Required` telling model to call `write_todos` first), `prefill_todos_with_llm` optional fast path via `JARVIS_PLANNING_PREFILL=1`.
- `core/agents.py`: `model_request_node` now checks if todos empty and injects planning directive via `build_planning_directive(_latest_user_text)`, added to `volatile_non_cached` before compaction. So first iteration forces `write_todos` for complex queries without extra LLM call.
- `core/system_prompt.md`: Planning section strengthened — MUST call `write_todos` first when `## Planning Required` appears, concrete 3-7 steps, verb-led, update list if scope grows, skip for one-shot Q&A.
- `workflow/nodes.py`: new `PlannerNode` (`node_type="planner"`, alias `"plan"`) — single-turn LLM call with instruction to output JSON array of steps (max_steps 1..10), parses via `_extract_first_json`, fallback splits numbered/bullet lines, emits `node_token` per step, returns `{plan: [str], plan_text: "1. ...", result: [str]}`. Registered in `NODE_REGISTRY`.

## Artifact Versioning (ADK ArtifactService analog)
- `db/models.py`: `ArtifactVersion` (artifact_id FK cascade, version int unique per artifact, title, filename, created_at). `Artifact.versions` relationship.
- `db/ops.py`: `create_artifact_version()`, `list_artifact_versions()`, `get_artifact_version()`, `get_latest_artifact_version_number()`. `delete_conversation` + `delete_artifact` collect version file paths before cascade and unlink them.
- `tools/artifacts.py`: `write_artifact()` dispatches to `_write_markdown_artifact` / `_write_file_artifact` and versions: on create writes live `{id}.md` + versioned `{id}_v1.md` + DB row v1; on update migrates old file without history (saves as v1) then writes live + new version file `_v{latest+1}.md`. `read_artifact(artifact_id, version=None)` reads specific version when provided, otherwise live. `list_artifacts()` includes `versions` count, new tool `list_artifact_versions(artifact_id)`.
- `core/agents.py`: binds `list_artifact_versions` alongside other artifact tools.
- GraphQL: `ArtifactVersion` type with `content` resolver, `Artifact.versions` field + `version_count`, query `artifactVersions(artifactId)`. Files cleaned up on conversation/artifact delete.
- Frontend: schema regenerated; artifact UI can show version history via `versions` field.

## Approval / Long-Running Tools (ADK LongRunningFunctionTool analog)
`core/approval.py`: generic human-in-the-loop approval layered on LangGraph's
`interrupt` mechanism (previously used only by the browser tool).
- `request_tool_approval(tool_name, args, reason)` emits `approval_request`
  SSE event (structured: tool, args, reason) and suspends via
  `current_ctx().request_input({"type":"approval", ...})`. Frontend shows
  `InterruptPrompt` with the reason + args; user replies `approve`/`deny`
  (free-text, parsed by `is_affirmative_answer()` with affirmative/negative sets).
  On resume emits `approval_resolved`.
- `require_approval(reason_template)` decorator for tools that always need approval.
- Currently wired into `write_file` (overwrite guard) and
  `delete_automation` / `delete_workflow`. Additional tools can opt-in via
  the same helper. Outside a run (tests/CLI), auto-approves.
- GraphQL `events.py` adds `ApprovalRequestEvent` / `ApprovalResolvedEvent` /
  `WorkflowToolEvent` to `ChatEvent` union; `frontend/src/hooks/useTaskEvents.ts`
  surfaces approval as pending interrupt (rich question = `tool: reason`).
- Streaming: `core/streaming.py` forwards `approval_request`/`approval_resolved`/
  `workflow_event` custom events.

## Approvals Inbox (`/approvals`)
The one place that answers "what is waiting on a human, and where is it from".
Every request is a durable `Approval` row (`db/models.py`), so it outlives the
run that raised it; `queries/approval.py` is a single indexed read of that
table, not a merge of live and stored state.

**Two shapes**, distinguished by whether `Approval.action` is set:
- **Blocking** (`action IS NULL`) — a run is suspended *right now*: a chat
  LangGraph interrupt, a paused workflow `approval`/`human_input` node, or a
  board task with `blocked_kind="needs_input"`. Resolving hands the answer to
  the waiting run.
- **Deferred** (`action` set) — nobody is waiting. The operation was *recorded*
  instead of performed, and approving is what executes it
  (`core/approvals.py:ACTIONS`). Rendered as "runs on approval".

**Why deferred exists at all:** the `jarvis` SDK runs in a **separate kernel
process** (`core/kernels.py` uses jupyter_client's `AsyncKernelManager`), where
`current_ctx()` finds no LangGraph runtime — `request_tool_approval` cannot
reach it, and would silently auto-approve via `_is_headless`. SDK writes arrive
as ordinary GraphQL requests, so the gate lives in the resolver, which has
nothing to suspend. Deferred is also the better shape where blocking *is*
possible: a human may take hours, and a blocked run pins a worker slot and a
kernel the whole time.

**`TaskState.pending_interrupt`** (`core/state.InterruptRequest`) still carries
the live payload beside `pending_interrupt_id`, which alone only says *whether*
a run is paused. **Set and clear it via `set_interrupt()` / `clear_interrupt()`**
so the id and payload cannot drift. Producers write the durable row too, via
`core/approvals.record_blocking_request` — best-effort, because failing to
record must never take down a run that is already suspended.

### Durability is not uniform — reconcile at startup
`core/approvals.reconcile_startup()` runs in the lifespan **after** the zombie
sweep (which is what decides whether a run still has a job to be re-claimed by):
| Source | On restart | Reconciliation |
|---|---|---|
| deferred | nothing was waiting | stays `pending` |
| board_task | the block is a column on the task | stays `pending`; rows backfilled for tasks blocked before this table existed |
| chat | LangGraph checkpointed the interrupt | stays `pending` **iff** a `Job` row still exists in `pending`/`running` |
| workflow / automation | the engine holds its whole run state in memory (BFS frontier, asyncio futures) and checkpoints nothing, so the graph re-runs from the start and asks again | `expired` |

`expired` is the point: an unanswerable row listed as pending is a button that
resumes nothing. **`_run_agent_task` reads the row to resume chat across a
restart** (`_pending_approval_for`) — without it the handler re-sends the
original prompt, discarding the checkpointed pause and replaying the turn.

### Closing rows — the chokepoints
A row left `pending` after its question is moot is the failure mode, so closing
is centralized rather than spread across call sites:
- `db/ops.update_board_task` — **every** board status change flows through it;
  anything not `blocked`/`needs_input` closes the task's open rows.
- `core/streaming._finalize_message` — the run ended, so expire what it owed.
- `resumeTask` / `resumeWorkflowRun` / `resolveWorkflowApproval` — answering in
  the chat view or on a workflow page also clears the inbox row.
- `server/task_board_runtime.answer_board_task` — shared by the board card and
  the inbox. It closes as `answered` **before** calling `update_board_task`,
  whose own cleanup would otherwise file a genuine answer as `cancelled`.

### Gating the agent's destructive writes
**Off by default — the framework is wired, the enforcement is opt-in.** Every
gated call site resolves an `ActionSpec` and consults `required_actions()`, but
`_DEFAULT_REQUIRED` is empty, so today every action passes straight through and
auto-approves (logged at debug). Turning it on changes what the agent may do
unsupervised, which is an install-level decision rather than a default; the
plumbing being live means enabling it is a config write, not a deploy:

```bash
uv run python main.py config set approval.required_actions "all"
uv run python main.py config set approval.required_actions "delete_workflow,delete_skill"
uv run python main.py config set approval.required_actions "none"   # pass-through
```

`"all"` resolves through `ACTIONS` at read time, so a new `ActionSpec` is gated
without editing the setting again.

`core/approvals.gate_action` records and raises `ApprovalRequired` instead of
performing the operation. Wired into `deleteWorkflow`, `deleteAutomation`,
`deleteSkill` — the SDK's only destructive writes.
- **Caller discrimination**: `tools/sdk.py:api()` sends `X-Jarvis-Caller: agent`
  (+ `X-Jarvis-Conversation`), surfaced as `info.context["caller"]` by
  `graphql/context.py`. A human clicking Delete in the UI *is* the approval, so
  only `caller == "agent"` is gated. This is an **ergonomic** boundary, not a
  security one — the header is self-asserted; deployment isolation is the
  actual control.
- Gate **before** any side effect: `deleteAutomation` gates before
  `_remove_scheduler_job`, or a denied approval would still leave the
  automation unscheduled.
- Duplicate requests collapse (`_find_duplicate`), so an agent retrying in a
  loop does not fill the inbox.

`resolveApproval(id, answer)` (`mutations/approval.py`) is the single entry
point — `core/approvals.resolve` dispatches on the row, so the inbox never
needs to know which of the three resume paths applies. Approve/deny parsing is
`is_affirmative_answer`, which **denies on anything ambiguous**. Deferred
actions execute *before* the row is closed, so a failure leaves it answerable;
`resolve_approval_row` refuses an already-closed row, so a double-submit cannot
delete twice.

Frontend: `routes/approvals.tsx` + `hooks/usePendingApprovals.ts` (an external
store like `useRunningTasks`, but polling whenever subscribed — an approval can
appear with no run in flight, so there is no push chokepoint to stop polling
on; forced refreshes wait out an in-flight poll rather than adopting its
pre-write snapshot) and a nav badge in `__root.tsx`.

> **Still unreachable:** `core/approval.py:request_tool_approval` (the blocking,
> in-process helper) is called only by `delete_workflow`/`delete_automation` in
> `tools/workflows.py`/`tools/automations.py`, both `[unbound]`. The gating that
> actually fires today is the deferred path above. Board and automation runs
> also have no resume loop (they `break` on interrupt), which is why
> `_is_headless` still auto-approves them — deferred sidesteps that entirely.

## Config + Maintenance (Settings → Config / Maintenance)
Everything `main.py` can do is also reachable from the web UI. Two tabs close
the gap the dedicated tabs left:

**Settings → Config** is the generic editor over `config_settings` — the table
`config set/get/list/delete` writes. `core/settings_admin.py` owns two things:
- `KNOWN_SETTINGS`, a registry of label/description/kind per key, so the page
  lists a key **before** anyone sets it (`Setting.isSet: false`). A page that
  only showed stored rows would never tell you the Telegram allowlist exists,
  which is exactly what the CLI made you read the docs for. Unknown keys stay
  allowed — the table is open-ended and the CLI never validated keys either.
- `apply_setting(session, key)`, the in-process side effects. **This is why the
  resolver exists rather than a raw row write**: the CLI writes from a process
  that then exits, but these run *inside* the server, where several keys are
  cached in memory (`tools.policy` → the tool-policy cache + every compiled
  agent graph, `mcp.*` → the connected client, `embedding.model` → the embedder
  override) and a plain write would be ignored by the very process serving the
  page that made it. Delete goes through the same path — the caches never knew
  a row was involved, only that the effective value changed.
  `scheduler.timezone` is the one key that cannot be applied live (APScheduler
  won't reconfigure a running scheduler), and it says so instead of lying.

Keys another tab owns (`tools.policy`, `models.custom`, `mcp.*`) carry
`managedBy` and are refused unless `allowManaged: true`: they hold serialized
state that tab rewrites wholesale, so a hand edit here is silently discarded on
its next write. Shown read-only, behind a "Show managed keys" toggle.

**Settings → Maintenance** covers the two subcommands that previously needed a
terminal on the box:
- `pruneCheckpoints(dryRun)` runs the **online** sweep
  (`core/checkpoint_retention.py`), not the CLI's offline one — it is safe
  against a live server because it skips threads with an in-flight run and
  anything under an hour old. `checkpointStats` reports the post-guard numbers
  (it includes a dry run), so "3 of 100 prunable" is the guards working, not a
  bug. The result `note` says the file will not shrink: freed pages go on
  SQLite's freelist, and VACUUM stays `main.py maintenance prune-checkpoints`
  with the server stopped.
- `downloadVoice(force)` fetches the Piper voice `POST /tts` 404s without.
  `core/voice.py` is shared with the CLI (one implementation, two front ends);
  it writes to a `.part` sibling and renames, because `exists` is all the
  status check can cheaply do and a truncated `.onnx` would read as ready
  forever. Blocking IO, so the resolver runs it on a worker thread.

`memory show/set/reset` land on `/memory`: the legacy `AGENTS.md` blob renders
with Edit (`updateMemory`) and Clear (`deleteAgentMemory`) below the discrete
items. `deleteAgentMemory` is distinct from `updateMemory("")` — the agent's
fallback branches on `exists`, so an empty blob and a missing one differ.

Not ported, deliberately: `start` (it boots the server that serves the UI) and
`reports` / `view`, which read loose `.md` files from a `./reports` directory
nothing in the app has written since artifacts replaced it.

## Tool Policy (Settings → Tools)
One page listing **every** tool the agent can reach and two switches per tool:
`enabled` (may it be called at all) and `requires approval` (a human must say
yes first). `core/tool_policy.py` owns the inventory and the storage; the
families it merges have nothing else in common:

| kind | key | where it lives | in the prompt? |
|---|---|---|---|
| `bound` | `bound:<name>` | the graph's `main_tools` (`core/agents.py`) | yes — schema on every LLM call |
| `sdk` | `sdk:<name>` | `tools/sdk.py` `_CATEGORIES`, called from a kernel | no — discovered on demand |
| `mcp` | `mcp:<server>/<tool>` | MCP servers | only for `always` servers |

**Storage is one `config_settings` row** (`tools.policy`), a JSON map holding
*only non-default entries* — a fresh install stores nothing. It is a settings
row rather than a table because the `jarvis` SDK must read it from a separate
process over its read-only sqlite connection, with no ORM and no HTTP hop.
Reads are sync and cached for 2s (hot path: every agent build, every SDK call).

**Disabled means unbound, not refused.** `_allowed()` filters the toolset in
`_build_agent` (main agent *and* worker roles), so a disabled tool never enters
the model's schema list; `set_tool_policy` drops the compiled-graph cache
(`invalidate_agent_cache`) so the change lands on the next run, not the next
restart. In the SDK it is also hidden from `jarvis.help()` — advertising a
locked door only invites a call that raises.

### Blocking approval that works on every surface (`core/tool_gate.py`)
The pre-existing mechanisms each cover one caller: `core/approval.py` blocks by
raising a LangGraph **interrupt** (chat only — `_is_headless` auto-approves
board/automation/bot/workflow), and `core/approvals.py`'s deferred shape blocks
nothing. The gate makes the durable `Approval` row itself the rendezvous, so it
does not depend on the caller's runtime:
- **In-process** (the graph's gate node, `callMcpTool`) — create the row, then
  `await` an `asyncio.Event` registered by approval id, with a DB poll under it
  so an answer that arrives from another path still lands.
- **In the kernel** (`tools/sdk.py:_await_gate`) — the row is created through
  `requestToolApproval` and then *polled* over the read-only connection. Same
  row, same resolution path, no new transport.

`core/approvals.resolve` dispatches `source == "tool"` to `_resolve_tool_gate`,
which only closes the row — nothing is executed and no `resume_future` is woken,
because the waiter is watching the row. That is exactly what lets a board task,
an automation and a kernel call block the same way a chat does. A restart
`expire`s these rows: the waiter died with the process.

**Gating happens at the graph node, not by wrapping tools** (`core/tool_gate_node.py`
replaces `ToolNode`). `write_todos`/`set_todo_status` take `InjectedToolCallId`/
`InjectedState` and return `Command` deltas, so a wrapper that re-declared their
schema would break injection or the reducer write. The AI message in history
keeps **all** its tool calls; only the copy handed to `ToolNode` is narrowed to
the approved ones, so every `tool_use` still gets exactly one `tool_result` —
a denial must not leave the orphan pairing Anthropic/Bedrock reject on the
*next* call.

**`run_cell`'s 60s cell timeout is suspended while a request is open.** Without
that, "this tool needs approval" would mean "and you have 60 seconds to answer":
`core/kernels.py:_hold_for_approval` re-checks `has_open_gate(conversation_id)`
every 5s and only interrupts once nothing is waiting, capped at
`MAX_HOLD_SECONDS` (30 min, matching `JARVIS_TOOL_GATE_TIMEOUT`). A stop still
interrupts immediately — that path is `CancelledError`, not the timeout.

The cost is real and deliberate: a waiting run holds its worker slot (and, for
an SDK call, its kernel) until answered or timed out. A timeout denies.

**Frontend:** `components/settings/ToolsTab.tsx` (a fourth Settings tab, grouped
by family then server/category, with search). `AgentTool` carries a plain `id`
(the policy key) so Relay normalizes `setToolPolicy`'s response onto the records
already rendered — without it the toggle shows stale until a refetch. In chat,
`ApprovalRequestEvent.approvalId` marks a gate: `InterruptPrompt` then answers
with `resolveApproval` instead of `resumeTask`, since there is no interrupt to
resume — the run is parked inside the tool call.

## Context Caching + Runner (ADK Runner / ContextCacheConfig analog)
- `core/context_cache.py`: `CacheSegment` (name, content, cacheable, token_est),
  `ContextCacheConfig` (enabled, max_breakpoints=4, min_chars=50, cache_ttl),
  `build_cached_system_message()` builds a `SystemMessage` with up to 4
  `cache_control: ephemeral` blocks. Logs per-call cache stats.
  - **Cache TTL** is `5m` (the API default) unless `JARVIS_CACHE_TTL=1h`.
    `resolve_cache_ttl(provider)` gates it to **anthropic only** — Bedrock's
    Converse API models cache points differently, and an unsupported field there
    fails *every* call rather than degrading, so it stays on 5m until verified.
    At `5m` the `ttl` key is **omitted entirely**, keeping the request body
    byte-identical to before the setting existed. 1h is a cost trade, not a free
    win: cache *writes* bill at 2x base instead of 1.25x, so it only pays off if
    reads land within the hour. Resolved once per compiled agent in
    `_build_agent`, alongside `use_cache`.
  - Layout: [system prompt cached] + [core_memory cached] + [skills cached] +
    [project_instructions cached] + [project_memory + todos volatile uncached].
  - Tiny segments (<50 chars) skip cache to avoid breakpoint waste.
  - ADK pattern: stable content (system, memory, skills, instructions) gets its
    own cached block; highly volatile (todos, project_memory live edits,
    summary SystemMessages folded from history) stays after last breakpoint.
- `core/messages.py`: legacy `_make_system_message()` (single breakpoint) kept,
  new `_make_system_message_multi()` delegates to `context_cache`. `build_llm_messages()`
  now accepts `cache_segments: list[CacheSegment]` for multi-breakpoint.
- `core/agents.py`: `model_request_node` classifies `_retrieved_volatile_parts`
  + `_project_volatile_parts` into cacheable vs volatile: agent memory,
  relevant memories, skills, project header/instructions → cached;
  project memory, current tasks → volatile suffix. Logs cache stats via
  `get_last_cache_stats()`. Loads MCP tools via `get_mcp_tools_sync()` (ADK McpToolset)
  and appends to both main and general/researcher worker tool lists.
- `core/runner.py`: `JarvisRunner` (ADK Runner analog) owns checkpointer/store/
  queue/http/config, exposes `build_agent()`, `should_use_cache()` (only `bedrock, anthropic`),
  `get_context_cache_config()`, `get_budget_limits(kind)`. Lifespan
  in `server/entrypoint.py` initializes MCP (`initialize_mcp()` → cached tools), creates
  global runner via `set_runner()` and tears down with `set_runner(None)` + `get_mcp_manager().close()`.
  Future backends (Postgres, Redis) slot in here.

## Budget Tracking (MAF TokenUsageTermination / BudgetTracker analog)
- `core/budget.py`: `BudgetLimits` (max_total_tokens, max_input, max_output, max_llm_calls,
  max_tool_calls, max_duration_seconds), `BudgetTracker` (per-run mutable, syncs to
  `TaskState.input_tokens/output_tokens/llm_calls/tool_calls/budget_exceeded`),
  `BudgetCallbackHandler` (LangChain callback that feeds tracker and cancels on exceed),
  `get_budget_limits_for_task(kind)` (env overrides: `JARVIS_BUDGET_MAX_*`; tries runner first).
- `core/state.py`: `TaskState` now holds `input_tokens`, `output_tokens`, `llm_calls`,
  `tool_calls`, `budget_exceeded`, `budget_reason`, `_budget_tracker`.
- Wiring: chat (`server/chat_runtime.py`), board (`task_board_runtime.py`), automation
  (`automation_runtime.py`), workflow (`workflow_runtime.py` + `workflow/nodes.py:_run_agent_text`)
  each create a `BudgetTracker` + `BudgetCallbackHandler` per run. On exceed emits
  `budget_exceeded` event (`core/streaming.py` forwards) → `BudgetExceededEvent` GraphQL
  (`server/graphql/types/events.py`) → `useTaskEvents.ts` surfaces error. Chat finalizes
  with budget message; board sets `blocked_kind="budget"`; workflow raises to error.
- Runner: `RunnerConfig` holds `budget_max_*` defaults, merged into `JarvisRunner.get_budget_limits()`.

## Throughput Tracking (prefill / eval tok/s)
`core/perf.py` measures how *fast* a run went, which the token counters can't answer:
`UsageAccumulator`/`BudgetTracker` carry no timing, and `BudgetTracker.snapshot()['elapsed_seconds']`
spans tools, retrieval, and compaction, so it is not a throughput denominator. `PerfCallbackHandler`
times each LLM call and splits it at the first streamed chunk — **prefill** (start → first token,
i.e. prompt processing) and **decode** (first token → end, i.e. generation/"eval") — feeding a
per-run `PerfTracker`. Aggregates are **token-weighted** (Σtokens / Σseconds), never a mean of
per-call rates. Callbacks propagate, so worker/subagent calls are included, same scope as `UsageAccumulator`.

Three things make a naive `tokens / elapsed` wrong, and each has a guard:
- **Prompt caching.** A cache hit does almost no prefill work. `cache_read` (LangChain folds it
  into `input_tokens` and reports the split under `usage_metadata.input_token_details`) is
  subtracted; a *fully* cached call is excluded from the prefill aggregate rather than logged
  as 0 tok/s. Without this, prefill on a cached turn reads several times the real rate.
- **Buffered streams.** The split assumes the first chunk marks the start of generation. A provider
  that flushes at the end violates that: measured on `google_genai:gemma-4-31b-it`, a short reply
  landed 7 tokens in the final 12ms — an apparent 570 tok/s from a model that runs at ~38.
  The guard is a floor on the decode *span* (`_MIN_DECODE_SECONDS`, 0.25s), **not** on chunk count
  or tokens-per-chunk — a real 74-token generation arrives in as few as 6 coarse chunks and is
  perfectly measurable. Below the floor the call is `source="prefill_only"`: eval withheld, prefill
  kept (a swallowed flush inflates TTFT, which *understates* prefill — wrong in the safe direction).
- **Provider-reported durations win.** Ollama's `prompt_eval_duration`/`eval_duration` are measured
  server-side and exclude queueing/transport; when both are present the call is `source="provider"`.

`source` is one of `provider` | `measured` | `prefill_only` | `unsplit` (nothing streamed).
**Any rate may legitimately be `None`** — callers must render "unknown", never 0.

Wiring: `_run_agent_task` creates the `PerfTracker` (`TaskState._perf_tracker`) and passes it to
`get_plugin_manager(perf_tracker=...)` / `build_callbacks(perf_tracker=...)` (`PerfPlugin` in
`core/plugins.py`), appending the handler directly if a plugin manager didn't. Per LLM call it emits
`perf_update` → `PerfUpdateEvent` → `useTaskEvents.ts` (`perf` state) → the ActivitySidebar budget
box. On finalize, `PerfTracker.message_perf()` persists `ttft_ms`/`llm_ms`/`prefill_tps`/`eval_tps`
onto the `Message` row (nullable Floats, migrated in `db/engine.py:_migrate`), rendered by
`PerfBadge` in `MessageBubble.tsx` as `TTFT / pp / tg` (llama.cpp's shorthand). `ttft_ms` is the
**first** call's TTFT — what the user waited for; `llm_ms` sums every call's round trip and so is
always less than the wall-clock turn. Only chat persists; other runtimes get the measurement
through `build_callbacks` when they pass a tracker.

## MCP Tool Loader (ADK McpToolset analog)
- `core/mcp.py`: `load_mcp_server_configs()` merges env `JARVIS_MCP_SERVERS` (JSON dict or list)
  + file `~/.jarvis/mcp.json` / `$WORK_DIR/mcp.json` / `./mcp.json` (supports Claude Desktop's
  `mcpServers` key, or `{servers: {...}}`). Normalized to `dict[name -> connection dict]`
  for `langchain_mcp_adapters.client.MultiServerMCPClient`.
- **Tools are loaded per server**, not via one flat `get_tools()`. The adapter puts annotations
  in a tool's `metadata` and never the server name, so `get_tools(server_name=...)` is the only
  source of attribution — and everything downstream needs it: load-mode filtering, honest tool
  counts (the old UI guessed by substring-matching the server name against tool names), and
  `callMcpTool` routing. It also downgrades one unreachable server from "no MCP tools at all"
  to "that server has none".
- Lifespan: `server/entrypoint.py` calls `initialize_mcp()` early; `close()` on shutdown.
- Agent: `core/agents.py` `_build_agent` binds `get_mcp_tools_sync()` (see load modes below) to
  main + general/researcher worker tools (`_ROLE_TOOLS`). If no config, returns [] (no-op).
- Dependency: `langchain-mcp-adapters>=0.3.0` (+ `mcp` transitive). Optional — safe fallback.
- GraphQL/events unchanged; bound MCP tool calls flow as normal `tool` steps.

### Load modes: `always` (bound) vs `lazy` (on demand)
Tool schemas are re-sent on **every** LLM call and `should_use_cache()` is only true for
anthropic/bedrock, so a chatty MCP server is billed on every iteration of every run. Each server
picks its own mode:
- **`always`** (the default — unchanged behaviour): tools are bound to the agent, appear as normal
  `tool` steps, and the model gets their schemas without asking.
- **`lazy`**: connected and callable, but unbound. Nothing of the server's schemas enters the
  prompt; the agent reaches it through the `jarvis` SDK's `mcp` category.

The mode is a jarvis-only key **inside the connection dict** (`"x-jarvis-load": "always"|"lazy"`),
so it round-trips through all three config sources with no extra plumbing — plus a per-server DB
override (`mcp.load_modes`, written by `setMcpServerLoadMode`) kept as a *separate map* so
flipping a server defined in env or `mcp.json` doesn't snapshot the rest of that config into the
DB, where it would win forever and silently ignore later edits to the source file. Fallback for
servers that declare nothing: `mcp.default_load_mode` (or `JARVIS_MCP_DEFAULT_LOAD`), default
`always`. **`strip_jarvis_keys()` removes the key before the dict reaches the client** —
`create_session` splats every non-`transport` key into the transport constructor, so a leaked key
is a `TypeError` at connect time, not an ignored field.
- Flipping a mode changes what is bound, and the compiled agent graphs bake that in, so
  `McpManager.set_load_mode` drops them (`_invalidate_agent_graphs`) without reconnecting — the
  tools are already loaded, only the binding decision changed.
- **A lazy server must still be advertised, or it is invisible**: the agent cannot ask for a
  capability it has never heard of. `_mcp_volatile_parts()` (`core/agents.py`) injects a
  `## MCP Servers (on demand)` cache segment with *names and tool names only* — the same trade
  skills make, where the body stays behind `use_skill`. Segment name `mcp_servers`, ranked in
  `_SEGMENT_STABILITY`. Returns [] when every server is `always` (their bound schemas already
  describe them) or when a lazy server loaded no tools.

### `jarvis.mcp_call` — how a lazy tool actually runs
The SDK runs in a **separate kernel process**; the MCP client and the stdio subprocesses it owns
live in the server. A second client in the kernel would double-launch every server, so the kernel
routes through the API instead — the same write transport as the rest of the SDK:

```
run_cell → kernel → jarvis.mcp_call(...) → api() POST /graphql (X-Jarvis-Caller: agent)
        → callMcpTool resolver (server process) → McpManager.call_tool → tool.ainvoke
```

`mcp_servers()` / `mcp_tools(server)` / `mcp_help(server, tool)` / `mcp_call(server, tool, args)`
are the `mcp` category in `tools/sdk.py`. Discovery is deliberately two hops: the listing carries
descriptions only, and `mcp_help` is the one that pays for the JSON Schema.
- The adapter opens **a fresh MCP session per tool call** (tools are built with
  `session=None, connection=...`), so a lazy call costs the same as a bound one and there is no
  session state to thread through the request.
- `call_tool` invokes with a **ToolCall payload, not a bare dict**: with `handle_tool_errors=True`
  (the adapter default) an MCP-level failure comes back as ordinary *content*, and the resulting
  `ToolMessage.status` is the only thing that distinguishes it from success. Hence
  `(text, is_error)` → `McpToolResult { content, isError }` → `"MCP tool error: …"` text the agent
  can read and correct, rather than an exception.
- `api()` takes a `timeout` (MCP calls can outlast its 30s default); the server-side
  `asyncio.wait_for` is set below the client's so a hang returns a clean error instead of a
  dropped connection.
- **What lazy costs**: calls are code inside a `run_cell` step, so the activity sidebar shows
  `run_cell`, not the tool name; the model builds args without the schema in context (hence the
  "check `mcp_help` before the first call" instruction); and interactive elicitation/auth
  callbacks can't drive an interrupt from the kernel path.
- Gating gets *better*, though: `callMcpTool` is wired to the `call_mcp_tool` `ActionSpec`
  (`core/approvals.py`) for `caller == "agent"`, so MCP writes are gateable through the same
  deferred-approval path as the SDK's other destructive writes. Off unless the operator opts in
  (`approval.required_actions`), like everything else in `ACTIONS`.

Tests: `tests/test_mcp_load_modes.py` (modes, filtering, the manager) and
`tests/test_mcp_integration.py`, which spawns **real** stdio MCP servers
(`tests/fixtures/*_mcp_server.py`) and drives the GraphQL resolvers — every assumption here is a
contract with langchain-mcp-adapters that a mock would happily agree with while being wrong.

## Memory Feature
Agent memory has **two layers**, selected by whether an embedder is configured (`embeddings_available()`):
- **With an embedder (default): discrete vector memory.** Atomic items live in the `Memory` SQL table (`kind` = `core` | `fact`), embedded on write. `core/memory_store.py` exposes `upsert_memory`, `load_core` (always-on `core` items), and `search_memory` (top-k cosine over `fact` items). The agent reads/writes via the `remember` / `search_memory` tools (`tools/memory.py`, bound only when embeddings are available). Each turn `core/agents.py` (`_memory_volatile_parts`) injects the `core` items + the items retrieved for the current user turn into the system prompt's **volatile suffix** (after the cache breakpoint).
- **Without an embedder (keyless/Ollama): a single free-text blob.** Falls back to one `AGENTS.md` blob in the LangGraph store under the `_MEMORY_NS`/`_MEMORY_KEY` keys (`core/memory_consolidation.py`), accessed via `get_store()`. `consolidate_memory()` collapses/merges it; `_migrate_legacy_key()` upgrades the old key on read.

GraphQL `agentMemory` query + `updateMemory` (blob) / `updateMemoryItem` (discrete) mutations expose both; `frontend/src/components/MemoryView.tsx` + the `/memory` route render and edit them.

### Hybrid retrieval (`core/retrieval.py`)
`search_memory` and `doc_index.search_chunks` run **two arms and fuse them by rank**:
- **Dense** — cosine over stored embeddings (as before).
- **Sparse** — BM25 via SQLite **FTS5**. `memories_fts` / `document_chunks_fts` / `messages_fts` are external-content virtual tables created in `db/engine.py:_ensure_fts()` (called from `_migrate`), kept in sync by AFTER INSERT/DELETE/UPDATE-OF-text triggers on the source tables — no application bookkeeping. Missing FTS5 support degrades to dense-only with a warning; a fresh index backfills via `INSERT INTO x_fts(x_fts) VALUES('rebuild')`, so adding one to `_FTS_TABLES` indexes existing rows on the next boot.

`messages_fts` is the odd one out: messages carry no embeddings, so it is not a hybrid arm but the **entire** implementation of `jarvis.search_conversations` — lexical only, no dense fallback, hence the LIKE-scan path in the SDK for SQLite builds without FTS5.

**Never pass user text to `MATCH`** — FTS5 parses it as a query language and raises on bare `"`, `*`, `:`, `-`, or the bare keywords AND/OR/NOT/NEAR. Always go through `fts_match_expr()`, which tokenizes to word chars, drops stopwords, quotes each term, and ORs them. `bm25()` returns a *negative* score where lower is better — `ORDER BY bm25(t)` ascending; callers use rank order only, never the magnitude.

Fusion is **RRF** (`rrf_fuse`), not a weighted sum: bm25 is unbounded/negative and cosine is [-1, 1], so any linear blend of raw scores is meaningless. `select_hybrid()` then applies the cutoff — an item survives if its cosine clears `max(min_score, rel_drop * best_cosine)` **or** it is a top-`k` lexical hit (bypassing the dense floor is the point of the sparse arm: exact tokens — error codes, filenames, ids — carry no meaning for an embedding to encode). **Returning zero results is a valid outcome**; callers must handle an empty list rather than assuming top-k.

`min_score` is model-specific and cannot be derived a priori — tune per install via `JARVIS_MEMORY_MIN_COSINE` / `JARVIS_MEMORY_REL_DROP` / `JARVIS_DOCS_MIN_COSINE` / `JARVIS_DOCS_REL_DROP`, using the kept/dropped scores `select_hybrid` logs. Because the sparse arm needs no embedder, memory + document search now work on keyless setups (lexical-only) instead of returning nothing.

## Projects Feature
A **project** groups web conversations under shared context (claude.ai-style): `Project.instructions` (user-owned guidance) and `Project.memory` (a free-text blob the agent maintains itself). `Conversation.project_id` is a nullable indexed FK; **only `surface="web"` conversations may join** (enforced in `set_conversation_project`, db/ops.py). Deleting a project keeps its conversations — the FK is nulled explicitly before the row delete.
- **Injection**: `_project_volatile_parts` (core/agents.py) re-reads the project row **every model iteration** (todos pattern, NOT `_retrieval_cache`) and appends `## Project` / `### Project Instructions` / `### Project Memory` sections to the volatile suffix — so agent writes and live user edits apply on the very next LLM call without busting the prompt cache. The graph is model-shared, so project context must never be closured into `_build_agent`. When memory is empty it injects a plain `(empty)` — the old placeholder was phrased as an instruction to fill it, and read as a standing order.
- **Scope plumbing**: `_run_agent_task` (server/chat_runtime.py) resolves `Conversation.project_id` fresh at run start and puts it in `config["configurable"]["project_id"]` → `current_ctx().project_id` (tools/context.py). Automation/board/bot/CLI runtimes never set it, so injection + tool are inert there.
- **Agent path**: `jarvis.project_memory(action=read|append|write, content)` (`tools/sdk.py`) — reads over the ro-sqlite connection, writes through the `updateProject` mutation, scoped by the kernel-injected `_project_id`. (`tools/projects.py` holds an older copy of the same tool; it is **unbound** — the SDK is the only path the agent reaches.)
- **Frugality is enforced in three places, because prompt-only restraint didn't hold.** (1) The `_project_volatile_parts` header states one bar — *append only a fact that would make a future conversation act differently* — instead of the previous three MUST-directives + per-turn "did we learn something?" self-check, and the empty state is a plain `(empty)` rather than a nag. (2) `append` dedups: `core/text_dedupe.dedupe_against` — shared with the consolidation job's merge mode so both answer "is this already said?" identically — drops lines already present (exact, substring, or ≥0.85 `SequenceMatcher` on marker/case/punctuation-normalized text), prunes headings left with no surviving body, and returns "nothing appended" when everything was a repeat. (3) `_PROJECT_MEMORY_CAP` (24k) is enforced on the **SDK** path — over-cap raises and tells the agent to `write` a condensed version. The cap deliberately does not live in the `updateProject` resolver, so a human editing memory in the UI isn't blocked.
- **Why it matters**: `project_memory` is a `cacheable=False` segment injected into *every* LLM call of *every* conversation in the project, so a junk entry is billed forever. That asymmetry is the reason the bar is "don't write if in doubt".
- **Consolidation job** (`core/project_memory_consolidation.py`) — an interval sweep (`register_project_memory_job`, every 30 min) that replaced a fire-and-forget hook in `chat_runtime.py`. That hook ran after every completed chat, i.e. once per **turn**: there is no end-of-conversation signal to hook, so it never had a session to batch over. The job batches over the `messages` table instead — the messages a turn already writes *are* the buffer, so nothing needs staging.
  - **Three writers, split by authority, not schedule** — the agent in-band (instant, add-only), the job's **merge** mode (~15–45 min, add-only), the job's **rewrite** mode (~daily, the only writer that may delete). Two tiers that could both evict would fight over the same entries on different schedules with no way to tell which dropped a fact.
  - **Merge is add-only in code, not by prompt** — whatever the model returns is passed through `core/text_dedupe.dedupe_against` against the existing memory, and only surviving lines are appended; the existing text is carried over verbatim. A merge that would breach `_MEMORY_CAP` or `_MAX_BULLETS` escalates to rewrite in the same pass.
  - **Gates**, in order: new messages exist → the newest is ≥ `_QUIET_MINUTES` (15) old **or** material has been waiting ≥ `_MAX_STALENESS_HOURS` (24, so a never-quiet project still consolidates) → ≥ `_MIN_NEW_CHARS`. Staleness is measured from the **oldest unconsumed message**, not the watermark: a project with no watermark would otherwise read as maximally stale and skip the quiet wait on its first pass. A project with no recorded rewrite is likewise **not** treated as rewrite-due — the first pass over existing memory merges, and that pass starts the clock.
  - **Two watermarks per project** (`messages_through`, `last_rewrite_at`) live in the LangGraph store under `_META_NS`, mirroring `memory_consolidation`'s `last_run_at` — so no migration. `messages_through` advances only as far as `_render_material` actually consumed, which turns a budget cut into a backlog instead of the silent gap `get_recent_messages` still has (it takes the newest 200 and advances past everything older).
  - **Concurrency**: `_commit_memory` compare-and-sets on `Project.updated_at`; a losing pass declines to advance its watermark. Correctness doesn't depend on it — all three writers derive from the same transcript, so a clobbered fact is re-derived next pass.
  - Per-message truncation is asymmetric (`_USER_MSG_CAP` 1.2k / `_ASSISTANT_MSG_CAP` 3k) with an explicit `…[truncated]` marker. A single cap guts the assistant side, which is where decisions and contracts get stated, and biases extraction toward whatever is easy to say in the first few hundred characters.
  - On-demand: the `consolidateProjectMemory(id, model)` mutation runs one pass with `force=True` (skips the quiet + minimum-material gates only).
- **GraphQL + UI**: `projects`/`project` queries; `createProject`/`updateProject`/`deleteProject`/`consolidateProjectMemory` + `setConversationProject(conversationId, projectId)` (dedicated mutation — `updateConversation`'s None-means-unchanged convention can't express "clear"; pass `projectId: null` to remove). `StartTaskInput.projectId` attaches a *new* conversation at creation. Frontend: `/projects` + `/projects/$id` routes (`ProjectsView`/`ProjectDetail`), a project badge on `/c/$id`, and a new-chat InputBox embedded in the detail page.

## Skills Feature
A **skill** is a named, reusable procedure the agent can author and later reload: a `description` (the routing key, embedded for intent retrieval) plus a `body` (the full instructions, loaded on demand). Stored in the `Skill` SQL table.
- **Agent tools** (`tools/skills.py`, all bound): `use_skill(name)` loads a body to follow; `manage_skills(action=list/create/update/delete, ...)` curates them.
- **Surfacing** (`core/skill_store.py`): each description is embedded; `skill_catalog(query)` returns enabled skills ranked by intent match, which `core/agents.py` (`_skills_volatile_parts`) injects as a `## Available Skills` list — **name + description only** — in the volatile suffix. The body stays out of context until `use_skill` pulls it.
- **GraphQL + UI**: `skills` query + `createSkill`/`updateSkill`/`deleteSkill` mutations (`server/graphql/queries|mutations/skill.py`, type in `types/skill.py`); `frontend/src/components/SkillsView.tsx` + the `/skills` route manage them.

## Notifications
`NotificationChannel` rows define delivery targets. GraphQL `notification` query/mutation + `frontend/src/components/NotificationsEditor.tsx` manage them; delivery logic in `core/notifications.py`.

## Uploads & Documents
- **Uploads** are *staging-only* (no DB table). `POST /uploads` (`routes_uploads.py`) stores the bytes + a `.meta.json` under `staging_dir` and returns an opaque `uploadId`; the client passes that back in `startTask` instead of inlining large files in the GraphQL body. A scheduler job GCs stale staged files. Cap: 100 MiB/file.
- **Documents** are extracted-text sources persisted for a conversation. When a chat is started with attachments, `register_chat_task` (`chat_runtime.py`) decodes/writes the bytes under `documents_dir` and creates a `Document` row. Raw bytes: `GET /documents/{id}/raw`. Text extraction: `core/document_extractor.py` (PDF/DOCX/XLSX). Image attachments flow through the model's vision path instead.
- **Large documents are indexed, not inlined.** Documents whose extracted text exceeds `INLINE_THRESHOLD` (12k chars, `core/doc_index.py`) are chunked + embedded into `DocumentChunk` rows and replaced in the message by a stub carrying the `document_id`; the agent retrieves passages via the `search_documents`/`read_document` tools (`tools/documents.py`, conversation-scoped). Embeddings use a Gemini model (default `models/gemini-embedding-001`, override via `config set embedding.model <id>`; needs `GOOGLE_API_KEY`). Batches embed concurrently under a semaphore (`_EMBED_CONCURRENCY`, default 4, `JARVIS_EMBED_CONCURRENCY`). When embeddings are unavailable — or the document came from a source without a `Document` row (bots, CLI) — it falls back to inlining (capped at 80k chars), so nothing breaks on keyless/Ollama-only setups. Search is a single vectorized numpy matmul (`core/retrieval.py:cosine_ranking`) over the conversation's chunks; chunks cascade-delete with their Document.
- **Indexing is backgrounded, and readers must wait on it.** `start_indexing()` returns immediately so embedding a large PDF doesn't sit in front of the first token; `_document_part` no longer awaits it. This is only safe because of the readiness signal: **an empty retrieval result is a *valid* outcome** (`select_hybrid`), so a search racing the indexer would return nothing and the agent would conclude the document is irrelevant. `Document.index_status` (`pending` | `indexed` | `failed`; NULL = never indexed) is the durable signal, written in the same transaction as the chunks. Two waiters, because the `jarvis` SDK runs in a **separate kernel process**: in-process tools use `await_index_ready` / `await_conversation_indexes` (await the real task, fall back to polling), and `tools/sdk.py` polls the column via `_wait_for_index`. **Any new document-reading path must wait first.** Background indexing can no longer fall back to inlining (the message content is already fixed), so a failure records `index_status='failed'` and the tools report it instead of silently returning nothing.

## Database
- SQLite at `~/.jarvis/database.db` (configurable via `DATABASE_URL` env var).
- Async via `aiosqlite` + SQLAlchemy async.
- `async_session` uses `expire_on_commit=False` — no `session.refresh()` needed after commit.
- `update_*` functions must use ORM-level `setattr` (not raw SQL UPDATE) so `onupdate` callbacks fire.
- All ForeignKey columns carry `index=True`; new ones must too.
- Tables: `Conversation, Message, Step, Automation, AutomationRun, BoardTask, BoardTaskLink, ConfigSetting, NotificationChannel, Artifact, ArtifactVersion, Document, DocumentChunk, Workflow, WorkflowRun, Job, Memory, Project, Skill` (`Job` = the durable queue's backing table; `DocumentChunk` = embedded passages for large-doc retrieval; `Memory` = discrete vector memory items; `Project` = conversation groups with shared instructions/memory; `Skill` = reusable agent procedures; `BoardTask`/`BoardTaskLink` = the task board's cards and dependency edges; `ArtifactVersion` = versioned snapshots of artifacts).
- Two SQLite files live under `~/.jarvis/`: `database.db` (app state) and `checkpoints.db` (LangGraph thread state + the store, keyed by `thread_id == conversation_id`). Conversation deletion cascades both: ORM deletes app-DB rows, then `delete_conversation` calls `adelete_thread(conv_id)` on the async checkpointer.
- **The two files get their PRAGMAs from different places.** `database.db` is set per pooled connection in `db/engine.py:_set_sqlite_pragmas`. `checkpoints.db` is opened by `AsyncSqliteSaver`/`AsyncSqliteStore`, which set nothing at connect time — langgraph's `setup()` does set `journal_mode=WAL`, but lazily, on the first checkpoint operation, and never sets `busy_timeout` (default **0**: a writer that finds the lock held fails immediately instead of waiting) or `synchronous`. `server/entrypoint.py:_tune_checkpoint_connections` applies WAL + `busy_timeout=30000` + `synchronous=NORMAL` to both handles right after they open. Anything else that opens `checkpoints.db` should do the same.

### Checkpoint retention (`core/checkpoint_retention.py`)
LangGraph re-serializes the **entire** graph state on every super-step, so `checkpoints.db` grows quadratically with run length and nothing in LangGraph reclaims it — measured, a 48-iteration run over a 302 KB conversation leaves 99 rows / 16 MB (53× amplification). `prune_checkpoints()` is an **online** sweep (hourly at `:20` via `register_checkpoint_prune_job`) that handles the two kinds of garbage differently:
- **Root ns** (`checkpoint_ns=''`) — keep the newest `KEEP_PER_THREAD` (3) per thread. Resume-after-restart, `Command(resume=...)` and interrupts all read only the latest.
- **Subgraph ns** (`tools:<uuid>`, worker/tool subgraphs) — the namespace embeds a per-invocation uuid, so each is written once and never revisited. Deleted outright; these are the bulk of the bytes because each snapshots the full parent state.

Two guards keep it off live data, both deliberately over-cautious (a row that survives is caught next sweep; deleting a live resume point is not recoverable): threads in the `_tasks` registry are skipped, and no checkpoint younger than `MIN_AGE_SECONDS` (1h) is touched. Ages come from the checkpoint_id itself — LangGraph mints them with **uuid6**, so the id is both time-sortable (lexical DESC == newest first, same ordering LangGraph's own `list()` uses) and carries a timestamp, no blob deserialization needed. Matching `writes` rows are deleted with each checkpoint.

The sweep does **not** VACUUM — freed pages go on SQLite's freelist and are reused by later checkpoint writes, so the file plateaus rather than shrinking. To hand pages back to the filesystem, stop the server and run `uv run python main.py maintenance prune-checkpoints` (the offline path, `main.py`: keeps exactly one checkpoint per thread, then VACUUMs).

## Default Model
Compile-time default is `google_genai:gemma-4-31b-it` (requires `GOOGLE_API_KEY`). Override at runtime via `uv run python main.py model set-default <id>` — stored in the `config_settings` table under key `default.model`. `get_default_model(session)` in `db/ops.py` returns the DB value or falls back to the catalog default. Ollama and AWS Bedrock models also available — see `core/model_catalog.py`.

`Conversation.surface` records where a conversation lives: `web` | `telegram` | `discord` | `automation` | `task`. The `conversations` GraphQL query filters to `surface: "web"` by default so bot threads, automation histories, and board-task transcripts stay out of the web sidebar (pass another surface, or `null` for all). Bots and the automation/task-board runtimes set it at `get_or_create_conversation` call sites; `_migrate()` backfills pre-existing bot rows by id prefix.

`Conversation.model` is **sticky per-conversation**: the chat `startTask` mutation updates it whenever the request's model differs from the stored value, and the InputBox commits a conversation-update mutation on dropdown change so a model picked mid-conversation persists across reloads. The frontend seeds the dropdown from `conversation.model`, falling back to the catalog default only when no conversation exists yet.

## LLM-call node requirement
Any new agent-loop node that calls an LLM must run `strip_historical_thinking` + `repair_orphan_tool_calls` + `build_llm_messages` (all defined in `core/messages.py`) on the history **before** `.ainvoke` — otherwise Bedrock/Anthropic reject the call (orphaned tool calls / stale thinking blocks). Loop nodes should also run `apply_per_call_compaction()` (token hygiene: `elide_stale_tool_results` + `collapse_old_tool_results`, per-call only, checkpointer keeps full text) from `core/compaction.py`. `group_messages()` handles Anthropic HumanMessage tool_result carriers via `_is_tool_result_carrier`. For multi-breakpoint caching, pass `cache_segments: list[CacheSegment]` from `core/context_cache.py` to `build_llm_messages`.

### Reasoning blocks have three spellings
`_THINKING_TYPES` in `core/messages.py` must list **`thinking`, `redacted_thinking` *and* `reasoning`** — the first two are Anthropic's names, the third is LangChain's v1 content-block name for the same thing. Providers dereference these blocks unguarded, so a spelling missing from that set is a block that survives into history and crashes a *different* provider later. Concretely: a Meta run persists `{"type": "reasoning", "summary": [], ...}` blocks that carry no `reasoning` key, `langchain_google_genai` does a bare `part["reasoning"]`, and every subsequent Gemini call on that thread dies with `KeyError: 'reasoning'` in ~7ms — before any network request, so it reads like a model outage rather than a history problem. Threads are shared across models, so this is cross-provider contamination, not a per-provider concern. `_THINKING_KWARGS` strips the same content from `additional_kwargs`.

### Compaction contract
`maybe_compact()` returns a **`CompactionResult`**, never `None`: `.messages` (always the per-call-compacted view, ready for the LLM), `.state_update` (RemoveMessage deltas + summary, empty when nothing fired), `.compacted` (bool). **Use `.messages` directly — do not call `apply_per_call_compaction` on the result.** Counting tokens requires building the leaned view, so `maybe_compact` hands it back; calling both repeats the elide and grouping passes on every agent-loop iteration. A node that only needs hygiene and no token check still calls `apply_per_call_compaction` on its own (see the worker path in `core/agents.py`).

### Cache-segment tagging
`_memory_volatile_parts` / `_skills_volatile_parts` / `_project_volatile_parts` return **`list[CacheSegment]`**, each tagging its own `name` + `cacheable`. `model_request_node` only sorts them by `_SEGMENT_STABILITY` rank (most stable first, since prefix caching invalidates everything after a changed block) and routes `cacheable=False` to the volatile suffix. This replaced heading-sniffing (`if "### Project Memory" in part`), where renaming a section heading silently moved content across the cache breakpoint. **A new segment needs a `_SEGMENT_STABILITY` entry**, or it sorts last among cached blocks. Per-turn-varying content (`relevant_memories`) and live-edited content (`project_memory`) must stay `cacheable=False`.

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

## Frontend styling (StyleX)
No UI library and no global stylesheet: every rule is a `stylex.create` object
compiled to atomic classes, colocated with the component and dead-code-eliminated
with it. The palette lives in `frontend/src/theme/`.

- **Tokens** — `theme/tokens.stylex.ts`. `channels` holds raw hue triples and
  `colors` derives from them (two `defineVars` calls, because a single
  self-referencing one is a TS circularity error), plus `type` / `space` /
  `radii` / `layout`. `themes.stylex.ts` is the light `createTheme`;
  `keyframes.stylex.ts` re-exports every keyframe through `defineVars`, which is
  the documented way to use one across files.
- **Every export of a `.stylex.ts` file is treated as a StyleX constant.** A
  plain `export const X = 'var(--kb-inset, 0px)'` compiles to a *hashed
  identifier*, producing invalid CSS the browser drops silently. Raw CSS syntax
  goes in `stylex.defineConsts` — that is what `css` and `bp` are for.
- **Shorthands are split into longhands**, so `background: 'none'` only sets
  `background-image`. Always write `backgroundColor` / `backgroundImage`, and
  `animationName` / `animationDuration` / … rather than `animation`.
- **Where styles live**: colocated at the bottom of the component when small; in
  a sibling `*.styles.ts` when large; in `components/ui/` when shared (`btn`,
  `field`, `page`, `badge`, `modal`, `Switch`, `prose`, …). Import from `./ui`,
  not its parts.
- **Combinators do not exist.** A parent styling a child becomes a prop, or a
  CSS custom property the parent publishes and the child reads
  (`--item-actions-opacity`, `--md-*`, `--rf-*`, `--btn-icon-opacity`). Sibling
  selectors become components — that is why `Switch` is one.
- **Responsive rules fold inward** as per-property conditions
  (`width: {default: 480, '@media (max-width: 860px)': '100%'}`), not a separate
  media section.
- **`base.css` (~285 lines) is the whole global stylesheet**, and only holds what
  StyleX structurally cannot express: the reset, the pre-paint theme block, the
  `::view-transition-*` animations, `[data-md]` rules for `marked` output
  injected as HTML, and the `.react-flow__*` / `.wf-handle` overrides for DOM
  `@xyflow/react` renders itself. Those last ones are prefixed `:root` because
  the library's own stylesheet loads after this file and would otherwise win.
- **The theme is a hashed class**, so the pre-paint script in `index.html` cannot
  apply it: it stamps `data-theme` only, and `theme/applyTheme.ts` adds the real
  class (plus the body styles, since `<body>` is React's parent) at boot. The
  four `--pp-*` literals in `base.css` cover the window before the bundle runs
  and must track `colors.bg` / `colors.text`.
- `vite.config.ts` sets `cssInjectionTarget` explicitly — the plugin's default
  picks the first `.css` asset, which here is the lazy WorkflowEditor chunk.
  `.browserslistrc` must target browsers with native `light-dark()`.

## Environment
- Python 3.13, managed with `uv`.
- Backend stack: FastAPI + **Strawberry GraphQL** (`strawberry-graphql[fastapi]`, graphql-ws), SQLAlchemy async + `aiosqlite`, LangGraph/LangChain (Anthropic, AWS Bedrock, Google GenAI, Ollama, OpenAI — used for OpenRouter, Meta), `langgraph-checkpoint-sqlite`, APScheduler, Playwright (the `read()` fallback in `tools/research.py`), faster-whisper/mlx-whisper + piper-tts (audio), yfinance (finance tools).
- Frontend stack: React 19, TanStack Router/Query, **Relay** (`babel-plugin-relay` run as a standalone Vite transform — see `vite.config.ts`), **StyleX** (`@stylexjs/unplugin`, a second independent Babel pass in the same config), Vite, TypeScript.
- Tests: `uv run pytest` (pytest + pytest-asyncio, `asyncio_mode = "auto"`). Tests live in `tests/` and run against a throwaway `WORK_DIR` — never `~/.jarvis`. The `jarvis` fixture (`tests/conftest.py`) boots a full `JarvisRunner` in-process without uvicorn, the scheduler, or queue workers, so handlers can be driven directly and synchronously. Tests that call a real model are marked `llm` and skip without `GOOGLE_API_KEY`; run just those with `-m llm`.
- No linter configured; use `uvx pyrefly check --summarize-errors` for Python type checking and `pnpm typecheck` for the frontend. Frontend formatting via `pnpm fmt` (oxfmt).
- Frontend: no UI library. Styling is **StyleX** — see "Frontend styling" below.
