# server/ — API, runs and features

## Adding a GraphQL field
1. CRUD in `db/ops.py` if needed.
2. Type in `graphql/types/`. DB-backed types are Relay Nodes: `id: relay.NodeID[str]`, `from_db`, `resolve_node`. Resolve expensive fields lazily.
3. Resolver in the relevant `*Query`/`*Mutation`/`*Subscription` mixin; a new mixin goes into `merge_types(...)` in `graphql/schema.py`.
4. Session via `info.context["session"]`. `get_context` params must be `HTTPConnection` (it runs for WebSockets too).
5. `cd frontend && pnpm schema && pnpm relay`, then a module under `frontend/src/relay/`.
6. If the edge already serves this type, update the Rust port and its parity test (see `edge/README.md`).

## Adding a REST endpoint (only when GraphQL doesn't fit)
`routes_*.py` with `Annotated[AsyncSession, Depends(get_session)]` → `app.include_router` in `entrypoint.py` → proxy entry in `frontend/vite.config.ts` (`ws: true` for sockets) → helper in `frontend/src/lib/api.ts`.

## Runs: job queue + live streaming
All run kinds (chat, automation, workflow, board, maintenance) share one pattern:
1. A mutation calls `register_*` in `*_runtime.py`, which writes the work row, enqueues a `Job`, and sets `_tasks[task_id] = TaskState(...)` **before** `session.commit()`.
2. A `Worker` (`core/queue/worker.py`) claims the job and runs its handler (`chat_job_handler`, `automation_job_handler`, `workflow_job_handler`, `board_task_job_handler`).
3. The handler emits events via `emit_event`; subscriptions (`taskEvents`, `automationRunEvents`, `boardTaskEvents`, `workflowRunEvents`) yield via `stream_task_events`. If the task is gone they fall back to the DB for a final `done`/`error`.
4. Stop: `stopRunningTask` flips in-process flags **and** calls `get_queue().cancel(task_id)`.

**Behind the edge**, steps 1 and 3–4 are the edge's: it writes the rows + Job (`edge/src/gql/start.rs`), mirrors the run, and serves subscriptions and stops. Handlers create state from the job — pass `job=job` to `get_or_create_task_state`. Operations that need a live `TaskState` (`queueMessage`, `resumeTask`, workflow resumes) reach Python as a `call` to the same function the resolver uses — keep resolvers thin wrappers over `chat_runtime.resume_chat_task`, `workflow_runtime.resume_workflow_run`, etc.

`running_tasks` lists `_tasks`; finished tasks linger ~5s.

### Mid-run message queue (`chat_runtime.py`)
A message sent while a conversation has a run in flight is queued, not started (two runs on one thread would interleave their writes to it). Every surface goes through `route_to_live_run`; `startTask` returns the running task id with `queued: true`.
- Two carriers: `TaskState.pending_input` (fast path, drained synchronously in `model_request_node`) and a `messages` row with status `queued` (renders, survives restart).
- Delivered messages get status `delivered` (not `done`) so the UI can lift them above the reply they landed in.
- Clean finish with leftovers → `_redispatch_queued` starts the next turn. Stop/error/restart → rows stay `queued`; `_adopt_queued_messages` picks them up on the next run.
- Refused while the run is paused on an interrupt, or with attachments (queued rows are text-only).

## Automations (`automation_runtime.py`)
Input types: `prompt`, `code` (subprocess), `webhook`, `monitor` (delta-gated: a reply starting with `NO_CHANGE` finishes as `no_change` and sends no notification).
- Stateful prompt automations and monitors share the conversation/thread `automation_{automation_id}`; overlapping runs are `skipped` (`_has_inflight_sibling`). Stateless runs use `automation_{run_id}`.
- Behind the edge, the edge fires every schedule (automations, board dispatch, maintenance sweeps via `maintenance` jobs); only the kernel reaper stays in APScheduler.

## Task board (`task_board_runtime.py`)
`BoardTask`: `todo → ready → running → blocked/done → archived`, with parent→child `BoardTaskLink`s.
- `dispatch_board_tasks()` is the single dispatcher (behind the edge it asks the edge; `edge/src/schedule.rs:dispatch` must stay a port). Promotes `todo`→`ready` when all parents are done, then enqueues up to `MAX_IN_PROGRESS` by priority.
- Each run gets a **fresh UUID** job id (`BoardTask.job_id`), unlike other kinds, because tasks re-run.
- `_finish_task` only applies when the row still belongs to this run (`job_id == run_id`) and is still `running`. The dispatcher skips `ready` tasks whose previous job is still live.
- `block_task(needs_input=True)` → `answerBoardTask` stores `pending_answer` and re-queues; the next run resumes on the same conversation `boardtask_{id}`.
- `decompose_board_task` parks the original in `todo` **first**, then creates subtasks as its parents.
- Startup sweep flips `running` tasks back to `ready` only if no live job holds them.

## Projects
- Only `surface="web"` conversations may join (`set_conversation_project`). Deleting a project nulls the FK, keeping conversations.
- `_run_agent_task` puts `project_id` in `config["configurable"]`; other runtimes never set it.
- Agent writes go through `jarvis.project_memory` (dedups via `core/text_dedupe.dedupe_against`; 24k cap on the SDK path only).
- `core/project_memory_consolidation.py` runs every 30 min: merge mode is add-only (enforced in code); only rewrite mode (~daily) may delete. Gates: new messages → quiet ≥15 min or waiting ≥24h → minimum material. Watermarks live in the LangGraph store.
- `setConversationProject` exists because `updateConversation` can't express "clear".

## Chat attachments (`chat_runtime.py`, `core/streaming.py`, `core/doc_index.py`)
- `register_chat_task` writes attachments under `documents_dir` and creates `Document` rows; every stub carries the on-disk path so the agent can open it with `run_cell`.
- Tabular files (csv/tsv/xlsx/parquet/jsonl) never enter the conversation: path + measured line count + head only (`_routes_to_code`). State counts only when measured.
- Text over `INLINE_THRESHOLD` (12k chars) is indexed in the background. **Any document-reading path must wait on `Document.index_status`** (`await_index_ready` in-process, `_wait_for_index` in the SDK) — an empty search during indexing reads as "irrelevant".
- Without embeddings or a `Document` row (bots, CLI), text is inlined up to 80k and says when it truncated.

## Config + Maintenance
- Config writes go through `core/settings_admin.apply_setting` so in-process caches (tool policy, MCP, embedder) update. `scheduler.timezone` can't apply live and says so.
- `pruneCheckpoints` runs the online sweep (no VACUUM). `downloadVoice` writes to `.part` then renames.

## Bots (`telegram_bot.py`, `discord_bot.py`)
Behind the edge the bots run there (`edge/src/bots/`) and these modules aren't started (`behind_edge()`); they run only when this server stands alone, so change both. Notifications (`core/notifications.py`) call the Bot API / Discord REST directly and need no running bot.
Enabled by `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN`. Allowlists (`telegram.allowed_users`, `discord.allowed_users`) reject everyone when empty. One thread per chat/channel (`telegram_{chat_id}`, `discord_{channel_id}`).
- Never send a placeholder message — create it on the first real token, then edit roughly every second.
- Discord: replies in DMs, or in guilds when @mentioned / replied to; message cap `_MAX_MSG_LEN = 1900`; needs the Message Content Intent.

## Conversations
- `Conversation.surface`: `web` | `telegram` | `discord` | `automation` | `task`. The `conversations` query defaults to `web`.
- `Conversation.model` is sticky: `startTask` updates it when the model changes.
