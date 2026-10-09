# Jarvis — Claude Setup

Multi-agent research assistant. Users submit queries via the web UI, CLI, or Telegram/Discord bots; agents run them and stream results live. Also: automations (scheduled/manual), a kanban task board, visual workflow graphs, projects, skills, persistent memory, notification channels.

Deeper notes live next to the code and load when you work there:
`edge/CLAUDE.md` (+ `edge/README.md` — the server) · `tools/CLAUDE.md` (the `jarvis` SDK, web/browser) ·
`frontend/CLAUDE.md`.

## Architecture

- **The server is Rust** (`edge/`, axum) — moved off Python so jarvis runs on old, low-RAM hardware. One process on :8000: GraphQL (queries/mutations over HTTP POST `/graphql`, live streams over `graphql-ws` subscriptions on the same path), REST only for what GraphQL can't carry (binary download, `/ws/browser`, log tailing, health), the agent loop, the scheduler, the bots, MCP, and the SPA. Frontend is React 19 + Relay.
- **Python is the agent's notebook kernels**: the agent writes Python in `run_cell`, in an `ipykernel` the server starts, with the `jarvis` SDK (`tools/sdk.py`) preloaded. The SDK reads the database read-only and goes through the server's GraphQL for everything else; it needs only the standard library, `httpx` and the browser libraries — no other Python.
- **Long-running work goes through a durable SQLite job queue**, never a bare spawned task: a trigger writes the `jobs` row, the agent loop (`edge/src/agent/queue.rs`) claims it. `job.id == task_id` is the single cancellation key.

```
edge/            the server and CLI (Rust) — see edge/README.md
tools/           sdk.py (the kernel-preloaded `jarvis` SDK), research.py / browser.py (web + browser)
frontend/        React + TanStack Router + Relay + StyleX + Vite
tests/           pytest; tests/test_edge_*.py drive the binary against Python's recorded answers
```

## Commands

```bash
cd edge && JARVIS_APP_DIR=.. cargo run       # the server on :8000
edge/target/debug/jarvis-edge run|config|model|memory …   # the CLI
cd edge && cargo test                        # unit tests

uv add <package>                             # Python dependency (pyproject.toml + uv.lock) — for the kernels
uv run pytest                                # tests; `-m llm` for real-model tests (need GOOGLE_API_KEY)
uvx pyrefly check --summarize-errors         # Python type check (no linter configured)

cd frontend && pnpm dev                      # vite + relay-compiler --watch on :5173 (always pnpm, never npm)
pnpm schema && pnpm relay                    # after changing the server's GraphQL schema
pnpm typecheck / pnpm build
```

Tests run against a throwaway `WORK_DIR`, never `~/.jarvis`. `tests/test_edge_*.py` build the binary once per session and compare it against Python's answers, recorded before Python's server and runtime were deleted (`tests/python_golden.py`); a deliberate change to an answer is an edit to the recording. Seed test databases with `tests/seed.py` (rows as the ORM wrote them), never by hand-rolled defaults.

## Rules that are easy to break

**Agent / LLM calls** (`edge/src/agent/`, `edge/src/llm/`)
- Every model call shapes its history through `llm/shape.rs` (`strip_historical_thinking`, `repair_orphan_tool_calls`, the cache layout) or Bedrock/Anthropic reject it. Thinking types include `thinking`, `redacted_thinking` **and** `reasoning` — threads are shared across models.
- A new prompt segment needs its place in the stability order (`agent/prompt.rs`); per-turn or live-edited content stays out of the cached region.
- Pick a run's model with `catalog::resolve_model` — stored model ids can outlive the catalog.

**Runs and streaming**
- A trigger registers the run **before** committing its job (`gql/start.rs:commit_run`), so a subscriber can't race the agent loop.
- Every run event is appended through the run (`Run::emit_local` / `agent/events.rs`), in the raw `{"event", "data"}` shape the subscriptions coerce.

**GraphQL**
- A new query/mutation object goes into `Query` / `Mutation` in `edge/src/gql/mod.rs`; then `pnpm schema && pnpm relay`.
- An agent's (`X-Jarvis-Caller: agent`) destructive write passes `approval::gate_action` when it is one of the deferred actions.

**Database**
- The schema is `edge/src/schema.sql` (a new database) plus `schema.rs:migrate` (an existing one): a change goes in both — `tests/test_edge_schema.py` checks a migrated database has the fresh shape.
- A write leaves a row exactly as SQLAlchemy did: `uuid4()` ids, its timestamp text, `updated_at` bumped where `onupdate` fired, cascades as explicit DELETEs.

**Scheduling**
- Cron is `edge/src/cron.rs`, a port of APScheduler's `CronTrigger` (timezone + Unix day-of-week), held to its recorded answers (`tests/test_edge_schedule.py`).
- Every automation write tells the scheduler (`Scheduler::schedules_changed`).

**Agent tools**
- The main agent is code-first: only loop-coupled tools are bound (`run_cell`, `write_artifact`, `write_todos`/`set_todo_status`, `spawn_workers`, `run_workflow`, `remember`, `complete_task`/`block_task` on board runs; schemas in `edge/src/agent/tools.json`). Everything else goes in the `jarvis` SDK (`tools/sdk.py`). See `tools/CLAUDE.md`.
- SDK writes go through GraphQL mutations, not direct DB writes, so server-side side effects fire.

**Frontend**
- After changing a `graphql` literal run `pnpm relay`; after changing the server's schema, `pnpm schema` first. Never edit `src/__generated__/` or `routeTree.gen.ts`.
- `pnpm fmt` rewrites files you didn't touch — format only what you changed.

## Security posture
No runtime LLM safety gates. Safety rests on the operating constraints in `edge/src/system_prompt.md` (compiled into the server) and on deployment isolation (container / isolated box). Don't add in-process sandboxing or LLM judges.
