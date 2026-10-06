# Jarvis — Claude Setup

Multi-agent research assistant. Users submit queries via the web UI, CLI, or Telegram/Discord bots; agents run them and stream results live. Also: automations (scheduled/manual), a kanban task board, visual workflow graphs, projects, skills, persistent memory, document uploads, notification channels.

Deeper notes live next to the code and load when you work there:
`core/CLAUDE.md` (agent loop, prompt caching, model catalog, memory, MCP, approvals) ·
`server/CLAUDE.md` (GraphQL, job queue + live streaming, automations, board, projects, bots) ·
`tools/CLAUDE.md` (the `jarvis` SDK, web/browser) · `db/CLAUDE.md` · `workflow/CLAUDE.md` ·
`frontend/CLAUDE.md` · `edge/CLAUDE.md` (+ `edge/README.md`).

## Architecture

- **API is GraphQL-first** — Strawberry + FastAPI. Queries/mutations over HTTP POST `/graphql`; live streams over `graphql-ws` subscriptions on the same path. REST only for what GraphQL can't carry: binary download, file upload, TTS/transcription, `/ws/live`, `/ws/browser`, log tailing, health. Frontend is React 19 + Relay.
- **A Rust edge (`edge/`) sits in front of Python** — phase 1 of moving to Rust so jarvis runs on old, low-RAM hardware. The edge owns :8000, answers the GraphQL operations ported so far from SQLite, fires all schedules, and proxies everything else to Python on :8001. With `JARVIS_WORKER_CMD` set it **starts Python on demand and stops it after `JARVIS_WORKER_IDLE` seconds idle**. Consequence for Python code: process start is no longer crash recovery — anything that runs at startup must be safe to run every few minutes.
- **Long-running work goes through a durable SQLite job queue** (`core/queue/`), never bare `asyncio.create_task`. `job.id == task_id` is the single cancellation key. A job with `runtime = 'edge'` is the edge's agent loop's (`edge/src/agent/`): anything in Python that claims, reaps or sweeps jobs or their run rows must skip it.

```
main.py          CLI (typer): run, start, config *, model *, memory *, maintenance *
core/            agent factory + loop, messages/compaction/caching, model catalog, memory,
                 MCP, approvals + tool gate, budget/perf, queue, scheduler, kernels, config
db/              models.py (ORM), ops.py (async CRUD), engine.py (init + _migrate)
server/          entrypoint.py (lifespan, routers), graphql/ (types, queries, mutations,
                 subscriptions), *_runtime.py (job handlers), routes_*.py (REST), bots
tools/           bound agent tools + sdk.py (the kernel-preloaded `jarvis` SDK), research/browser
workflow/        engine.py (BFS executor) + nodes.py (node types)
frontend/        React + TanStack Router + Relay + StyleX + Vite
edge/            Rust edge (axum) — see edge/README.md
tests/           pytest; parity tests diff the edge against Python
```

## Commands

```bash
# Backend — edge on :8000, starting/stopping Python on :8001 on demand
cd edge && JARVIS_APP_DIR=.. \
  JARVIS_WORKER_CMD='exec .venv/bin/uvicorn server.entrypoint:app --port $JARVIS_BACKEND_PORT' \
  cargo run
# …or Python always on (with --reload), linked to the edge
JARVIS_EDGE_URL=http://127.0.0.1:8000 uv run uvicorn server.entrypoint:app --reload --port 8001
cd edge && cargo run
# …or Python alone on :8000 — still fully works
uv run uvicorn server.entrypoint:app --reload

uv run python main.py run "<query>"          # one-shot CLI query
uv run python main.py config set|get|list|delete <key> [value]
uv run python main.py model list|add|remove|set-default|sync
edge/target/debug/jarvis-edge run|config|model|memory …   # the same CLI in Rust (edge/src/cli/)
uv add <package>                             # dependency (pyproject.toml + uv.lock)

uv run pytest                                # tests; `-m llm` for real-model tests (need GOOGLE_API_KEY)
uvx pyrefly check --summarize-errors         # Python type check (no linter configured)

cd frontend && pnpm dev                      # vite + relay-compiler --watch on :5173 (always pnpm, never npm)
pnpm schema && pnpm relay                    # after changing the Python GraphQL schema
pnpm typecheck / pnpm build
```

Tests run against a throwaway `WORK_DIR`, never `~/.jarvis`. The `jarvis` fixture (`tests/conftest.py`) boots a full `JarvisRunner` in-process without uvicorn, the scheduler, or queue workers.

## Rules that are easy to break

**Agent / LLM calls**
- Any node that calls an LLM must run `strip_historical_thinking` + `repair_orphan_tool_calls` + `build_llm_messages` (`core/messages.py`) on history before `.ainvoke`, or Bedrock/Anthropic reject the call. Loop nodes also run `apply_per_call_compaction()`. Pass `cache_segments` and `cache_provider=spec.provider` when caching. See `core/CLAUDE.md`.
- `_THINKING_TYPES` must list `thinking`, `redacted_thinking` **and** `reasoning` — threads are shared across models, and a reasoning block one provider leaves behind crashes another.
- `maybe_compact()` returns a `CompactionResult` whose `.messages` is already compacted — don't also call `apply_per_call_compaction` on it.
- A new prompt `CacheSegment` needs a `_SEGMENT_STABILITY` entry in `core/agents.py`. Per-turn or live-edited content must be `cacheable=False`.
- Pick models via `db.ops.resolve_model()` (async) / `resolve_model_spec()` (sync) — stored model ids can outlive the catalog. `is_valid_model` is only for write boundaries.

**Runs and streaming**
- `register_*` pre-registers `_tasks[task_id] = TaskState(...)` **before** committing the job, so a subscriber can't race the worker. Handlers read `state = _tasks[task_id]`; never `setdefault`. Behind the edge, pass `job=job` to `get_or_create_task_state`.
- Every event goes through `emit_event` — it is the one append the edge link ships. Workflow nodes use `_emit()`.
- Custom events from tools: `adispatch_custom_event(name, {"type": name, ...})` — the `"type"` key is required.

**GraphQL**
- `get_context` also runs for the subscription WebSocket: its params must be `HTTPConnection`, never `Request`, or every subscription silently fails while queries keep working.
- A new `*Query`/`*Mutation`/`*Subscription` mixin must be added to `merge_types(...)` in `server/graphql/schema.py`.

**Database**
- `async_session` has `expire_on_commit=False` — don't `session.refresh()` after commit.
- Updates use ORM `setattr`, not raw `UPDATE`, so `onupdate` fires. FK columns get `index=True`. Schema changes to existing tables go in `_migrate()`. The edge owns the schema (`edge/src/schema.rs`): a model change is re-captured into `edge/src/schema.sql`, and a `_migrate` step is ported there too (`tests/test_edge_schema.py`).

**Scheduling**
- Build every cron trigger with `core/scheduler.py:_cron(expr)`, never `CronTrigger.from_crontab` — it applies the timezone and the Unix day-of-week fix. `edge/src/cron.rs` is a port: change both.
- Every path that creates/updates/deletes a scheduled automation calls `_register_scheduler_job` / `_remove_scheduler_job`.

**Agent tools**
- The main agent is code-first: only graph-coupled tools are bound (`run_cell`, `write_artifact`, `write_todos`/`set_todo_status`, `spawn_workers`, `run_workflow`, `remember` with an embedder, `complete_task`/`block_task` on board runs). Everything else goes in the `jarvis` SDK (`tools/sdk.py`). See `tools/CLAUDE.md`.
- SDK writes go through GraphQL mutations, not direct DB writes, so server-side side effects fire.

**Frontend**
- After changing a `graphql` literal run `pnpm relay`; after changing the Python schema, `pnpm schema` first. Never edit `src/__generated__/` or `routeTree.gen.ts`.
- `pnpm fmt` rewrites files you didn't touch — format only what you changed.

## Security posture
No runtime LLM safety gates. Safety rests on the operating constraints in `core/system_prompt.md` and on deployment isolation (container / isolated box). Don't add in-process sandboxing or LLM judges.
